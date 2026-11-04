"""
GuruFocus economic-indicator scraper — HTTP-first, no browser.

2026-10-23 rewrite (measured, not assumed)
------------------------------------------
The old version drove a full Selenium session, waited for the rendered history
table, and executed a JS `findSeries(window.__NUXT__)` in the page — because the
series was believed to only exist after client-side hydration. It is not: the
page ships the complete series inside its Nuxt payload as a literal
`datas:{"<id>":[["YYYY-MM-DD",value],…]}` array, and a plain HTTP GET with a
browser TLS fingerprint is served 200 + that array. Verified live:

  * id 151 (SP500_EY)  → 17,583 points, 1957-01-02 … 2026-10-21, last 3.823
    (equals the page's own "Last Value 3.823% / Latest Period 2026-10-21")
  * id 57  (SP500_PE)  → 18,900 points, 1871-01-01 … 2026-10-22

So one request yields the ENTIRE history: no driver, no pagination, no
`driver.quit()` RAM spike, and a missed run self-heals because the upsert covers
every date from the payload. `extract_nuxt_series()` decodes the payload in pure
Python (the aliases are just positional arguments of the `__NUXT__` IIFE), so no
JS engine is required either.

Transport order: Scrapling `Fetcher` (rotating impersonation) → bare `curl_cffi`
(chrome120 — the SAME TLS fingerprint Scrapling rides on, but with no `scrapling`
dependency) → Scrapling stealth browser → plain `requests`. Every path feeds the
same decoder, so each is a real fallback rather than a different parser.

2026-11-01 (later) — the real 403 was scrapling being ABSENT
------------------------------------------------------------
The runtime venv raised `ModuleNotFoundError: No module named 'scrapling'`, so
`HAVE_SCRAPLING` was False and BOTH `http_get()` and `browser_get()` returned
None without a single attempt. The job then dropped to plain `requests`, which
GuruFocus 403s at the TLS-fingerprint layer. `curl_cffi` — installed as a
scrapling dependency and therefore still present — was measured answering 200 +
3,684,418 chars for EY in that exact situation. So `_page_html()` now tries
`_curl_cffi_get()` (chrome120, browser-free) as the first fallback: a missing
scrapling can no longer push the job onto the 403-ing `requests` path.

2026-11-01 — the failure was NEVER the 403
-------------------------------------------
Every transport was measured live and all of them work: `http_get` returned 200 +
3,684,418 chars for EY (17,590 points) and 200 + 3,735,690 for PE (18,906 points),
both in the main thread and inside a daemon thread (what Run Now uses). What
actually killed the job was arithmetic: ``EconomicIndicator.value`` is mapped as
``Numeric``, so DB rows arrive as ``Decimal`` while the decoder produces floats,
and the YOY line ``current_value - last_year_value`` raised TypeError — on EVERY
incremental run, ever since the table had any history. The 403 in the ledger
message was the one-off Cloudflare challenge from 18:17 UTC; the crash is what
made the job unrecoverable. See ``_num()``.

Two related facts, so nobody re-tries them:
  * Plain `requests` (even with a Chrome User-Agent) is 403'd by GuruFocus, which
    reads the TLS fingerprint, not the headers. Only a curl_cffi-backed transport
    (raw `curl_cffi` or Scrapling's `Fetcher`, which rides on it) is served 200.
    Measured 2026-11-01: `curl_cffi.requests.get(url, impersonate="chrome120")`
    called DIRECTLY → 200 + 3,684,418 chars, no "Just a moment..." interstitial
    — the earlier note that raw curl_cffi 403s was WRONG and is corrected here.
  * A challenged IP gets worse the harder it is hammered, so scrapling_fetch now
    logs each rejected fingerprint at WARNING (they were DEBUG-only, which is why
    "no usable GuruFocus page" sat unexplained) and retries the rotation once
    after a pause when the site answered with a block — see RETRY_PAUSE_S there.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# Same convention as eurodollar_scraper.py: make `app` importable when this
# file is executed directly (the Runbook's copy-command runs it as a script,
# where sys.path[0] is the scrapers folder and `import app…` would fail).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from bs4 import BeautifulSoup
from sqlalchemy import func

from app.core.database import SessionLocal
from app.models.economic_indicators import EconomicIndicator
from app.scrapers.scrapling_fetch import (
    FetchResult, RETRY_PAUSE_S, browser_get, http_get, looks_blocked, page_title,
    source_is_expected)

logger = logging.getLogger(__name__)

# Re-written on every run so official revisions and missed crons are picked up.
# The series are monthly/daily-updating, so 120 days of re-upsert covers a
# skipped monthly job several times over while keeping each run small.
LOOKBACK_BUFFER_DAYS = int(os.getenv("GURUFOCUS_LOOKBACK_BUFFER_DAYS", "120"))

# A 200 from GuruFocus is NOT proof of the right series: id 1031 was measured
# answering 200 while redirecting to "Quits: Total Nonfarm". These tokens must
# appear in the page title/URL before any number is trusted.
SERIES_TITLE_TOKENS: Dict[str, Tuple[str, ...]] = {
    "SP500_EY": ("earnings yield",),
    "SP500_PE": ("pe ratio",),
}

# Fingerprints for the browser-free curl_cffi fallback (candidate #2). chrome120
# is measured good on GuruFocus; "chrome" (latest) is the retry partner so a
# single transient WAF challenge on one fingerprint does not fail the run.
_CURL_IMPERSONATIONS: List[str] = [
    t.strip() for t in os.getenv(
        "GURUFOCUS_CURL_IMPERSONATE", "chrome120,chrome").split(",") if t.strip()
]


def _guru_fail(error: str) -> dict:
    """Failure shape understood by job_tracking.with_result_handling."""
    return {"ok": False, "rows": 0, "stage_failures": [
        {"stage": "gurufocus", "error": error[:400]}]}


def _num(value: Any) -> Optional[float]:
    """A DB value as a float, or None.

    ``EconomicIndicator.value``/``yoy_pct`` are mapped as ``Numeric``, so every
    row read back is a ``Decimal`` while the Nuxt decoder produces floats. The
    two do not mix: ``float - Decimal`` raises TypeError (which is what killed
    every incremental run since the table had history — the fetch was fine), and
    ``Decimal("3.897") != 3.897`` is True because the float is 3.896999…, so each
    existing row also looked "changed" and was rewritten on every single run.
    Normalising at this one boundary fixes both.
    """
    return None if value is None else float(value)


# ── Nuxt payload decoder ──────────────────────────────────────────────────────

def series_id_from_url(url: str) -> Optional[str]:
    """`…/economic_indicators/151/sp-500-earnings-yield` → '151'."""
    m = re.search(r"/economic_indicators/(\d+)", url or "")
    return m.group(1) if m else None


def _match_bracket(s: str, start: int, open_ch: str, close_ch: str) -> str:
    """Substring from `start` (must point at `open_ch`) through its match.

    String literals are skipped so a brace inside a quoted URL cannot unbalance
    the count. Returns "" when unbalanced.
    """
    depth, i, in_str, esc = 0, start, None, False
    n = len(s)
    while i < n:
        c = s[i]
        if in_str is not None:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == in_str:
                in_str = None
        elif c == "'" or c == '"':
            in_str = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
        i += 1
    return ""


def _split_args(tail: str) -> List[str]:
    """Split a JS argument list on top-level commas (strings/brackets respected)."""
    args: List[str] = []
    buf: List[str] = []
    depth, in_str, esc = 0, None, False
    for c in tail:
        if in_str is not None:
            buf.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == in_str:
                in_str = None
            continue
        if c == "'" or c == '"':
            in_str = c
            buf.append(c)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            args.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
    if buf:
        args.append("".join(buf).strip())
    return [a for a in args if a != ""]


_NUM_RE = re.compile(r"-?(?:\d+\.?\d*|\.\d+)$")
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*$")


def _unquote(lit: str) -> Optional[str]:
    lit = lit.strip()
    if len(lit) >= 2 and lit[0] in ("'", '"') and lit[-1] == lit[0]:
        return (lit[1:-1]
                .replace("\\/", "/").replace('\\"', '"').replace("\\'", "'"))
    return None


def _nuxt_env(html: str) -> Dict[str, str]:
    """param name → argument literal for the `window.__NUXT__` IIFE.

    Nuxt 2 minifies the state into `(function(a,b,c,…){return {…}}(1,0,"x",…))`.
    Every repeated string/number becomes a positional parameter, so the arg list
    *is* the alias table.
    """
    key = "window.__NUXT__="
    k = html.find(key)
    if k < 0:
        return {}
    blob = _match_bracket(html, k + len(key), "(", ")")
    head = re.match(r"^\(function\((.*?)\)\s*\{", blob, re.S)
    if not head:
        return {}
    names = [n.split("=")[0].strip() for n in head.group(1).split(",")]
    t = blob.rfind("}(")
    if t < 0:
        return {}
    call = _match_bracket(blob, t + 1, "(", ")")  # the "(args)" invocation
    if not call:
        return {}
    return dict(zip(names, _split_args(call[1:-1])))


def _resolve_token(tok: str, env: Dict[str, str], depth: int = 0) -> Any:
    """One array element → str / float / None (alias chains are followed)."""
    tok = tok.strip()
    s = _unquote(tok)
    if s is not None:
        return s
    if _NUM_RE.match(tok):
        try:
            return float(tok)
        except ValueError:
            return None
    if depth < 8 and _IDENT_RE.match(tok) and tok in env:
        return _resolve_token(env[tok], env, depth + 1)
    return None


_PAIR_RE = re.compile(r"\[\s*([^\[\],]+?)\s*,\s*([^\[\],]+?)\s*\]")


def extract_nuxt_series(html: str, indicator_id: str,
                        env: Optional[Dict[str, str]] = None) -> List[Tuple[date, float]]:
    """Full (date, value) series from the `datas:{"<id>":[…]}` payload array."""
    if not html or not indicator_id:
        return []
    env = env if env is not None else _nuxt_env(html)
    m = re.search(r'"%s"\s*:\s*\[' % re.escape(str(indicator_id)), html)
    if not m:
        logger.warning(f"GuruFocus payload has no datas array for id {indicator_id}")
        return []
    arr = _match_bracket(html, html.index("[", m.end() - 1), "[", "]")
    rows: List[Tuple[date, float]] = []
    skipped = 0
    for dm, vm in _PAIR_RE.findall(arr):
        dt_s = _resolve_token(dm, env)
        val = _resolve_token(vm, env)
        if not isinstance(dt_s, str) or not isinstance(val, float):
            skipped += 1
            continue
        try:
            rows.append((datetime.strptime(dt_s, "%Y-%m-%d").date(), val))
        except ValueError:
            skipped += 1
    if skipped:
        logger.info(f"   decoder skipped {skipped} non-series entries")
    # The payload is chronological, but never trust that: sort + de-duplicate so
    # a repeated timestamp cannot create two DB rows for one date.
    dedup: Dict[date, float] = {}
    for dt, val in rows:
        dedup[dt] = val
    return sorted(dedup.items())


# ── Fallback: the rendered HTML table ─────────────────────────────────────────

def parse_history_table(html: str) -> List[Dict[str, Any]]:
    """Read the visible "Historical Data" table (single page, no pagination).

    Only used when the payload array is absent. Values keep the yoy column when
    the table exposes one.
    """
    soup = BeautifulSoup(html, "lxml")
    target = None
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if any("date" in h for h in headers) and len(headers) >= 2:
            target = table
            break
    if target is None:
        for table in soup.find_all("table"):
            first = table.find("tr")
            if first and any("date" in c.get_text(strip=True).lower()
                             for c in first.find_all(["td", "th"])):
                target = table
                break
    if target is None:
        return []

    out: List[Dict[str, Any]] = []
    body = target.find("tbody")
    for row in (body.find_all("tr") if body else target.find_all("tr")):
        cols = row.find_all("td")
        if len(cols) < 2:
            continue
        date_str = cols[0].get_text(strip=True)
        val_str = cols[1].get_text(strip=True).replace("%", "").replace(",", "")
        yoy = None
        if len(cols) >= 3:
            try:
                yoy = float(cols[2].get_text(strip=True)
                            .replace("%", "").replace("+", ""))
            except ValueError:
                yoy = None
        try:
            out.append({"date": datetime.strptime(date_str, "%Y-%m-%d").date(),
                        "value": float(val_str), "yoy": yoy})
        except ValueError:
            continue
    return out


# ── Transport ─────────────────────────────────────────────────────────────────

def _curl_cffi_get(url: str, referer: Optional[str] = None,
                   timeout: int = 30) -> Optional[FetchResult]:
    """Fetch `url` with curl_cffi's Chrome TLS fingerprint — WITHOUT Scrapling.

    GuruFocus 403s plain `requests` at the TLS-fingerprint layer, but a bare
    ``curl_cffi.requests.get(url, impersonate="chrome120")`` is served 200 + the
    full Nuxt payload (measured 2026-11-01: 3,684,418 chars, no interstitial).
    curl_cffi installs as a scrapling dependency, so it survives in an image
    where `scrapling` itself failed to import — exactly the runtime where
    `http_get`/`browser_get` short-circuit to None and the scraper used to drop
    straight to the 403-ing `requests` path. This is the first fallback, and it
    needs no browser.

    The WAF challenge is TRANSIENT (measured 2026-11-01: the same URL answered
    403 + a 6,129-byte interstitial on one hit and 200 + 3.6 MB on the next, a
    minute apart, from the same IP), and `scrape_gurufocus_indicator` makes only
    ONE call here — so a single block would otherwise fail the whole run. Mirrors
    `http_get`'s adaptive rule: rotate the fingerprints, then one bounded
    pause-and-retry when the site ANSWERED (any status). A connection error that
    never got a response is not retried.

    Returns the same `FetchResult` shape as the Scrapling helpers so `_page_html`
    guards the series identically; None when every attempt failed.
    """
    try:
        from curl_cffi import requests as curl_requests
    except Exception as e:  # curl_cffi absent — let the caller reach `requests`
        logger.warning(f"curl_cffi transport unavailable: "
                       f"{type(e).__name__}: {str(e)[:120]}")
        return None

    attempts: List[str] = []

    def pass_once(imp: str) -> Optional[FetchResult]:
        kwargs: Dict[str, Any] = {"impersonate": imp, "timeout": timeout,
                                  "allow_redirects": True}
        if referer:
            kwargs["headers"] = {"Referer": referer}
        try:
            resp = curl_requests.get(url, **kwargs)
        except Exception as e:
            attempts.append(f"{imp}: raised {type(e).__name__}")
            logger.warning(f"[curl_cffi] {imp} request failed: "
                           f"{type(e).__name__}: {str(e)[:120]}")
            return None
        text = resp.text or ""
        status = int(getattr(resp, "status_code", 0) or 0)
        final = str(getattr(resp, "url", url))
        if status == 200 and text and not looks_blocked(text):
            logger.info(f"[curl_cffi] 200 via {imp}: {len(text):,} chars <- {final[-70:]}")
            return FetchResult(text=text, status=status, url=final,
                               via=f"curl_cffi:{imp}")
        attempts.append(f"{imp}: {status}/{len(text):,}b")
        return None

    for imp in _CURL_IMPERSONATIONS:
        result = pass_once(imp)
        if result:
            return result

    # Retry only when the site actually answered (a block, not a socket failure):
    # hammering a challenged IP back-to-back deepens the challenge.
    answered = [a for a in attempts if ": raised " not in a]
    if answered and RETRY_PAUSE_S > 0:
        logger.info(f"[curl_cffi] blocked by the site ({'  '.join(attempts)}) — "
                    f"one retry in {RETRY_PAUSE_S:g}s")
        time.sleep(RETRY_PAUSE_S)
        for imp in _CURL_IMPERSONATIONS:
            result = pass_once(imp)
            if result:
                return result

    logger.warning(f"[curl_cffi] no usable response for {url[-70:]}: "
                   f"{'; '.join(attempts) or 'no fingerprint ran'}")
    return None


def _page_html(url: str, indicator_code: str) -> Tuple[Optional[str], str]:
    """(HTML, why-not) for the indicator page — cheapest transport first, series-guarded.

    The second value exists because "no usable GuruFocus page" was unreadable for
    hours: a 403 block, a wrong-series redirect and an empty body all looked the
    same in job_runs. The caller puts it straight into the failure message.
    """
    must = SERIES_TITLE_TOKENS.get(indicator_code.upper(), ())
    candidates: List[Optional[Any]] = [
        lambda: http_get(url, referer="https://www.gurufocus.com/"),
        lambda: _curl_cffi_get(url, referer="https://www.gurufocus.com/"),
        lambda: browser_get(url, wait_selector="table"),
    ]
    tried: List[str] = []
    for name, getter in zip(("http", "curl_cffi", "browser"), candidates):
        try:
            res = getter()
        except Exception as e:  # a transport blowing up must not kill the job
            tried.append(f"{name} raised {type(e).__name__}")
            logger.warning(f"{indicator_code}: {name} transport error "
                           f"{type(e).__name__}: {e}")
            continue
        if res is None or not res.text:
            tried.append(f"{name}: no usable response")
            continue
        if must and not source_is_expected(res.text, must, url=res.url):
            detail = (f"{name} answered {res.status} but the page is not {must} "
                      f"(title={page_title(res.text)[:70]!r})")
            logger.error(
                f"{indicator_code}: GuruFocus served a DIFFERENT series — refusing "
                f"to parse. {detail} url={res.url[-60:]!r}")
            return None, detail
        logger.info(f"{indicator_code}: {len(res.text):,} chars via {res.via}")
        return res.text, ""

    # Absolute last resort. curl_cffi above already covers an image WITHOUT
    # scrapling (it is a scrapling dependency, so it is present even when
    # scrapling fails to import), and GuruFocus 403s plain `requests` by TLS
    # fingerprint — so this only runs if curl_cffi is missing or blocked too.
    try:
        import requests
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://www.gurufocus.com/",
        })
        resp.raise_for_status()
        if must and not source_is_expected(resp.text, must, url=url):
            logger.error(f"{indicator_code}: requests fallback served the wrong series")
            return None, "requests fallback served the wrong series"
        return resp.text, ""
    except Exception as e:
        tried.append(f"requests: {type(e).__name__}: {str(e)[:90]}")
        logger.warning(f"{indicator_code}: all transports failed — {' | '.join(tried)}")
        return None, "all transports failed — " + " | ".join(tried)


# ── Main entry point ──────────────────────────────────────────────────────────

def scrape_gurufocus_indicator(url: str, indicator_code: str,
                               mode: str = "incremental",
                               max_pages: Optional[int] = None) -> dict:
    """Scrape one GuruFocus series into economic_indicators.

    Returns {"ok": bool, "rows": n_written, "inserted": n, "yoy_updated": n}
    for job_tracking.

    mode:
      - "incremental": re-upsert only the window from (latest DB date −
        LOOKBACK_BUFFER_DAYS) forwards. Because one HTTP request already carries
        the whole series, coverage of a gap is automatic — there is nothing to
        "page through", and any missed schedule is backfilled on the next run.
      - "full": upsert every historical point the page carries.

    `max_pages` is retained for call-site compatibility (manual routes pass 3).
    It is ignored by the payload decoder and only caps the legacy table fallback.
    """
    logger.info(f"🚀 GuruFocus scraper: {indicator_code} (mode={mode}) <- {url}")

    sid = series_id_from_url(url)
    if not sid:
        return _guru_fail(f"could not read a GuruFocus series id from {url!r}")

    html, why = _page_html(url, indicator_code)
    if html is None:
        return _guru_fail(f"no usable GuruFocus page for {indicator_code}: {why}")

    series = extract_nuxt_series(html, sid)
    if series:
        records = [{"date": dt, "value": v, "yoy": None} for dt, v in series]
        logger.info(f"✅ {indicator_code}: {len(records)} points decoded from the "
                    f"Nuxt payload ({records[0]['date']} … {records[-1]['date']})")
    else:
        logger.warning(f"⚠️ {indicator_code}: payload decode empty — falling back "
                       f"to the rendered table")
        records = parse_history_table(html)
        if max_pages:
            records = records[:max_pages * 20]

    if not records:
        return _guru_fail(f"GuruFocus page for {indicator_code} yielded no data points")

    db = SessionLocal()
    try:
        existing_records = {
            r.report_date: r for r in db.query(EconomicIndicator)
            .filter(EconomicIndicator.indicator_code == indicator_code).all()
        }

        # Smart Incremental Backfill: start just before the newest stored date so
        # official revisions and any scheduling gap are re-covered, never from a
        # hardcoded page count.
        if mode == "incremental" and existing_records:
            latest = max(existing_records)
            cutoff = latest - timedelta(days=LOOKBACK_BUFFER_DAYS)
            before = len(records)
            records = [r for r in records if r["date"] >= cutoff]
            logger.info(f"   📅 Latest in DB {latest}; re-upserting from {cutoff} "
                        f"({before} → {len(records)} points)")
            if not records:
                return {"ok": True, "rows": 0, "skipped": "already up to date"}
        elif mode == "incremental":
            logger.info(f"   📅 No {indicator_code} rows in DB yet — writing the "
                        f"full decoded series ({len(records)} points)")

        # YOY needs last-year context, which the incremental window usually does
        # not contain → build the lookup from DB + this scrape (spec: no regressions).
        full_history_lookup: Dict[date, Optional[float]] = {
            dt: _num(r.value) for dt, r in existing_records.items()}
        for record in records:
            full_history_lookup[record["date"]] = record["value"]

        new_objs = []
        updated_count = 0

        for record in records:
            dt = record["date"]
            current_value = record["value"]
            yoy_val = record.get("yoy")

            if yoy_val is None:
                try:
                    target_date = dt.replace(year=dt.year - 1)
                except ValueError:
                    target_date = dt.replace(year=dt.year - 1, day=28)

                last_year_value = None
                for i in range(10):  # holidays/weekends: look back up to 10 days
                    check_date = target_date - timedelta(days=i)
                    if check_date in full_history_lookup:
                        last_year_value = full_history_lookup[check_date]
                        break

                if last_year_value and last_year_value != 0:
                    yoy_val = round(
                        ((current_value - last_year_value) / last_year_value) * 100, 2)

            if dt in existing_records:
                existing = existing_records[dt]
                changed = False
                if _num(existing.value) != current_value:
                    existing.value = current_value
                    changed = True
                if yoy_val is not None and _num(existing.yoy_pct) != yoy_val:
                    existing.yoy_pct = yoy_val
                    changed = True
                if changed:
                    updated_count += 1
            else:
                new_objs.append(EconomicIndicator(
                    report_date=dt,
                    indicator_code=indicator_code,
                    value=current_value,
                    yoy_pct=yoy_val,
                ))

        if new_objs:
            db.bulk_save_objects(new_objs)

        db.commit()
        logger.info(f"💾 {indicator_code}: inserted {len(new_objs)} new, "
                    f"touched {updated_count} existing")
        return {"ok": True, "rows": len(new_objs) + updated_count,
                "inserted": len(new_objs), "yoy_updated": updated_count}

    except Exception as e:
        db.rollback()
        logger.error(f"❌ GuruFocus scraper failed for {indicator_code}: {e}",
                     exc_info=True)
        return _guru_fail(str(e))
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")
    parser = argparse.ArgumentParser(description="GuruFocus Indicator Scraper")
    parser.add_argument("--url", required=True, help="GuruFocus indicator URL")
    parser.add_argument("--code", required=True, help="Indicator code (e.g. SP500_EY)")
    parser.add_argument("--mode", default="incremental",
                        choices=["incremental", "full"],
                        help="incremental: lookback window | full: entire history")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="legacy table fallback only (ignored by the decoder)")
    args = parser.parse_args()
    # Record in job_runs like the Admin/Run-Now path does (same key, same
    # dict-failure contract) — a terminal run that is invisible to the ledger
    # forces the Runbook to judge this card "from tables" instead of real.
    from app.services.job_tracking import track_job, with_result_handling
    with track_job("manual:gurufocus", "manual") as run:
        out = with_result_handling(run, scrape_gurufocus_indicator(
            url=args.url, indicator_code=args.code,
            mode=args.mode, max_pages=args.max_pages))
    print(out)
    raise SystemExit(0 if out.get("ok") else 1)
