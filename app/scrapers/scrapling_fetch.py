"""
scrapling_fetch.py — shared Scrapling fetch layer for every scraper.

Why this exists
---------------
Before adopting Scrapling we measured each blocked target with read-only
probes (2026-10-23, local residential IP). The measurements — not guesses —
shaped these rules:

1. `Fetcher` (curl_cffi TLS fingerprint) beats plain `requests` on
   gurufocus.com (200 + full `__NUXT__` payload, so Selenium is unnecessary)
   and on the investing.com futures page for most fingerprints.
2. Fingerprint choice is NOT cosmetic: for the same investing.com URL
   `chrome131` returned 403 while `chrome120 / chrome / safari180 /
   firefox135 / edge101` all returned 200. Hence the rotation list below —
   and why chrome131 is deliberately excluded.
3. The two fetchers do not share timeout units. `Fetcher.get(timeout=…)` is
   SECONDS; `StealthyFetcher.fetch(timeout=…)` is MILLISECONDS (passing 60
   means 60 ms and every attempt "times out" instantly). Callers here pass
   seconds everywhere and this module converts for the browser.
4. `network_idle=True` never settles on investing.com (ads/telemetry keep the
   socket busy) → it stayed off; `wait_selector` is used instead.
5. sama.gov.sa fails curl_cffi's certificate validation
   (`CertificateVerifyError: unable to get local issuer certificate`) because
   curl_cffi ships its own CA store. This module NEVER disables verification
   (that would be a MITM hole). SAMA therefore goes through the browser
   (real Chrome uses the OS trust store) or through plain `requests`, which
   validates the chain fine today.

Graceful degradation
--------------------
If `scrapling` is not importable (e.g. an image built before the dependency
landed), every function here returns None and callers fall back to their
existing `requests`/browser path. A missing optional dependency must never
turn a working scraper into an ImportError.

Proxy support
-------------
Anti-bot blocks come in two layers: the fingerprint (handled by impersonation
above) and the IP reputation. A datacenter IP cannot be fingerprinted out of a
block, so the IP itself has to change — that is what `resolve_proxy()` and the
`proxy` / `proxy_scope` arguments on the fetchers are for. Resolution is
opt-in per call site (pass `proxy_scope="EURODOLLAR"`, say) so a stray
`HTTPS_PROXY` in an image can never silently re-route an unrelated scraper.
Credentials are never logged — see `_mask_proxy()`.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

try:  # optional dependency — see "Graceful degradation" above
    from scrapling.fetchers import Fetcher, StealthyFetcher, StealthySession
    HAVE_SCRAPLING = True
except Exception as _e:  # ImportError, or a broken browser install
    Fetcher = None
    StealthyFetcher = None
    StealthySession = None
    HAVE_SCRAPLING = False
    logger.info(f"Scrapling unavailable ({_e}); scrapers use their legacy path")


# Measured: chrome131 got 403 on investing.com where everything else passed.
IMPERSONATE_ORDER = [
    t.strip() for t in os.getenv(
        "SCRAPLING_IMPERSONATE_ORDER",
        "chrome120,chrome,safari180,firefox135,edge101",
    ).split(",") if t.strip()
]

# Real Chrome is already installed in the backend image (Dockerfile
# google-chrome-stable) and on this dev box; using it means `scrapling
# install` (a several-hundred-MB browser download) is NOT needed.
_CHROME_ENV_KEYS = ("GOOGLE_CHROME_BIN", "CHROME_BIN")

# Browser navigation budget, in the MILLISECONDS the browser fetcher expects.
DEFAULT_BROWSER_TIMEOUT_MS = int(os.getenv("SCRAPLING_BROWSER_TIMEOUT_MS", "60000"))

# Adaptive (self-relocating) selectors: Scrapling remembers an element by a hash
# under an `identifier` and, when the site renames its classes, relocates the
# element instead of returning nothing. Measured on Scrapling 0.5: it is a
# PARSER-level switch (`Fetcher.configure(adaptive=…)` / per-call
# `css(sel, identifier=…, adaptive=True, auto_save=True)`), NOT a fetch kwarg,
# and `auto_save` is ignored unless adaptive was enabled up front. It persists
# hashes through `storage`, which needs a writable directory — so this stays
# opt-in and any failure degrades to plain selectors rather than failing the
# scrape (verified: 5 of 6 investing.com/gurufocus targets were located by
# header text anyway, so adaptivity is a bonus, never a dependency).
ADAPTIVE_ENABLED = os.getenv("SCRAPLING_ADAPTIVE", "0") == "1"
_adaptive_done = False

_BLOCK_MARKERS = (
    r"cf-browser-verification", r"Just a moment", r"Attention Required",
    r"Checking your browser", r"Access Denied", r"Error 403",
    r"<title>[^<]*[Dd]enied",
)
_BLOCK_RE = re.compile("|".join(_BLOCK_MARKERS), re.I)

# Soft markers: words that also appear inside a NORMAL page's markup. Measured
# 2026-10-23 — saudiexchange.sa's rendered <head> loads a gstatic reCAPTCHA
# script, so a bare `captcha` match made browser_get discard a perfectly good
# 200 / 1,099,712-char page and push the sukuk scraper toward its seed fallback.
# They only mean "blocked" on a small body, i.e. an actual interstitial.
_SOFT_MARKERS = (r"captcha", r"verify you are human", r"are you a robot",
                 r"cf-chl", r"challenge-platform")
_SOFT_RE = re.compile("|".join(_SOFT_MARKERS), re.I)
_SOFT_BLOCK_MAX_LEN = int(os.getenv("SCRAPLING_SOFT_BLOCK_MAX_LEN", "20000"))


# ─── Proxy resolution ─────────────────────────────────────────────────────────
# Generic keys, least specific last. `<SCOPE>_PROXY` (checked by resolve_proxy)
# always wins over these, so one scraper can be re-proxied without touching
# the others.
PROXY_ENV_KEYS = ("SCRAPLING_PROXY", "HTTPS_PROXY", "ALL_PROXY", "HTTP_PROXY")


def resolve_proxy(scope: Optional[str] = None) -> Optional[str]:
    """Proxy URL from the environment, most specific name first.

    `scope="EURODOLLAR"` checks `EURODOLLAR_PROXY` → `SCRAPLING_PROXY` →
    `HTTPS_PROXY` → `ALL_PROXY` → `HTTP_PROXY` and returns the first non-empty
    value, or None (meaning: run direct, exactly as before proxies existed).
    Accepted shape: `http://host:port` or `http://user:pass@host:port`
    (residential/ISP proxies are the only kind that survives an IP-reputation
    block; a datacenter proxy just moves the 403).
    """
    keys = ((f"{scope.upper()}_PROXY",) if scope else ()) + PROXY_ENV_KEYS
    for key in keys:
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return None


def _split_proxy(proxy: str) -> Tuple[str, Optional[Tuple[str, str]]]:
    """`http://u:p@host:port` -> (`http://host:port`, (`u`, `p`)).

    curl_cffi (the HTTP fetcher) wants the server and the credentials as two
    separate arguments, while Playwright wants one dict — so normalise here and
    let each fetcher re-shape it.
    """
    if not proxy or "://" not in proxy:
        return proxy, None
    scheme, rest = proxy.split("://", 1)
    if "@" not in rest:
        return proxy, None
    creds, hostport = rest.rsplit("@", 1)
    user, _, password = creds.partition(":")
    return f"{scheme}://{hostport}", (user, password)


def _mask_proxy(proxy: Optional[str]) -> str:
    """Proxy URL safe to put in a log line (password replaced by ****)."""
    if not proxy:
        return "direct"
    return re.sub(r"://([^/@:]+):[^@]+@", r"://\1:****@", proxy)


@dataclass
class FetchResult:
    """Uniform shape for both fetch paths so callers stay transport-agnostic.

    `page` is the raw Scrapling object when one exists, so a caller can run
    adaptive selectors over it; `text` alone is always enough for a bs4 parse.
    """
    text: str
    status: int
    url: str
    via: str = "scrapling"
    page: Any = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.text) and not looks_blocked(self.text)


def looks_blocked(text: str) -> bool:
    """True when the body is an anti-bot page rather than the real content.

    A 200 alone is NOT proof of success — investing.com serves a 200 block
    page, so callers must use `.ok` / this check.
    """
    if not text:
        return True
    # Only the first ~40 KB is scanned: a real page mentions nothing hostile
    # up front, and scanning 3 MB of GuruFocus JS for the word "captcha"
    # would false-positive on its own analytics bundle.
    head = text[:40_000]
    if _BLOCK_RE.search(head):
        return True
    # Soft words count only while the body is interstitial-sized (see above).
    if len(text) <= _SOFT_BLOCK_MAX_LEN and _SOFT_RE.search(head):
        return True
    return False


def _chrome_executable() -> Optional[str]:
    for key in _CHROME_ENV_KEYS:
        p = os.getenv(key)
        if p and os.path.exists(p):
            return p
    # Standard installation locations
    defaults = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for d in defaults:
        if os.path.exists(d):
            return d
    return None


def enable_adaptive() -> bool:
    """Turn on Scrapling's adaptive selectors globally, once, if asked to.

    Returns the effective state. Never raises: a broken storage backend must
    not take a working scraper down with it.
    """
    global _adaptive_done
    if not HAVE_SCRAPLING or not ADAPTIVE_ENABLED or _adaptive_done:
        return bool(_adaptive_done)
    try:
        Fetcher.configure(adaptive=True)
        StealthyFetcher.configure(adaptive=True)
        _adaptive_done = True
        logger.info("[scrapling] adaptive selectors enabled")
    except Exception as e:
        logger.warning(f"[scrapling] adaptive selectors unavailable: "
                       f"{type(e).__name__}: {str(e)[:140]}")
    return _adaptive_done


def adaptive_select(page, css: str, identifier: str, first: bool = True):
    """Query a fetched Scrapling page with relocation, degrading to plain css.

    `identifier` is the stable name the element's fingerprint is stored under;
    `auto_save` records it so a future DOM change can be relocated to.
    Returns a list of matched elements (possibly empty) — never raises.
    """
    if page is None or not hasattr(page, "css"):
        return []
    adaptive = enable_adaptive()
    try:
        found = page.css(css, identifier=identifier, adaptive=adaptive,
                         auto_save=adaptive)
    except TypeError:
        # Older/other builds may not carry these kwargs at all.
        try:
            found = page.css(css)
        except Exception as e:
            logger.debug(f"[scrapling] css({css}) failed: {e}")
            return []
    except Exception as e:
        logger.debug(f"[scrapling] adaptive css({css}) failed: {e}")
        return []
    if first:
        try:
            one = found.first
            return [one] if one is not None else []
        except Exception:
            return list(found)[:1]
    try:
        return list(found)
    except Exception:
        return []


def http_get(url: str, *, referer: Optional[str] = None, timeout: int = 30,
             retries_per_engine: int = 1,
             engines: Sequence[str] | None = None,
             extra_headers: Optional[dict] = None,
             proxy: Optional[str] = None,
             proxy_scope: Optional[str] = None) -> Optional[FetchResult]:
    """Fetch `url` with browser TLS impersonation, rotating fingerprints.

    `timeout` is SECONDS (Fetcher's unit). Returns None when Scrapling is
    absent or every engine failed — the caller then uses its legacy path.

    Proxy: an explicit `proxy` URL wins; otherwise `proxy_scope` triggers an
    environment lookup (`<SCOPE>_PROXY` → `SCRAPLING_PROXY` → `HTTPS_PROXY`…);
    with neither, the request goes out direct. A proxy is a *second* transport
    dimension, so the fingerprint rotation still runs inside it.
    """
    if not HAVE_SCRAPLING:
        return None

    proxy = proxy if proxy is not None else (resolve_proxy(proxy_scope) if proxy_scope else None)
    proxy_server, proxy_auth = _split_proxy(proxy) if proxy else (None, None)
    if proxy:
        logger.info(f"[scrapling http] proxy={_mask_proxy(proxy)}")

    for imp in (engines or IMPERSONATE_ORDER):
        kwargs = dict(impersonate=imp, stealthy_headers=True,
                      timeout=timeout, retries=retries_per_engine,
                      follow_redirects=True)
        if proxy_server:
            kwargs["proxy"] = proxy_server
            if proxy_auth:
                kwargs["proxy_auth"] = proxy_auth
        if referer:
            kwargs["headers"] = {**(extra_headers or {}), "Referer": referer}
        elif extra_headers:
            kwargs["headers"] = extra_headers
        try:
            page = Fetcher.get(url, **kwargs)
        except Exception as e:
            logger.debug(f"[scrapling http] {imp} failed for {url}: {type(e).__name__}: {str(e)[:140]}")
            continue

        body = page.body or b""
        text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else str(body)
        status = int(getattr(page, "status", 0) or 0)
        final = str(getattr(page, "url", url))
        if status == 200 and text and not looks_blocked(text):
            logger.info(f"[scrapling http] 200 via {imp}: {len(text):,} chars <- {final[-70:]}")
            return FetchResult(text=text, status=status, url=final,
                               via=f"http:{imp}" + ("+proxy" if proxy else ""),
                               page=page)
        logger.debug(f"[scrapling http] {imp} -> status={status} len={len(text):,} (rejected)")

    return None


def browser_get(url: str, *, wait_selector: Optional[str] = None,
                timeout_ms: int = DEFAULT_BROWSER_TIMEOUT_MS,
                solve_cloudflare: bool = False, retries: int = 1,
                extra_kwargs: Optional[dict] = None,
                proxy: Optional[str] = None,
                proxy_scope: Optional[str] = None) -> Optional[FetchResult]:
    """Fetch `url` through Scrapling's stealth browser (JS-rendered pages).

    `timeout_ms` is MILLISECONDS — that is StealthyFetcher's unit and the
    single easiest thing to get wrong in this library.

    `solve_cloudflare=True` makes Scrapling wait out/answer the Turnstile
    interstitial before returning; Scrapling itself raises the navigation
    timeout to at least 60 000 ms when it is on (`_validators.StealthConfig`),
    so callers must budget for a *minute-plus* per attempt, not seconds.

    `proxy`/`proxy_scope` behave as in `http_get`; Playwright wants the whole
    URL (or a `server/username/password` dict), so the string is passed
    through and Scrapling splits it itself.
    """
    if not HAVE_SCRAPLING:
        return None

    proxy = proxy if proxy is not None else (resolve_proxy(proxy_scope) if proxy_scope else None)

    kwargs = dict(headless=True, timeout=timeout_ms, retries=retries,
                  disable_resources=True, network_idle=False)
    exe = _chrome_executable()
    if exe:
        # Reuse the image's Chrome instead of downloading a private build.
        kwargs["real_chrome"] = True
        kwargs["executable_path"] = exe
    if solve_cloudflare:
        kwargs["solve_cloudflare"] = True
    if proxy:
        kwargs["proxy"] = proxy
        logger.info(f"[scrapling browser] proxy={_mask_proxy(proxy)}")
    if wait_selector:
        kwargs["wait_selector"] = wait_selector
    if extra_kwargs:
        kwargs.update(extra_kwargs)

    try:
        page = StealthyFetcher.fetch(url, **kwargs)
    except Exception as e:
        logger.warning(f"[scrapling browser] failed for {url}: {type(e).__name__}: {str(e)[:180]}")
        return None

    text = getattr(page, "html_content", "") or ""
    if not text:
        body = page.body or b""
        text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else str(body)
    status = int(getattr(page, "status", 0) or 0)
    final = str(getattr(page, "url", url))
    if status == 200 and text and not looks_blocked(text):
        logger.info(f"[scrapling browser] 200: {len(text):,} chars <- {final[-70:]}")
        return FetchResult(text=text, status=status, url=final, via="browser",
                           page=page)
    logger.warning(f"[scrapling browser] unusable: status={status} len={len(text):,} {final[-60:]}")
    return None


@dataclass
class CapturedResult:
    """Rendered HTML plus the XHR bodies a stealth session recorded."""
    html: str
    status: int
    url: str
    captured: list  # [(url, body_bytes)] in recording order


def browser_capture(url: str, *, url_pattern: str, timeout_ms: int = DEFAULT_BROWSER_TIMEOUT_MS,
                    wait_selector: Optional[str] = None, wait_ms: int = 2000,
                    include_static: bool = False,
                    max_recorded: int = 300) -> Optional[CapturedResult]:
    """Load `url` in a stealth session and return bodies of matching XHRs.

    This is what replaces hand-written Playwright response interception:
    measured on Scrapling 0.5, `capture_xhr` was removed in favour of
    `record_requests=True` + `session.network.search(url_pattern=…, status=…)`
    (engines/_browsers/_validators.py raises a TypeError pointing at exactly
    that). The recorder lives on the session, so the one-shot
    `StealthyFetcher.fetch()` cannot expose it — a `StealthySession` is needed,
    which is also cheaper here: one browser for discovery + interception.

    Returns None when Scrapling is missing or the load failed. Matching XHRs
    whose body is not JSON are still returned; parsing is the caller's job.
    """
    if not HAVE_SCRAPLING:
        return None

    kwargs = dict(headless=True, record_requests=True,
                  max_recorded_requests=max_recorded, disable_resources=True)
    exe = _chrome_executable()
    if exe:
        kwargs["real_chrome"] = True
        kwargs["executable_path"] = exe

    fetch_kwargs = dict(timeout=timeout_ms)
    if wait_selector:
        fetch_kwargs["wait_selector"] = wait_selector
    if wait_ms:
        # The table shell is painted before its data XHR lands, so hold the
        # page open briefly (milliseconds — StealthyFetcher's unit here).
        fetch_kwargs["wait"] = wait_ms

    try:
        with StealthySession(**kwargs) as sess:
            page = sess.fetch(url, **fetch_kwargs)
            found = sess.network.search(url_pattern=url_pattern,
                                        include_static=include_static,
                                        limit=None)
            captured = [(str(r.url), r.body or b"") for r in found]
            html = page.html_content or ""
            status = int(getattr(page, "status", 0) or 0)
    except Exception as e:
        logger.warning(f"[scrapling session] failed for {url}: "
                       f"{type(e).__name__}: {str(e)[:180]}")
        return None

    logger.info(f"[scrapling session] status={status} html={len(html):,} chars, "
                f"{len(captured)} response(s) matched {url_pattern!r}")
    return CapturedResult(html=html, status=status, url=url, captured=captured)


def page_title(text: str) -> str:
    m = re.search(r"<title>(.*?)</title>", text or "", re.S | re.I)
    return (m.group(1).strip() if m else "")


def source_is_expected(text: str, must_contain: Sequence[str], url: str = "") -> bool:
    """Guard against a source silently redirecting to a DIFFERENT series.

    Measured 2026-10-23: gurufocus `economic_indicators/1031/us-sp-500-earnings-yield`
    answers 200 but lands on `1031/quits-total-nonfarm` ("Quits: Total
    Nonfarm"). Its 1.9 value sails through the 0.5–15 sanity window, so a
    scraper pointed at that id stores a labour statistic as the S&P 500
    Earnings Yield. Any indicator scrape must therefore confirm the page is
    about the series it claims, before the number is trusted.
    """
    hay = f"{page_title(text)} {url}".lower()
    return all(term.lower() in hay for term in must_contain)
