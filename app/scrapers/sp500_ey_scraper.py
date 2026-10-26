"""
SP500 Earnings Yield Scraper
Fetches the S&P 500 Earnings Yield (EY) and stores it as indicator_code='SP500_EY'.

Primary source : GuruFocus. The page ships its FULL daily history inside the
                 Nuxt payload (`datas:{"151":[["YYYY-MM-DD",v],…]}`), decoded in
                 pure Python by app.scrapers.gurufocus_scraper — one HTTP GET
                 with a browser TLS fingerprint yields ~17.5k points, 1957→today.
                 That turns this scraper from a one-value snapshot into a
                 gap-closing series (Smart Incremental Backfill).
Fallback 1     : the page's headline value only (same HTML, current figure)
Fallback 2     : Yahoo Finance PE → EY = 1 / PE (cookie+crumb handshake — plain
                 quoteSummary calls return 401 since Yahoo enabled crumb auth)
Fallback 3     : our own sp500_history.pe_ratio (Multpl monthly) → EY = 100 / PE
"""

import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup
from sqlalchemy import func

from app.core.database import SessionLocal
from app.models.economic_indicators import EconomicIndicator
from app.scrapers.gurufocus_scraper import extract_nuxt_series, series_id_from_url
from app.scrapers.scrapling_fetch import http_get, page_title, source_is_expected

logger = logging.getLogger(__name__)

INDICATOR_CODE = "SP500_EY"

# How far behind the newest stored date a normal run re-covers. GuruFocus
# restates recent sessions, and the day-10 / monthly cron can be missed; the
# window means the next run patches both. Gaps LONGER than this are covered too
# — see _backfill_window_start(), which widens the window to the newest row.
EY_LOOKBACK_DAYS = int(os.getenv("SP500_EY_LOOKBACK_DAYS", "90"))
# First-run ceiling: the payload goes back to 1957 (~17.5k points), but seeding
# that into economic_indicators from a scheduled job is a surprise write storm.
# 10 years (~2.5k daily points) is plenty for the charts; run the module with
# --full-history (or raise this env) to seed deeper.
EY_INITIAL_BACKFILL_YEARS = int(os.getenv("SP500_EY_INITIAL_BACKFILL_YEARS", "10"))
# Series sanity bounds: reject junk a single-value parse could also reject, but
# keep the historical extremes (measured range on the live page: 0.761–15.425).
EY_PLAUSIBLE_MIN, EY_PLAUSIBLE_MAX = 0.05, 60.0

# 2026-10-23 DATA-INTEGRITY FIX. This pointed at economic_indicators/1031/, which
# answers HTTP 200 but redirects to 1031/quits-total-nonfarm — "Quits: Total
# Nonfarm: 1.9". 1.9 clears the 0.5–15% sanity window below, so a successful
# scrape silently stored a labour statistic as the S&P 500 Earnings Yield. The
# correct series id is 151 (verified: "S&P 500 Earnings Yield: 3.861% (Oct 2026)"),
# the same id app/core/scheduler.py:_GURUFOCUS_URLS and the admin endpoints use.
# TITLE_TOKENS is the guard that makes a future redirect impossible to miss.
GURUFOCUS_URL = (
    "https://www.gurufocus.com/economic_indicators/151/sp-500-earnings-yield"
)
TITLE_TOKENS = ("earnings yield",)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.gurufocus.com/",
}


# ── Source 1: GuruFocus ───────────────────────────────────────────────────────

def _page_html() -> Optional[str]:
    """HTML of the EY page, Scrapling first (TLS impersonation cleared the
    Gurufocus bot-check where plain requests needed Selenium), requests as the
    fallback so the scraper still runs if scrapling is not installed."""
    res = http_get(GURUFOCUS_URL, referer="https://www.gurufocus.com/")
    if res is not None:
        # Never trust a 200: confirm the page is about Earnings Yield.
        if not source_is_expected(res.text, TITLE_TOKENS, url=res.url):
            logger.error(
                f"GuruFocus answered 200 but is NOT the EY series "
                f"(title={page_title(res.text)[:70]!r} url={res.url[-60:]!r}) "
                f"— refusing to parse, falling through to Yahoo/DB")
            return None
        return res.text

    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        resp = session.get(GURUFOCUS_URL, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning(f"GuruFocus fetch failed (requests path): {e}")
        return None


def _fetch_from_gurufocus() -> Optional[float]:
    """
    Scrape the latest S&P 500 Earnings Yield from GuruFocus.
    Returns the value as a percentage (e.g. 3.861 for 3.861%).
    """
    try:
        html = _page_html()
        if html is None:
            return None

        soup = BeautifulSoup(html, "lxml")

        # Look for specific structural elements that hold the value
        main_value_text = None
        
        # 1. Look for a span with a class related to value or current indicator
        current_span = soup.select_one("span.currentValue, h1 + span, .indicator-value")
        if current_span:
            main_value_text = current_span.get_text(strip=True)
        else:
            # 2. Fallback to a table row labeled 'Current Value'
            for tr in soup.find_all("tr"):
                if "Current Value" in tr.get_text():
                    tds = tr.find_all("td")
                    if len(tds) >= 2:
                        main_value_text = tds[1].get_text(strip=True)
                        break

        if main_value_text and "%" in main_value_text:
            clean = main_value_text.replace("%", "").replace(",", "").strip()
            try:
                val = float(clean)
                if 0.5 < val < 15:
                    logger.info(f"GuruFocus SP500_EY: {val}%")
                    return val   # Standardized: store as percentage e.g. 4.107% matching DB history
                logger.warning(f"GuruFocus EY {val} outside 0.5–15% sanity window — rejecting")
            except ValueError:
                pass

        logger.warning("GuruFocus: could not parse earnings yield from page")
        return None

    except Exception as e:
        logger.warning(f"GuruFocus fetch failed: {e}")
        return None


# ── Source 1b: the same page's FULL history (preferred over one snapshot) ────

def _fetch_series_from_gurufocus() -> list:
    """[(date, pct)] for the whole EY series, [] when the payload is unavailable.

    Decoding the Nuxt payload costs the same single HTTP request as reading the
    headline number, but it also closes any date the DB is missing.
    """
    html = _page_html()
    if not html:
        return []
    sid = series_id_from_url(GURUFOCUS_URL)
    rows = extract_nuxt_series(html, sid) if sid else []
    clean = [(d, v) for d, v in rows
             if EY_PLAUSIBLE_MIN < v < EY_PLAUSIBLE_MAX]
    dropped = len(rows) - len(clean)
    if dropped:
        logger.warning(f"GuruFocus EY series: dropped {dropped} implausible values "
                       f"(outside {EY_PLAUSIBLE_MIN}–{EY_PLAUSIBLE_MAX}%)")
    if clean:
        logger.info(f"GuruFocus EY series decoded: {len(clean)} points "
                    f"({clean[0][0]} … {clean[-1][0]}, last={clean[-1][1]}%)")
    return clean


def _last_ey_date(db) -> Optional[date]:
    return (db.query(func.max(EconomicIndicator.report_date))
              .filter(EconomicIndicator.indicator_code == INDICATOR_CODE)
              .scalar())


def _backfill_window_start(db) -> date:
    """Routine window, widened to the newest stored date when the DB is behind,
    and bounded by EY_INITIAL_BACKFILL_YEARS when the table is empty. This is
    what makes "run it whenever" safe: a two-month outage backfills two months,
    not 90 days."""
    last = _last_ey_date(db)
    if last is None:
        seed_from = date.today().replace(year=date.today().year - EY_INITIAL_BACKFILL_YEARS)
        logger.info(f"No SP500_EY rows in DB — seeding from {seed_from} "
                    f"({EY_INITIAL_BACKFILL_YEARS}y; raise "
                    f"SP500_EY_INITIAL_BACKFILL_YEARS for deeper history)")
        return seed_from
    routine = date.today() - timedelta(days=EY_LOOKBACK_DAYS)
    if last < routine:
        logger.warning(f"SP500_EY is stale (newest {last}) — backfilling from {last}")
        return last
    return routine


def _upsert_ey_series(rows: list) -> int:
    """Atomic upsert on the (report_date, indicator_code) composite key.

    Chunked because a first run writes ~17.5k rows and a single VALUES list that
    large is close to Postgres' 65k-parameter ceiling.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    if not rows:
        return 0
    db = SessionLocal()
    written = 0
    try:
        for i in range(0, len(rows), 1000):
            chunk = [{
                "report_date": d,
                "indicator_code": INDICATOR_CODE,
                "value": float(v),
            } for d, v in rows[i:i + 1000]]
            stmt = pg_insert(EconomicIndicator).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["report_date", "indicator_code"],
                set_={"value": stmt.excluded.value},
            )
            db.execute(stmt)
            written += len(chunk)
        db.commit()
        logger.info(f"SP500_EY: upserted {written} points")
        return written
    except Exception as e:
        db.rollback()
        logger.error(f"DB error upserting SP500_EY series: {e}")
        return 0
    finally:
        db.close()


# ── Source 2: Yahoo Finance (fallback via P/E) ────────────────────────────────

def _fetch_from_yahoo() -> Optional[float]:
    """
    Fetch S&P 500 trailing P/E from Yahoo Finance and derive EY = 1 / PE.
    Uses the ^GSPC summary endpoint. quoteSummary has required a cookie+crumb
    pair since 2023 — a plain GET returns 401 (seen live on Render 2026-10-13),
    so we first warm a session cookie and fetch the crumb, then retry with it.
    """
    url = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/%5EGSPC"
    params = {"modules": "summaryDetail,defaultKeyStatistics"}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    try:
        session = requests.Session()
        session.headers.update(headers)
        # fc.yahoo.com 404s on purpose but still sets the A3 cookie.
        try:
            session.get("https://fc.yahoo.com", timeout=10)
        except requests.RequestException:
            pass
        crumb = ""
        try:
            r = session.get(
                "https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10)
            text = r.text.strip()
            if r.ok and text and "<" not in text:
                crumb = text
        except requests.RequestException:
            pass

        resp = session.get(url, params={**params, "crumb": crumb} if crumb
                           else params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        result = data["quoteSummary"]["result"][0]
        trailing_pe = (
            result.get("summaryDetail", {})
                  .get("trailingPE", {})
                  .get("raw")
        )

        if trailing_pe and trailing_pe > 0:
            ey_pct = (1.0 / trailing_pe) * 100.0
            logger.info(f"Yahoo Finance PE={trailing_pe:.2f} → EY={ey_pct:.2f}%")
            return ey_pct

    except Exception as e:
        logger.warning(f"Yahoo Finance EY fallback failed: {e}")

    return None


# ── Source 3: our own DB (Multpl PE already lives in sp500_history) ─────────

def _derive_from_db_pe() -> Optional[float]:
    """Last-resort, no-network source: EY = 100 / latest non-null pe_ratio in
    sp500_history (kept monthly by sp500_pe_scraper). Sanity window 0.5–15%
    mirrors the GuruFocus parser so a corrupt PE cannot poison the series."""
    from app.models.economic_indicators import SP500History

    db = SessionLocal()
    try:
        row = (
            db.query(SP500History.trade_date, SP500History.pe_ratio)
              .filter(SP500History.pe_ratio.isnot(None))
              .order_by(SP500History.trade_date.desc())
              .first()
        )
        if row is None or not row.pe_ratio or float(row.pe_ratio) <= 0:
            return None
        ey_pct = 100.0 / float(row.pe_ratio)
        if 0.5 < ey_pct < 15:
            logger.info(
                f"Derived SP500_EY={ey_pct:.3f}% from own DB "
                f"(PE={float(row.pe_ratio):.2f} @ {row.trade_date})")
            return ey_pct
        logger.warning(f"DB PE implies out-of-range EY {ey_pct:.3f}% — skipping")
    except Exception as e:
        db.rollback()
        logger.warning(f"DB-derived EY fallback failed: {e}")
    finally:
        db.close()
    return None


# ── Storage ───────────────────────────────────────────────────────────────────

def _save_sp500_ey(value: float, report_date: date) -> bool:
    db = SessionLocal()
    try:
        existing = (
            db.query(EconomicIndicator)
              .filter(
                  EconomicIndicator.indicator_code == INDICATOR_CODE,
                  EconomicIndicator.report_date == report_date,
              )
              .first()
        )

        if existing:
            existing.value = value
            logger.info(f"Updated SP500_EY for {report_date}: {value:.4f}")
        else:
            db.add(
                EconomicIndicator(
                    report_date=report_date,
                    indicator_code=INDICATOR_CODE,
                    value=value,
                )
            )
            logger.info(f"Inserted SP500_EY for {report_date}: {value:.4f}")

        db.commit()
        return True

    except Exception as e:
        db.rollback()
        logger.error(f"DB error saving SP500_EY: {e}")
        return False
    finally:
        db.close()


# ── Main entry point ──────────────────────────────────────────────────────────

def scrape_sp500_earnings_yield(force: bool = False, full_history: bool = False) -> dict:
    """
    Store the S&P 500 Earnings Yield.

    Preferred path: decode GuruFocus's full history and upsert the window from
    (newest stored date − EY_LOOKBACK_DAYS) to today — so a run at any time
    closes gaps and picks up revisions instead of writing a single row.
    full_history=True ignores any window and writes every decoded point (the
    deeper backfill a first run deliberately does not do).
    Only when that series is unavailable does it fall back to the point-in-time
    chain (headline value → Yahoo → own DB PE), which keeps the old
    "already scraped today" skip.
    Returns {"ok": bool, "rows": n_written} for job_tracking.
    """
    today = date.today()

    series = _fetch_series_from_gurufocus()
    if series:
        db = SessionLocal()
        try:
            start = date.min if full_history else _backfill_window_start(db)
        finally:
            db.close()
        window = [r for r in series if r[0] >= start]
        written = _upsert_ey_series(window)
        if written:
            return {"ok": True, "rows": written,
                    "window_from": str(start), "series_last": str(series[-1][0])}
        logger.warning("EY series decoded but the upsert failed — "
                       "falling back to the single-value chain")

    if not force:
        db = SessionLocal()
        try:
            exists = (
                db.query(EconomicIndicator)
                  .filter(
                      EconomicIndicator.indicator_code == INDICATOR_CODE,
                      EconomicIndicator.report_date == today,
                  )
                  .first()
            )
            if exists:
                logger.info(f"SP500_EY already scraped today ({today}). Use force=True to re-fetch.")
                return {"ok": True, "rows": 0, "skipped": "already scraped today"}
        finally:
            db.close()

    # Source chain: GuruFocus → Yahoo (cookie+crumb) → own-DB PE
    ey_value = _fetch_from_gurufocus()
    if ey_value is None:
        logger.info("Falling back to Yahoo Finance for SP500_EY...")
        time.sleep(1)
        ey_value = _fetch_from_yahoo()
    if ey_value is None:
        logger.info("Yahoo failed too — deriving SP500_EY from own sp500_history PE...")
        ey_value = _derive_from_db_pe()

    if ey_value is None:
        logger.error("❌ Failed to fetch SP500_EY from all sources")
        return {"ok": False, "rows": 0, "stage_failures": [
            {"stage": "sp500_ey_guru",
             "error": "GuruFocus returned 403/bot-blocked or unparseable"},
            {"stage": "sp500_ey_yahoo",
             "error": "Yahoo quoteSummary failed (401 crumb auth or no PE)"},
            {"stage": "sp500_ey_db",
             "error": "No usable pe_ratio in sp500_history to derive EY"}]} 

    saved = _save_sp500_ey(ey_value, today)
    if not saved:
        return {"ok": False, "rows": 0, "stage_failures": [
            {"stage": "sp500_ey",
             "error": "EY value fetched but the DB save failed (see log)"}]}
    return {"ok": True, "rows": 1}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import argparse
    parser = argparse.ArgumentParser(description="SP500 Earnings Yield Scraper")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if today already has data")
    parser.add_argument("--full-history", action="store_true",
                        help="Upsert every decoded GuruFocus point (1957 → today)")
    args = parser.parse_args()
    ok = scrape_sp500_earnings_yield(force=args.force, full_history=args.full_history)
    sys.exit(0 if ok.get("ok") else 1)
