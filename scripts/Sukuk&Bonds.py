"""
Sukuk & Bonds Scraper & DB Ingestion — Saudi Exchange (TASI)
============================================================
Extracts listed Sukuk and Government Bonds data from Tadawul debt page
and saves/updates them into the DB with strict Upsert logic.

Canonical Filename: backend/scripts/Sukuk&Bonds.py
Run Schedule      : Weekly or on-demand (not daily — sukuk list rarely changes)
Idempotent        : Yes — safe to re-run repeatedly without duplication

Transport (Scrapling, measured 2026-10-23 against the live page):
  - The grid's data arrives from an IBM WebSphere portal render URL that the
    page itself spells out: its JS asks for `p0/…getSukukMarketDetails=/`
    relative to `<base href="…/!ut/p/<state>!/">`. Rebuilding that pair and
    replaying it with Scrapling's impersonated HTTP returns the same 124 KB /
    62-item JSON a browser sees → no browser needed in the normal case.
  - Fallback 1: StealthySession with record_requests=True reads the live XHR
    off the network recorder (replaces the hand-written Playwright listener),
    then falls to parsing the rendered DOM table by HEADER NAME.
  - Fallback 2: verified local seed dataset (keeps Build-Up R running even if
    Akamai blocks the cloud IP entirely).
"""
import logging
import os
import sys
import time
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urljoin

# Setup path for backend imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session
from sqlalchemy import text
from app.core.database import SessionLocal, engine
from app.models.sukuk_bonds import SukukMarketData
from app.scrapers.scrapling_fetch import browser_capture, http_get


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
BASE_PAGE = "https://www.saudiexchange.sa/wps/portal/saudiexchange/ourmarkets/sukuk-market-watch?locale=en"
# Broad on purpose: every captured response is validated by `_looks_like_sukuk`,
# so a re-shape of the data URL must not silently kill the scrape. Measured
# 2026-10-23: exactly one recorded XHR matched the old hint, and it was the
# 128 KB / 62-item payload.
AJAX_URL_PATTERN = r"(sukukmarketdetails|sukuk.*market.*details|sukuk-market-watch/!ut/p/)"
# The grid's data URL is a portal render URL whose state string rotates, so it
# is rebuilt from the page on every run instead of being hardcoded. Measured
# 2026-10-23: the page's JS issues
#   ajax:{ url: 'p0/<portletState>=NJgetSukukMarketDetails=/', type:'GET',
#          data:{sectorParameter, iswatchListSelected, requestLocale} }
# — a path RELATIVE to the portal's <base href="…/!ut/p/<state>!/">. Rebuilding
# that pair and replaying it over impersonated HTTP returned the same 124 KB /
# 62-item JSON the browser sees, with no browser, no cookies and no JS.
# Sending it without the query answers `{"data":null}`, so both halves matter.
DETAILS_FRAG_RE = re.compile(r'''["'](p0/[^"'<>]*getSukukMarketDetails=/)["']''')
BASE_HREF_RE = re.compile(r'''<base\s+href\s*=\s*["\']([^"\']+)["\']''', re.I)
DETAILS_QUERY = "sectorParameter=all&iswatchListSelected=NO&requestLocale=en"
ORIGIN = "https://www.saudiexchange.sa"
# The verified listing is ~60 instruments; fewer than this means we hit some
# other grid on the page, not the sukuk table.
MIN_SUKUK_ITEMS = int(os.environ.get("SUKUK_MIN_ITEMS", "5"))
# A delisting run removes a handful of instruments. Two signals decide whether a
# shortfall is real delisting or a partial grid, because either floor alone is
# wrong on its own: a percentage floor refuses a legitimate retirement on a small
# table (1 of 4 is 25%), and an absolute cap would accept a 30-row hole in a
# 62-row market. So: a few missing rows always pass, and anything larger has to
# still cover most of the table.
MIN_DEACTIVATE_COVERAGE = float(os.environ.get("SUKUK_MIN_DEACTIVATE_COVERAGE", "0.8"))
MAX_RETIRE_PER_RUN = int(os.environ.get("SUKUK_MAX_RETIRE_PER_RUN", "5"))


# ──────────────────────────────────────────────────────────────────────────────
# Helpers — Type-safe parsers
# ──────────────────────────────────────────────────────────────────────────────

def _parse_decimal(value, *, label: str = "") -> Optional[Decimal]:
    """Convert various string/number representations to Decimal, or None."""
    if value is None:
        return None
    raw = str(value).strip().replace(",", "").replace("%", "").replace("٪", "")
    if raw in ("", "-", "N/A", "n/a", "--"):
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        logger.debug(f"[PARSE] Cannot convert {label!r}={value!r} to Decimal")
        return None


def _parse_date(value, *, label: str = "") -> Optional[date]:
    """
    Parse date from various formats used by Tadawul:
    YYYY-MM-DD, DD/MM/YYYY, MM/DD/YYYY, YYYY, 'DDMMYYYY', unix ms timestamps.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw in ("-", "N/A", "--"):
        return None

    # Unix-ms timestamp
    if raw.isdigit() and len(raw) == 13:
        try:
            return datetime.utcfromtimestamp(int(raw) / 1000).date()
        except Exception:
            pass

    # Try standard formats
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue

    # 4-digit year only
    if re.fullmatch(r"\d{4}", raw):
        try:
            return date(int(raw), 12, 31)
        except ValueError:
            pass

    logger.debug(f"[PARSE] Cannot parse {label!r}={value!r} as date")
    return None


# bondType codes as the grid actually spells them. Measured 2026-10-25 against
# the live payload (62 items) by crosstabbing bondType against sectorName:
#   'G' -> "Government Sukuk/Bonds Index"  (51 items, parentCompnaySymbol 5200,
#                                          issuer "KSA Sukuk 2041-07-20" …)
#   'S' -> "Corporate Sukuk/Bonds Index"   (11 items, parents 1010 / 9579 /
#                                          5025 …, issuer "RIYAD SUKUK PERP" …)
# So 'S' is the corporate *Sukuk* class, NOT "Sovereign". It used to fall through
# to the default branch and only looked correct by accident — an accidental 'C'
# is one source re-code away from silently moving the government benchmark onto
# corporate paper, which is what Build-Up R prices every company against.
_GOVT_TOKENS = ("G", "GOV", "GOVERNMENT", "SOVEREIGN", "SOVEREIGN SUKUK")
_CORP_TOKENS = ("C", "S", "CORP", "CORPORATE", "COMPANY")
# Substrings that identify sovereign paper even when the code column is absent:
# the DOM grid has no issuer-type column at all, so the name is the only signal.
_GOVT_NAME_HINTS = ("GOVERNMENT", "GOVT", "KSA SUKUK", "SAUDI ARABIA GOVERNMENT")


def _classify_bond_type(raw: Optional[str],
                       issuer_name: Optional[str] = None,
                       sector_name: Optional[str] = None) -> str:
    """Normalize to 'G' (Government) | 'C' (Corporate); never leave it ambiguous.

    Signal order: the source's own code, then the index the row belongs to, then
    the issuer name. The two name-based hints exist for the DOM fallback, where
    neither bondType nor sectorName is available — without them every instrument
    parsed from the rendered table was stamped corporate.
    """
    if raw:
        r = str(raw).strip().upper()
        if r in _GOVT_TOKENS:
            return "G"
        if r in _CORP_TOKENS:
            return "C"
        if "GOV" in r:
            return "G"
    for hint in (sector_name, issuer_name):
        h = str(hint or "").strip().upper()
        if h and any(tok in h for tok in _GOVT_NAME_HINTS):
            return "G"
    return "C"


# ──────────────────────────────────────────────────────────────────────────────
# DB Bootstrap
# ──────────────────────────────────────────────────────────────────────────────

def ensure_table_exists():
    """Ensure the sukuk_market_data table exists."""
    try:
        SukukMarketData.__table__.create(engine, checkfirst=True)
        logger.info("[DB] Table 'sukuk_market_data' checked/ready.")
    except Exception as e:
        logger.error(f"[DB] Error checking table: {e}")


def _extract_items(data: dict) -> list:
    """Extract sukuk list from various possible JSON response shapes."""
    if not isinstance(data, dict):
        return []
    return data.get("sukukList") or data.get("data") or data.get("aaData") or []


def _items_from_body(body) -> list:
    """Decode an HTTP/XHR response body into raw items, or [] if it isn't JSON."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", "ignore")
    if not body or body.lstrip()[:1] not in ("{", "["):
        return []
    try:
        data = json.loads(body)
    except Exception:
        return []
    if isinstance(data, list):
        return data
    return _extract_items(data)


def _looks_like_sukuk(items: list) -> bool:
    """Guard against harvesting a DIFFERENT grid on the same page."""
    if not items:
        return False
    first = items[0]
    return (isinstance(first, dict) and bool(str(first.get("symbol", "")).strip())
            and any(k in first for k in ("maturityDate", "couponRate", "bondType")))


# ──────────────────────────────────────────────────────────────────────────────
# Data Fetching — Strategy 1 (optional): ScraperAPI (paid proxy, Saudi IP)
# ──────────────────────────────────────────────────────────────────────────────

def fetch_sukuk_via_scraperapi(api_key: str) -> Optional[list]:
    """
    Primary live scraper using ScraperAPI to bypass Akamai WAF.
    Renders the Sukuk market watch page and extracts table data from the HTML.
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        logger.error("[SCRAPERAPI] requests/bs4 not installed")
        return None

    logger.info("🛡️ Attempting Sukuk scrape via ScraperAPI with Saudi IP...")
    try:
        r = requests.get("https://api.scraperapi.com", params={
            "api_key": api_key,
            "url": BASE_PAGE,
            "render": "true",
            "country_code": "sa",
        }, timeout=120)

        if r.status_code != 200:
            logger.warning(f"[SCRAPERAPI] Page render returned status {r.status_code}")
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        # Try to extract JSON from inline scripts (some pages embed sukuk data as JS objects)
        for script in soup.find_all("script"):
            script_text = script.string or ""
            if "sukukList" in script_text or "sukukMarketDetails" in script_text:
                # Try extracting JSON array from the script
                json_match = re.search(r'\[\s*\{.*?"symbol".*?\}\s*\]', script_text, re.DOTALL)
                if json_match:
                    try:
                        items = json.loads(json_match.group())
                        if items and len(items) > 3:
                            logger.info(f"[SCRAPERAPI] ✅ Extracted {len(items)} sukuk from inline JSON!")
                            return items
                    except json.JSONDecodeError:
                        pass

        # Fallback: parse DOM tables from rendered HTML
        items = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 3:
                continue

            for row in rows:
                cells = row.find_all(["td"])
                if len(cells) < 5:
                    continue

                symbol = cells[0].get_text(strip=True)
                # Skip header rows
                if not symbol or symbol in ("Symbol", "الرمز", "", "الإجمالي", "Total"):
                    continue

                items.append({
                    "symbol":                    symbol,
                    "issuerName":                cells[1].get_text(strip=True) if len(cells) > 1 else "",
                    "couponRate":                cells[2].get_text(strip=True) if len(cells) > 2 else "",
                    "issueDateStr":              cells[3].get_text(strip=True) if len(cells) > 3 else "",
                    "maturityDateStr":           cells[4].get_text(strip=True) if len(cells) > 4 else "",
                    "outstandingAmountModified": cells[5].get_text(strip=True) if len(cells) > 5 else "",
                    "bondType":                  cells[6].get_text(strip=True) if len(cells) > 6 else "",
                    "parentCompnaySymbol":       cells[7].get_text(strip=True) if len(cells) > 7 else "",
                })

        if items:
            logger.info(f"[SCRAPERAPI] ✅ Extracted {len(items)} sukuk from DOM table!")
            return items

        logger.warning("[SCRAPERAPI] Page rendered but no sukuk data found in HTML")
        return None

    except Exception as e:
        logger.error(f"[SCRAPERAPI] Sukuk fetch failed: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Data Fetching — Strategy 2/3: Scrapling HTTP replay, then stealth session
# ──────────────────────────────────────────────────────────────────────────────

def _data_url(html: str) -> Optional[str]:
    """Rebuild the grid's data URL exactly as the page's own JS resolves it.

    `_` is the Dojo cache-buster (milliseconds); the server ignores its value,
    but dropping the whole query makes it answer `{"data":null}`.
    """
    frag = DETAILS_FRAG_RE.search(html or "")
    if not frag:
        return None
    base = BASE_HREF_RE.search(html or "")
    url = urljoin(base.group(1) if base else BASE_PAGE, frag.group(1))
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{DETAILS_QUERY}&_={int(time.time() * 1000)}"


def fetch_sukuk_via_http(min_items: int = MIN_SUKUK_ITEMS) -> Optional[list]:
    """Browser-free primary path: replay the grid's own data URL over HTTP.

    Measured 2026-10-23: chrome120 / chrome / safari180 each returned
    200 + 124,713 bytes + the same 62 items the browser sees, so Chromium stays
    in reserve and the happy path costs two HTTP round trips.
    Returns raw items in the Tadawul JSON vocabulary, or None.
    """
    logger.info("[HTTP] Attempting sukuk data-URL replay (no browser)...")
    page = http_get(BASE_PAGE, referer=f"{ORIGIN}/", timeout=45)
    if page is None:
        logger.warning("[HTTP] Scrapling could not load the sukuk page")
        return None

    url = _data_url(page.text)
    if not url:
        logger.warning("[HTTP] data URL not quoted in the page — a browser is required")
        return None

    # Two tries: the portal state can expire between loading the page and
    # replaying it, and a freshly loaded page then carries a live one.
    for attempt in (1, 2):
        res = http_get(url, referer=BASE_PAGE, timeout=45,
                       extra_headers={"Accept": "application/json, text/javascript, */*;q=0.8",
                                      "X-Requested-With": "XMLHttpRequest"})
        items = _items_from_body(res.text) if res else []
        if len(items) >= min_items and _looks_like_sukuk(items):
            logger.info(f"[HTTP] ✅ {len(items)} sukuk from replayed data URL "
                        f"({len(res.text):,} bytes, {res.via}, no browser)")
            return items
        if attempt == 1:
            page = http_get(BASE_PAGE, referer=f"{ORIGIN}/", timeout=45)
            url = _data_url(page.text) if page else None
            if not url:
                break

    logger.warning("[HTTP] data URL answered no usable sukuk JSON")
    return None


def fetch_sukuk_via_browser(max_wait_seconds: int = 45) -> Optional[list]:
    """
    Secondary live scraper — Scrapling's stealth session in place of the
    hand-written Playwright one:
      - `record_requests=True` keeps every completed response on the session's
        NetworkRecorder and we search it by URL pattern. That replaces the old
        `page.on("response")` listener plus polling loop; note `capture_xhr` no
        longer exists in Scrapling 0.5, and the `--disable-http2` /
        AutomationControlled flags are unnecessary because the stealth browser
        is already shaped like one.
      - If no JSON matched, the rendered DOM table is parsed by header name.
    """
    timeout_ms = max(30, int(max_wait_seconds)) * 1000
    logger.info(f"[BROWSER] Launching Scrapling stealth session (timeout {timeout_ms:,} ms)...")

    res = browser_capture(BASE_PAGE, url_pattern=AJAX_URL_PATTERN,
                          wait_selector="table", timeout_ms=timeout_ms)
    if res is None:
        logger.warning("[BROWSER] stealth session could not load the page")
        return None

    # 1) the grid's own XHR, read off the recorder
    for url, body in res.captured:
        items = _items_from_body(body)
        if len(items) >= MIN_SUKUK_ITEMS and _looks_like_sukuk(items):
            logger.info(f"[BROWSER] ✅ Captured {len(items)} items from live network "
                        f"traffic ({len(body):,} bytes)")
            return items

    # 2) rendered DOM table
    logger.info(f"[BROWSER] no JSON matched; parsing the rendered table "
                f"({len(res.html):,} chars, {len(res.captured)} XHR(s) seen)...")
    items = _parse_dom_table(res.html)
    if items:
        logger.info(f"[BROWSER] ✅ Scraped {len(items)} items from the DOM table.")
        return items

    logger.warning("[BROWSER] page loaded but holds no sukuk rows")
    return None


# Raw item key ← header label substring. Matched by NAME so a column re-order
# cannot shift values into the wrong field: the previous fixed positions were
# already wrong for today's layout (measured 2026-10-23 — cells[2] is the ISIN
# and cells[3] is "Floating", so couponRate was being read from the ISIN).
_DOM_COLUMNS = (
    ("symbol",                    ("tadawulcode", "symbol")),
    ("issuerName",                ("name",)),
    ("couponRate",                ("couponrate",)),
    ("maturityDateStr",           ("maturitydate", "maturity")),
    ("issueDateStr",              ("issuedate", "issuancedate")),
    ("outstandingAmountModified", ("issuanceamountoutstanding", "issuanceamount",
                                   "outstanding", "amount")),
    # 'Instrument  Yield' (two spaces in the live header) is the grid's traded
    # yield — the same number `lastTadeYield` carries in JSON, so the DOM path
    # fills the identical raw key and `_normalize_item` needs no branch.
    ("lastTadeYield",               ("instrumentyield", "instumentyield",
                                    "lasttadedyield", "lasttradedyield")),
    ("currency",                  ("currency",)),
    ("parentCompnaySymbol",       ("parent",)),
)
_JUNK_SYMBOLS = ("symbol", "tadawulcode", "total", "الإجمالي", "الرمز")


def _norm_header(text: str) -> str:
    """Lowercase and strip everything but letters (EN + AR) for fuzzy matching."""
    return re.sub(r"[^a-z\u0600-\u06ff]+", "", (text or "").lower())


def _header_map(headers: list) -> dict:
    """{column index: raw item key}; each key is claimed by the first match."""
    mapping, used = {}, set()
    for idx, head in enumerate(headers):
        h = _norm_header(head)
        if not h:
            continue
        for key, needles in _DOM_COLUMNS:
            if key in used:
                continue
            if any(n in h for n in needles):
                mapping[idx] = key
                used.add(key)
                break
    return mapping


def _parse_dom_table(html: str) -> Optional[list]:
    """Map the rendered grid to raw items by header name (last-resort path).

    The DOM has no issuer-type column, so anything parsed here defaults to
    bond_type 'C' in `_classify_bond_type` — another reason the JSON stays the
    primary source and this only runs when it is unavailable.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.error("[BROWSER] bs4 not installed; DOM fallback unavailable")
        return None

    soup = BeautifulSoup(html or "", "html.parser")
    best = None
    for table in soup.find_all("table"):
        rows = [r for r in table.find_all("tr") if r.find("td")]
        if best is None or len(rows) > len(best[1]):
            best = (table, rows)
    if best is None or len(best[1]) < 3:
        return None
    table, rows = best

    headers = [th.get_text(strip=True) for th in table.find_all("th")]
    if not headers:
        headers = [td.get_text(strip=True) for td in rows[0].find_all("td")]
    colmap = _header_map(headers)
    if "symbol" not in colmap.values():
        logger.warning(f"[BROWSER] table headers not recognised: {headers[:8]}")
        return None
    logger.info(f"[BROWSER] column map: {colmap}")

    items = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue
        it = {key: cells[idx].get_text(strip=True)
              for idx, key in colmap.items() if idx < len(cells)}
        sym = str(it.get("symbol", "")).strip()
        # Compared RAW: Tadawul codes are digits ("5389"), and the letter-only
        # normalisation used for headers would collapse every one of them to "",
        # which is exactly how the first live run of this parser dropped all 62
        # rows.
        if not sym or sym.lower() in _JUNK_SYMBOLS:
            continue
        items.append(it)
    return items or None


# ──────────────────────────────────────────────────────────────────────────────
# Data Fetching — Backup Strategy: Curated Seed Dataset (Production Resilience)
# ──────────────────────────────────────────────────────────────────────────────

def load_sukuk_from_seed() -> Optional[list]:
    """
    Loads verified listed Sukuk & Bonds data from local seed repository.
    Ensures 100% continuous uptime for Build-Up R & valuation models when Akamai
    blocks requests in cloud/CI environments.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    seed_file = os.path.join(base_dir, "data", "sukuk_seed_data.json")
    if not os.path.exists(seed_file):
        logger.warning(f"[SEED] Seed file not found at: {seed_file}")
        return None

    try:
        with open(seed_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data and isinstance(data, list):
                logger.info(f"[SEED] ✅ Loaded {len(data)} verified sukuk records from local repository.")
                return data
    except Exception as e:
        logger.error(f"[SEED] Failed loading seed data: {e}")

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Normalize raw API item → typed dict
# ──────────────────────────────────────────────────────────────────────────────

def _normalize_item(it: dict, source_url: str) -> Optional[dict]:
    """
    Normalize a raw item from the Tadawul JSON response.
    Returns a typed dict ready for DB insertion, or None if symbol is missing.

    IMPORTANT: coupon_rate is stored as coupon_rate only.
    yield_to_maturity is stored separately when the source provides it.
    Never treat coupon_rate as YTM unless the source explicitly says so.
    """
    sym = str(it.get("symbol", "")).strip()
    if not sym:
        return None

    coupon = _parse_decimal(it.get("couponRate"), label="couponRate")

    # `lastTadeYield` is the source's own typo for "last traded yield" and it is
    # the ONLY instrument-level market yield the grid offers: measured non-null
    # for 53 of 62 live items. bidYield/askYield carry no extra coverage (the
    # same 53), so no mid-quote is synthesised — a traded yield or nothing, and
    # Build-Up R then falls back to coupon_rate with is_ytm=False.
    ytm_raw = (it.get("lastTadeYield") or it.get("ytm")
               or it.get("yieldToMaturity") or it.get("yield_to_maturity"))
    ytm = _parse_decimal(ytm_raw, label="ytm")

    issue_dt = _parse_date(
        it.get("issueDateStr") or it.get("issueDate"), label="issueDate"
    )
    maturity_dt = _parse_date(
        it.get("maturityDateStr") or it.get("maturityDate"), label="maturityDate"
    )

    amount = _parse_decimal(
        it.get("outstandingAmountModified") or it.get("outstandingAmount"),
        label="outstandingAmount"
    )

    bond_type = _classify_bond_type(it.get("bondType") or it.get("bond_type"),
                                    issuer_name=it.get("issuerName"),
                                    sector_name=it.get("sectorName"))

    parent_sym = str(it.get("parentCompnaySymbol", "")).strip() or None

    return {
        "symbol":                sym,
        "issuer_name":           it.get("issuerName") or it.get("issuer_name"),
        "parent_company_symbol": parent_sym,
        "bond_type":             bond_type,
        "coupon_rate":           coupon,
        "yield_to_maturity":     ytm,
        "issue_date":            issue_dt,
        "maturity_date":         maturity_dt,
        "outstanding_amount":    amount,
        # The live JSON names this field `issueCurrency`; reading only `currency`
        # used to stamp every row SAR regardless of what the source said.
        "currency":              str(it.get("currency") or it.get("issueCurrency") or "SAR").strip(),
        "sector_name":           it.get("sectorName") or it.get("sector_name"),
        "source_url":            source_url,
        "is_active":             True,
        "as_of":                 datetime.utcnow().strftime("%Y-%m-%d"),
    }


# ──────────────────────────────────────────────────────────────────────────────
# DB Upsert
# ──────────────────────────────────────────────────────────────────────────────

def _sukuk_key(symbol, parent) -> tuple:
    """Composite upsert key = the DB constraint uq_sukuk_symbol_parent.

    The parent is normalised to '' rather than passed through as None because
    Postgres treats NULLs as DISTINCT inside a UNIQUE constraint: (5389, NULL)
    twice is allowed, so a lookup keyed on None would miss the stored row and
    insert a fresh duplicate on every run — the table would quietly inflate.
    """
    return (str(symbol or "").strip(), str(parent or "").strip())


def _deactivate_retired(db: Session, seen_keys: set) -> int:
    """Flag rows the live grid no longer lists as is_active=False.

    Without this a matured/delisted sukuk stays active forever and Build-Up R
    keeps pricing companies off paper that no longer trades. Callers must only
    run it against a live payload: the seed dataset is a frozen snapshot, where
    "missing" means "the snapshot is old", not "delisted".
    """
    active = db.query(SukukMarketData).filter(SukukMarketData.is_active == True).all()
    active_keys = {_sukuk_key(r.symbol, r.parent_company_symbol) for r in active}
    stale = [r for r in active
             if _sukuk_key(r.symbol, r.parent_company_symbol) not in seen_keys]
    if not stale:
        return 0

    if (len(stale) > MAX_RETIRE_PER_RUN
            and len(seen_keys) < len(active_keys) * MIN_DEACTIVATE_COVERAGE):
        logger.warning(
            f"[DB] Retirement skipped: {len(stale)} of {len(active_keys)} active rows "
            f"are absent while the payload holds {len(seen_keys)} — that is more than "
            f"{MAX_RETIRE_PER_RUN} and under the {MIN_DEACTIVATE_COVERAGE:.0%} coverage "
            f"floor, so it looks like a partial grid rather than delisting"
        )
        return 0

    for r in stale:
        r.is_active = False
    logger.info(f"[DB] 🧹 {len(stale)} instrument(s) no longer listed by the grid "
                f"-> is_active=False: {[r.symbol for r in stale][:10]}")
    return len(stale)


def save_sukuk_to_db(items: list, source_url: str = BASE_PAGE,
                     mark_missing_inactive: bool = False) -> int:
    """
    Saves or updates Sukuk records in PostgreSQL using strict upsert logic.

    Unique key = (symbol, parent_company_symbol), exactly the DB constraint — the
    lookup used to be keyed on symbol alone, which silently re-parented one row
    whenever an issuer had two sukuk listed side by side.
    Uses a pre-fetched dictionary lookup (pattern from
    daily_financial_indicators_scraper) to eliminate N+1 queries and to avoid raw
    ON CONFLICT, whose target must match the constraint name for it to work.
    """
    ensure_table_exists()
    db: Session = SessionLocal()
    saved_count = 0
    skipped_count = 0
    deactivated_count = 0

    try:
        # Pre-fetch once: exact composite key, plus a per-symbol pool used only
        # to repair legacy rows whose parent column is still empty (they must be
        # adopted, never duplicated — see _sukuk_key).
        existing_records: dict = {}
        pool_by_symbol: dict = {}
        for r in db.query(SukukMarketData).all():
            existing_records[_sukuk_key(r.symbol, r.parent_company_symbol)] = r
            pool_by_symbol.setdefault(str(r.symbol or "").strip(), []).append(r)

        seen_keys: set = set()

        for raw_it in items:
            norm = _normalize_item(raw_it, source_url=source_url)
            if not norm:
                skipped_count += 1
                logger.warning(f"[DB] Skipped item with no symbol: {raw_it}")
                continue

            sym = norm["symbol"]
            key = _sukuk_key(sym, norm["parent_company_symbol"])
            seen_keys.add(key)

            try:
                existing = existing_records.get(key)

                if existing is None:
                    orphans = [r for r in pool_by_symbol.get(key[0], [])
                               if not str(r.parent_company_symbol or "").strip()]
                    if orphans:
                        existing = orphans[0]
                        pool_by_symbol[key[0]].remove(existing)
                        # Its old (symbol, '') slot now points at a row that is
                        # about to be re-parented — drop it or a later item could
                        # claim the same row twice.
                        existing_records.pop((key[0], ""), None)

                if existing:
                    existing.issuer_name           = norm["issuer_name"]
                    existing.parent_company_symbol = norm["parent_company_symbol"]
                    existing.bond_type             = norm["bond_type"]
                    existing.coupon_rate           = norm["coupon_rate"]
                    existing.yield_to_maturity     = norm["yield_to_maturity"]
                    existing.issue_date            = norm["issue_date"]
                    existing.maturity_date         = norm["maturity_date"]
                    existing.outstanding_amount    = norm["outstanding_amount"]
                    existing.currency              = norm["currency"]
                    existing.sector_name           = norm["sector_name"]
                    existing.source_url            = norm["source_url"]
                    existing.is_active             = norm["is_active"]
                    existing.as_of                 = norm["as_of"]
                else:
                    new_obj = SukukMarketData(**norm)
                    db.add(new_obj)
                    existing_records[key] = new_obj
                    pool_by_symbol.setdefault(key[0], []).append(new_obj)

                saved_count += 1

            except Exception as row_err:
                logger.error(f"[DB] Failed to upsert symbol={sym}: {row_err}")
                skipped_count += 1
                continue

        if mark_missing_inactive and saved_count >= MIN_SUKUK_ITEMS:
            # Same transaction as the upserts — one atomic write, never a half
            # retired table if the commit fails.
            deactivated_count = _deactivate_retired(db, seen_keys)

        db.commit()
        logger.info(
            f"[DB] ✅ Upserted {saved_count} Sukuk & Bonds records successfully. "
            f"Skipped {skipped_count} invalid items."
            + (f" Retired {deactivated_count}." if deactivated_count else "")
        )

    except Exception as e:
        db.rollback()
        logger.error(f"[DB] Fatal error during bulk upsert: {e}")
        return 0
    finally:
        db.close()

    return saved_count


# ──────────────────────────────────────────────────────────────────────────────
# Build-Up R Integration: get best yield for a company
# ──────────────────────────────────────────────────────────────────────────────

_SUKUK_CACHE = {"timestamp": 0.0, "company_map": {}, "govt": None}

def _get_sukuk_memory_cache():
    now = time.time()
    if _SUKUK_CACHE["company_map"] and (now - _SUKUK_CACHE["timestamp"] < 300):
        return _SUKUK_CACHE["company_map"], _SUKUK_CACHE["govt"]

    db = SessionLocal()
    company_map = {}
    govt = None
    try:
        all_active = db.query(SukukMarketData).filter(SukukMarketData.is_active == True).order_by(SukukMarketData.maturity_date.desc()).all()
        for item in all_active:
            if item.bond_type == "G" and govt is None:
                govt = {
                    "yield_pct": float(item.yield_to_maturity or item.coupon_rate) if (item.yield_to_maturity or item.coupon_rate) is not None else 5.5,
                    "symbol": item.symbol,
                    "issuer": item.issuer_name,
                    "as_of": item.as_of,
                    "is_ytm": item.yield_to_maturity is not None
                }

            p_sym = str(item.parent_company_symbol) if item.parent_company_symbol else None
            sym = str(item.symbol) if item.symbol else None
            y_val = item.yield_to_maturity or item.coupon_rate

            if y_val is not None:
                entry = {
                    "yield_pct": float(y_val),
                    "source": "company_sukuk_db",
                    "symbol": item.symbol,
                    "issuer": item.issuer_name,
                    "bond_type": item.bond_type,
                    "is_ytm": item.yield_to_maturity is not None,
                    "is_fallback": False,
                    "as_of": item.as_of,
                }
                if p_sym and p_sym not in company_map:
                    company_map[p_sym] = entry
                if sym and sym not in company_map:
                    company_map[sym] = entry

        _SUKUK_CACHE["timestamp"] = now
        _SUKUK_CACHE["company_map"] = company_map
        _SUKUK_CACHE["govt"] = govt
    except Exception as e:
        logger.warning(f"[BUILD-UP] Failed to pre-load sukuk memory cache: {e}")
    finally:
        db.close()
    return _SUKUK_CACHE["company_map"], _SUKUK_CACHE["govt"]


def get_buildup_sukuk_yield(equity_symbol: str) -> dict:
    """
    Returns the best available sukuk/debt yield for a company's Build-Up R calculation.
    Uses in-memory cache to execute instantly across universe loops.
    """
    eq_sym = str(equity_symbol)
    try:
        company_map, govt = _get_sukuk_memory_cache()
        if eq_sym in company_map:
            return company_map[eq_sym]

        if govt:
            return {
                "yield_pct":    govt["yield_pct"],
                "source":       "govt_sukuk_benchmark_db",
                "symbol":       govt["symbol"],
                "issuer":       govt["issuer"],
                "bond_type":    "G",
                "is_ytm":       govt["is_ytm"],
                "is_fallback":  True,
                "reason":       f"No company-specific sukuk for {equity_symbol}; using KSA Government Benchmark",
                "as_of":        govt["as_of"],
            }
    except Exception as e:
        logger.warning(f"[BUILD-UP] Sukuk lookup failed for {equity_symbol}: {e}")

    return {
        "yield_pct":    5.50,
        "source":       "sama_repo_rate_fallback",
        "symbol":       None,
        "issuer":       "SAMA",
        "bond_type":    "G",
        "is_ytm":       False,
        "is_fallback":  True,
        "reason":       "No active sukuk found in DB for this company or government benchmark.",
        "as_of":        None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ──────────────────────────────────────────────────────────────────────────────

def run_sukuk_sync() -> int:
    """
    Main execution entry point — cheapest transport first:
      1. ScraperAPI, only when a key is configured (paid proxy, Saudi IP)
      2. Scrapling impersonated HTTP replay of the grid's data URL (no browser)
      3. Scrapling stealth session: recorded XHR, else DOM-by-header
      4. Verified local seed dataset (keeps Build-Up R served 100% of the time)
    """
    logger.info("=== Starting Sukuk & Bonds Data Sync (Phase 5) ===")

    # 0. Try ScraperAPI first for WAF bypass
    scraperapi_key = os.environ.get("SCRAPERAPI_KEY")
    if not scraperapi_key:
        try:
            from app.core.config import settings
            scraperapi_key = getattr(settings, "SCRAPERAPI_KEY", None)
        except Exception:
            pass

    items = None
    source_url = BASE_PAGE

    if scraperapi_key:
        try:
            items = fetch_sukuk_via_scraperapi(scraperapi_key)
        except Exception as e:
            logger.error(f"[SYNC] ScraperAPI attempt failed: {e}")

    # 2. Browser-free HTTP replay of the grid's own data URL
    if not items:
        items = fetch_sukuk_via_http()

    # 3. Live stealth browser (recorded XHR, then DOM)
    if not items:
        items = fetch_sukuk_via_browser()

    # 4. Resilient local seed fallback
    if not items:
        logger.warning("[SYNC] Live scraping was blocked by Akamai — activating verified seed dataset...")
        items = load_sukuk_from_seed()
        source_url = "local_seed_repository"

    if items:
        # Retirement marking only makes sense against a live grid. The seed file
        # is a frozen snapshot: marking its absences as delisted would disable
        # real instruments the first time the seed lags the market.
        count = save_sukuk_to_db(
            items, source_url=source_url,
            mark_missing_inactive=(source_url != "local_seed_repository"),
        )
        logger.info(f"=== Sync Finished: {count} instruments updated ===")
        return count
    else:
        logger.error("=== Sync FAILED: No data fetched from live browser or seed dataset ===")
        return 0


if __name__ == "__main__":
    # Wrapped here (not inside run_sukuk_sync) so an APScheduler / admin-triggered
    # run is not recorded twice. job key matches scheduler._safe(job_key=...).
    from app.services.job_tracking import track_job
    with track_job("sukuk_importer", "manual") as run:
        result = run_sukuk_sync()
        run.rows_written = result if isinstance(result, int) else None
    print(f"\n[SUCCESS] Sukuk & Bonds sync complete: {result} records upserted.")