"""
SAMA & GaStat — Saudi Macroeconomic Data Sync (Phase 5, restructured 2026-10-26)
================================================================================
RUN SCHEDULE : MONTHLY (not daily) — SAMA publishes a new bulletin once per month.
TRIGGER      : Run manually on the 1st of each month, or when SAMA releases a new bulletin.

WHAT CHANGED IN THE RESTRUCTURE
-------------------------------
The old parser kept ONE row of a 334-row sheet (`s.iloc[-1]`) and stamped it with
`_CURRENT_PERIOD` — the month we ran the script. Measured against the live
bulletin: the row stored as 'saibor_3m / 2026-09 / 3.905' is the 2026-08-31
observation, and the '2026-10 / 3.897' row was a hard-coded fallback holding the
2026-07-31 value. Now the whole monthly block is written with SAMA's own
month-end dates, and the period column is a real DATE (alembic revision
2026_10_26_macro_period_date).

DATA COLLECTED
--------------
  Source : SAMA Monthly Statistical Bulletin
  URL    : https://www.sama.gov.sa/en-US/Statistics/pages/monthlystatistics.aspx
  Sheet  : '5-6' — Money & Banking / Interest Rates
  Path   : Scrapling StealthyFetcher renders the page, then its `page_action`
           hook downloads the newest .xlsx into memory (BytesIO) — the
           Selenium/ChromeDriver session-cookie dance is gone.
           CHROME_BIN / GOOGLE_CHROME_BIN must point at a Chrome binary
           (both are set in the Dockerfile); without one Scrapling falls back
           to Playwright's private chromium, which is not installed.

  Sheet '5-6' layout (header row 9, measured on the Aug-2026 issue):
      rows   0..18   1 sub-header + 19 ANNUAL observations (2007-2025)
      rows  20..97   78 QUARTERLY observations ('Q1'..'Q4', year in 'Unnamed: 1')
      rows  98..333  236 MONTHLY observations, month-end Period 2007-01-31 .. 2026-08-31
      Period | Unnamed: 1 | RR | RRR | 'Monthly Avg. ' | 1 week | 4 weeks |
      13 weeks | 26 weeks | 52 weeks
      'Monthly Avg. ' (4.83 on 2026-08 vs 3.905 for 13-week) has no documented
      definition — it is deliberately NOT imported.

  Source : GaStat / KAPSARC Open Data
  Data   : GDP at Current Prices (Nominal GDP, M SAR) — full TTM history from
           the last 20 published quarters (each TTM = 4 consecutive quarters).

  Source : GaStat — Labour Force Survey
  Data   : Saudi unemployment rate, with the publisher's own period date.

CALCULATED
----------
  Saudi Buffett Indicator = TASI Total Market Cap / GDP at Current Prices × 100
  (No buy/sell wording attached — methodological reference only.)
  NOT backfilled: historical market cap is not stored, so writing a fake history
  would be fabrication. One row per run, dated by its GDP reference quarter.

OUTPUT TABLE : saudi_economic_indicators
  period is a DATE (business date from the publisher), basis distinguishes
  derivations (TTM / Annualized). Composite key indicator_key + period + source
  is enforced by uq_indicator_period_source; every write is an
  INSERT ... ON CONFLICT DO UPDATE, so re-running heals past mistakes.

FALLBACK RULES (Phase 5 Non-Negotiable)
  - Fallback is written for the NEWEST observation only, dated from the last
    real observation we hold (or its own documented date), never stamped "now".
  - is_fallback=True + source_status='fallback' always travel with the value.
  - If the bulletin cannot be parsed, nothing is written for the rate keys —
    the API serves null + a `degraded` entry instead of a stale number.

CLI
---
    python "scripts/SAMA&GaStat.py"                 # dry run: parse + report only
    python "scripts/SAMA&GaStat.py" --execute       # write to the database
    python "scripts/SAMA&GaStat.py" --no-history    # newest observation only
    python "scripts/SAMA&GaStat.py" --bulletin-file path.xlsx   # offline parse
    python "scripts/SAMA&GaStat.py" --csv-out out.csv           # dump observations
"""
import argparse
import logging
import os
import re
import sys
import requests
import pandas as pd
from io import BytesIO
from datetime import date, datetime

# Setup backend imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text as sa_text
from app.core.database import SessionLocal, engine
from app.models.saudi_macro import SaudiEconomicIndicator
from app.scrapers.scrapling_fetch import browser_get

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
SAMA_BASE     = "https://www.sama.gov.sa"
SAMA_PAGE_URL = f"{SAMA_BASE}/en-US/Statistics/pages/monthlystatistics.aspx"
KAPSARC_GDP_API = (
    "https://datasource.kapsarc.org/api/explore/v2.1/catalog/datasets/"
    "gross-domestic-product-by-kind-of-economic-activity-at-current-prices-2023-100/"
    "records?where=economic_activity%3D%22Gross%20Domestic%20Product%22%20and%20unit%3D%22Million%20of%20Saudi%20Riyals%22"
    # limit=100 covers the whole dataset: measured 2026-10-26 total_count=64
    # quarterly rows, 2010-Q1 .. 2025-Q4.
    "&order_by=date%20desc&limit=100"
)
GASTAT_UNEMPLOYMENT_API = (
    "https://open.data.gov.sa/api/explore/v2.1/catalog/datasets/"
    "employment-unemployment-indicators/records?limit=5&order_by=date%20desc"
)
KAPSARC_UNEMPLOYMENT_API = (
    "https://datasource.kapsarc.org/api/explore/v2.1/catalog/datasets/labor-force-survey-data/records?"
    "where=search(indicator,%20%27Unemployement%27)%20and%20age_group%3D%22Total%22%20and%20gender%3D%22Total%22%20and%20nationality%3D%22Saudi%22"
    "&order_by=time_period%20desc&limit=1"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
}

# Canonical source spellings — the 2026-10 migration collapsed 'SAMA' and
# 'SAMA — Monthly Bulletin' into the ASCII spelling below. Do not vary it:
# the unique key (indicator_key, period, source) treats any drift as a new row.
SOURCE_SAMA       = "SAMA Monthly Bulletin"
SOURCE_GASTAT     = "GaStat / KAPSARC"
SOURCE_GASTAT_LFS = "GaStat Labour Force Survey"
SOURCE_REBH       = "REBH Engine"

# Fallback constants — last VERIFIED bulletin readings (2026-07-31 rows of the
# Aug-2026 bulletin sheet 5-6). Used only for the newest observation when the
# live bulletin is unreachable, always with is_fallback=True.
_FALLBACK_REPO_RATE    = 4.25
_FALLBACK_REVERSE_REPO = 3.75
_FALLBACK_SAIBOR_3M    = 3.897
_FALLBACK_SAIBOR_12M   = 4.062
_FALLBACK_GDP_M_SAR    = 4788536.0  # KAPSARC 2025 trailing annual GDP (M SAR)
_FALLBACK_GDP_PERIOD   = date(2025, 12, 31)
_FALLBACK_UNEMPLOY_PCT = 7.1        # GaStat 2024/2025 Saudi unemployment estimate
_FALLBACK_UNEMP_PERIOD = date(2024, 12, 31)

# indicator_key -> (sheet '5-6' column, human name). 'Unnamed: 7' / 'Unnamed: 9'
# were verified against production values on 2026-10-26: the DB row
# saibor_3m=3.905 equals the 2026-08-31 '13 weeks' cell, saibor_12m=4.103 the
# '52 weeks' cell.
_RATE_COLUMNS = (
    ("repo_rate",         "RR",           "Repo Rate (SAMA)"),
    ("reverse_repo_rate", "RRR",          "Reverse Repo Rate (SAMA)"),
    ("saibor_1w",         "Unnamed: 5",   "SAIBOR 1W"),
    ("saibor_1m",         "Unnamed: 6",   "SAIBOR 1M"),
    ("saibor_3m",         "Unnamed: 7",   "SAIBOR 3M"),
    ("saibor_6m",         "Unnamed: 8",   "SAIBOR 6M"),
    ("saibor_12m",        "Unnamed: 9",   "SAIBOR 12M"),
)
_CORE_RATE_KEYS = ("repo_rate", "reverse_repo_rate", "saibor_3m", "saibor_12m")

# The monthly block runs 2007-01..2026-08 (236 obs) in the Aug-2026 issue.
# Fewer rows than this means the walk off the bottom of the sheet hit the
# quarterly block or the layout moved — refuse rather than write a crippled
# series.
_MIN_MONTHLY_OBS = 120


# ──────────────────────────────────────────────────────────────────────────────
# DB Bootstrap
# ──────────────────────────────────────────────────────────────────────────────

def ensure_table_exists():
    try:
        SaudiEconomicIndicator.__table__.create(engine, checkfirst=True)
        logger.info("[DB] Table 'saudi_economic_indicators' checked/ready.")
    except Exception as e:
        logger.error(f"[DB] Error checking table: {e}")


def _schema_is_migrated() -> bool:
    """True when period is already DATE. The ORM would silently write ISO text
    into a DATE column otherwise, so refuse the write instead."""
    try:
        from sqlalchemy import inspect as sa_inspect
        cols = sa_inspect(engine).get_columns("saudi_economic_indicators")
        return any(c["name"] == "period" and "DATE" in str(c["type"]).upper()
                   for c in cols)
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# SAMA Bulletin — XLSX Download & Parse
# ──────────────────────────────────────────────────────────────────────────────

def _parse_decimal(val, default=None):
    if val is None or (isinstance(val, float) and pd.isna(val)) or val == "":
        return default
    try:
        if isinstance(val, str):
            val = val.replace(",", "").replace("%", "").strip()
        return round(float(val), 4)
    except Exception:
        return default


def _to_date(value) -> date:
    """Normalize anything the publishers send to a real date, or raise.
    Zero-substitution: no parsing rule may invent a plausible day."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.date()
    s = str(value or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), 1)
    m = re.match(r"^(\d{4})[ -]?Q([1-4])$", s, re.I)
    if m:
        import calendar
        end_month = (int(m.group(2)) - 1) * 3 + 3
        return date(int(m.group(1)), end_month,
                    calendar.monthrange(int(m.group(1)), end_month)[1])
    m = re.match(r"^(\d{4})$", s)
    if m:
        return date(int(m.group(1)), 12, 31)
    raise ValueError(f"unparseable publisher period: {value!r}")


class _BulletinUnreadable(Exception):
    """Raised with a human-readable reason when a download can't be parsed."""


# Measured on the live bulletin page (2026-10-23, read-only probe):
#   plain requests            -> SSLError  (SAMA's chain needs the OS trust store)
#   Scrapling HTTP            -> None      (curl_cffi ships its own CA store: curl 60)
#   Scrapling StealthyFetcher -> 200 / 136,066 chars / exactly one .xlsx href:
#       /en-US/Statistics/MonthlyStatistics/Monthly_Statistical_Bulletin_for_Aug_2026.xlsx
# So the browser is the only transport that reaches SAMA, and it replaces
# Selenium completely: one launch does both the discovery and the download,
# through the very session that passed the WAF (no cookie extraction).
_BULLETIN_DIR = "/en-US/Statistics/MonthlyStatistics/"
_MONTH_ABBR   = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _abs_url(href: str) -> str:
    return href if href.startswith("http") else SAMA_BASE + href


def _xlsx_hrefs(html: str) -> list:
    """Every .xlsx href on a SAMA page, in document order (newest first)."""
    return re.findall(r'href="([^"]+\.xlsx)"', html or "", re.I)


def _candidate_bulletin_urls(now=None, months_back: int = 4) -> list:
    """Build bulletin URLs from the measured naming pattern.

    Only reached when the rendered page exposes no link at all (the layout
    moved). The newest month label is tried first; `months_back` covers a
    release landing late in the month.
    """
    now = now or date.today()
    urls, y, m = [], now.year, now.month - 1
    for _ in range(months_back):
        if m <= 0:
            m, y = m + 12, y - 1
        urls.append(f"{SAMA_BASE}{_BULLETIN_DIR}Monthly_Statistical_Bulletin_"
                    f"for_{_MONTH_ABBR[m - 1]}_{y}.xlsx")
        m -= 1
    return urls


def _make_browser_action(sink: dict):
    """`page_action` callback: pull the XLSX down inside the browser session.

    Scrapling invokes `page_action(page)` synchronously right after navigation
    (engines/_browsers/_stealth.py), so Playwright's `page.request` reuses the
    stealth session's cookies and TLS trust. This is required because
    `FetchResult.text` is utf-8 decoded and can never carry a .xlsx binary.
    Result is written into `sink` = {content, url} or {error}.
    """
    def _action(page) -> None:
        try:
            hrefs = _xlsx_hrefs(page.content())
        except Exception as e:
            sink["error"] = f"DOM read failed: {type(e).__name__}"
            return
        for href in list(hrefs) + _candidate_bulletin_urls():
            url = _abs_url(href)
            try:
                body = page.request.get(url, timeout=120_000).body()
            except Exception as e:
                sink["error"] = f"{type(e).__name__}: {str(e)[:120]}"
                continue
            if body[:2] == b"PK":  # an xlsx is a zip; anything else is an HTML error page
                sink["content"], sink["url"] = body, url
                return
            sink["error"] = f"not an xlsx ({len(body)} bytes, magic {body[:4]!r})"

    return _action


def _download_bulletin_via_browser() -> tuple:
    """(content, url) fetched through the stealth browser, else (None, None)."""
    sink: dict = {}
    res = browser_get(SAMA_PAGE_URL, wait_selector="a", timeout_ms=120_000,
                      extra_kwargs={"page_action": _make_browser_action(sink)})
    if res is None:
        logger.warning("[SAMA] stealth browser could not load the bulletin page")
        return None, None
    if sink.get("content"):
        return sink["content"], sink["url"]
    logger.warning(f"[SAMA] page loaded via {res.via} but no bulletin binary: "
                   f"{sink.get('error', 'page_action did not run')}")
    return None, None


def _download_bulletin_via_requests() -> tuple:
    """Last resort: plain `requests`, which validates SAMA's chain where
    curl_cffi's bundled CA store cannot. Returns (content, url) or (None, None)."""
    hrefs: list = []
    try:
        page_res = requests.get(SAMA_PAGE_URL, headers=HEADERS, timeout=60)
        hrefs = _xlsx_hrefs(page_res.text)
    except Exception as e:
        logger.warning(f"[SAMA] requests discovery failed: "
                       f"{type(e).__name__}: {str(e)[:140]}")

    for href in list(hrefs) + _candidate_bulletin_urls():
        url = _abs_url(href)
        try:
            r = requests.get(url, headers={**HEADERS, "Referer": SAMA_PAGE_URL,
                                           "Accept": "application/octet-stream,*/*"},
                             timeout=120)
            r.raise_for_status()
        except Exception as e:
            logger.debug(f"[SAMA] requests download {url[-60:]}: {type(e).__name__}")
            continue
        if r.content[:2] == b"PK":
            logger.info("[SAMA] bulletin downloaded via requests (browser unavailable)")
            return r.content, url
        logger.warning(f"[SAMA] {url[-60:]} is not an xlsx ({len(r.content)} bytes)")
    return None, None


def _download_bulletin() -> tuple:
    content, url = _download_bulletin_via_browser()
    if content:
        return content, url
    return _download_bulletin_via_requests()


def _extract_monthly_block(df: pd.DataFrame) -> pd.DataFrame:
    """Walk backwards from the last row over month-end Period timestamps.

    Sheet '5-6' stacks three blocks with no marker between them: annual (int
    years / 31-Dec dates), quarterly (Period='Q1'..'Q4' with the year in
    'Unnamed: 1') and monthly (Period = month-end timestamp). The monthly block
    is the tail, so a backwards walk that stops at the first non-month-end row
    isolates it. Strict decrease guards against a section that repeats headers
    or restarts numbering.
    """
    period = df["Period"] if "Period" in df.columns else pd.Series([None] * len(df))
    idx = len(df) - 1
    picked = []
    prev = None
    while idx >= 0:
        p = period.iloc[idx]
        if not isinstance(p, (datetime, pd.Timestamp)):
            break
        cur = pd.Timestamp(p)
        if cur.day not in (28, 29, 30, 31):
            break
        if prev is not None and cur >= prev:
            break
        picked.append(idx)
        prev = cur
        idx -= 1
    picked.reverse()
    return df.iloc[picked]


def _parse_sama_series(content: bytes) -> dict:
    """Parse sheet '5-6' into the FULL monthly history for each rate key.

    Returns {indicator_key: [{period: date, value: float, is_fallback: False},
                              ... oldest -> newest ...]}.
    A column that is missing or entirely empty yields [] — the caller decides
    whether that means "write nothing" (core rates) or "skip this key".
    Raises _BulletinUnreadable when the sheet lost its shape, so the caller
    reports an explicit reason instead of silently serving fallback numbers.
    """
    xls = pd.ExcelFile(BytesIO(content), engine="openpyxl")
    if "5-6" not in xls.sheet_names:
        raise _BulletinUnreadable("Sheet '5-6' not present in downloaded XLSX")

    df = pd.read_excel(xls, sheet_name="5-6", header=8)
    df = df.iloc[:337].dropna(how="all").reset_index(drop=True)

    monthly = _extract_monthly_block(df)
    if len(monthly) < _MIN_MONTHLY_OBS:
        raise _BulletinUnreadable(
            f"monthly block too short ({len(monthly)} rows, floor "
            f"{_MIN_MONTHLY_OBS}) — sheet 5-6 layout may have changed")

    series: dict = {}
    for key, col, name in _RATE_COLUMNS:
        obs = []
        if col in monthly.columns:
            for _, row in monthly.iterrows():
                val = _parse_decimal(row[col])
                if val is None:
                    continue
                try:
                    p = _to_date(row["Period"])
                except ValueError:
                    continue  # never guess a date
                obs.append({"period": p, "value": val, "is_fallback": False})
        series[key] = obs
        logger.info(f"[SAMA-XLSX] {name}: {len(obs)} monthly observations "
                    f"({obs[0]['period']} .. {obs[-1]['period']})"
                    if obs else f"[SAMA-XLSX] {name}: column '{col}' empty/missing")

    if not any(series.get(k) for k in _CORE_RATE_KEYS):
        raise _BulletinUnreadable(
            "core rate columns (RR/RRR/Unnamed: 7/Unnamed: 9) all empty — "
            "XLSX layout may have changed")
    return series


def fetch_saibor_from_sama_bulletin(history: bool = True):
    """
    Downloads the SAMA Monthly Bulletin XLSX with Scrapling's StealthyFetcher
    (the SAMA WAF plus its TLS chain defeat requests/curl_cffi) and parses sheet
    '5-6' straight from memory.

    Returns:
        (series, error) — `series` is _parse_sama_series()'s output, or a
        single-observation fallback set built from the verified constants when
        the bulletin is unreachable (error explains why). series is None when
        even that is impossible.
    """
    logger.info("[SAMA] Discovering & downloading SAMA Monthly Bulletin via Scrapling...")
    try:
        content, url = _download_bulletin()
        if not content:
            return None, "SAMA bulletin unreachable (stealth browser and requests both failed)"
        logger.info(f"[SAMA] Downloaded {len(content):,} bytes: {url[-80:]}")
        series = _parse_sama_series(content)
        if not history:
            series = {k: v[-1:] for k, v in series.items() if v}
        return series, None
    except _BulletinUnreadable as e:
        return None, str(e)
    except Exception as e:
        logger.error(f"[SAMA] Bulletin fetch error: {e}")
        return None, f"Exception during SAMA fetch: {e}"


def _fallback_rate_series(reason: str) -> dict:
    """Newest observation only, from the verified constants. Dated with the
    period those constants were measured for (2026-07-31) — the honest label,
    not the run month."""
    stamped = {
        "repo_rate":         _FALLBACK_REPO_RATE,
        "reverse_repo_rate": _FALLBACK_REVERSE_REPO,
        "saibor_3m":         _FALLBACK_SAIBOR_3M,
        "saibor_12m":        _FALLBACK_SAIBOR_12M,
    }
    return {
        key: [{"period": date(2026, 7, 31), "value": val, "is_fallback": True,
               "reason": reason}]
        for key, val in stamped.items()
    }


# ──────────────────────────────────────────────────────────────────────────────
# GDP Fetch — KAPSARC API (full TTM history from the returned quarters)
# ──────────────────────────────────────────────────────────────────────────────

def _quarter_end(year: int, quarter: int) -> date:
    import calendar
    m = quarter * 3
    return date(year, m, calendar.monthrange(year, m)[1])


def _extract_gdp_quarter(rec: dict):
    """(year, quarter, value) from one KAPSARC record; None when unusable.

    Measured shape (2026-10-26, 64/64 rows identical):
        {"year": "2025", "quarter": "Q4", "date": "2025-12",
         "gdp": 1210405.0, "unit": "Million of Saudi Riyals",
         "economic_activity": "Gross Domestic Product"}
    The `date` component carries the quarter's END month, so the quarter comes
    straight from it; year/quarter are the fallback if a future row ever loses
    the date field.
    """
    gdp = rec.get("gdp")
    if gdp is None:
        return None
    d = str(rec.get("date") or "")
    m = re.match(r"^(\d{4})-(\d{2})", d)
    if m:
        y, end_month = int(m.group(1)), int(m.group(2))
    else:
        m = re.match(r"^(\d{4})$", str(rec.get("year") or ""))
        qm = re.search(r"([1-4])", str(rec.get("quarter") or ""))
        if not (m and qm):
            return None
        y, end_month = int(m.group(1)), int(qm.group(1)) * 3
    if not 3 <= end_month <= 12 or (end_month - 3) % 3:
        return None  # not a quarter-end month -> no defensible business date
    return y, end_month // 3, float(gdp)


def fetch_saudi_gdp() -> list:
    """
    GDP at current prices from KAPSARC, as a list of TTM observations.

    The dataset holds every published quarter (measured: 64 rows, 2010-Q1 ..
    2025-Q4). Each position with 4 consecutive quarters behind it becomes one
    trailing-12-month observation dated at that quarter's end, so one run
    lands the whole series — the YoY pair GET /api/rebh/market-macro needs has
    existed since the first run instead of after two.

    If fewer than 4 quarters are usable, a single Annualized row (latest
    quarter × 4) is written instead — marked basis='Annualized' so rebh_engine's
    YoY derivation never mixes bases.
    """
    logger.info("[GDP] Fetching Saudi GDP at Current Prices from KAPSARC...")
    try:
        res = requests.get(KAPSARC_GDP_API, headers=HEADERS, timeout=30)
        if res.status_code == 200:
            pairs = []
            for rec in res.json().get("results", []):
                p = _extract_gdp_quarter(rec)
                if p:
                    pairs.append(p)
            pairs.sort(key=lambda t: (t[0], t[1]))
            # A trailing-12M window is 4 CONSECUTIVE quarters ending at index
            # i+3 (that end quarter is the observation's business date — using
            # the oldest would back-date every point by 3 quarters and mis-pair
            # the YoY derivation). Consecutive means flat index increments by 1,
            # so a missing published quarter cannot make one window sum across a
            # gap (2024Q2 + 2024Q4 + …).
            flat = [y * 4 + (q - 1) for y, q, _ in pairs]
            ttm = []
            for i in range(len(pairs) - 3):
                if flat[i + 3] - flat[i] != 3:
                    continue  # not 4 back-to-back quarters — skip the window
                y_end, q_end, _ = pairs[i + 3]
                ttm.append({
                    "period": _quarter_end(y_end, q_end),
                    "value": round(sum(v for _, _, v in pairs[i:i + 4]), 1),
                    "basis": "TTM", "is_fallback": False})
            if ttm:
                logger.info(f"[GDP] {len(ttm)} TTM observations "
                            f"({ttm[0]['period']} .. {ttm[-1]['period']}, "
                            f"{ttm[-1]['value']:,.0f} M SAR)")
                return ttm
            if pairs:  # 1-3 quarters only: annualize, never mix with TTM rows
                y, q, v = pairs[-1]
                logger.warning(f"[GDP] only {len(pairs)} quarter(s) returned — "
                               f"writing Annualized (1Q*4)")
                return [{"period": _quarter_end(y, q), "value": round(v * 4, 1),
                         "basis": "Annualized", "is_fallback": False}]
    except Exception as e:
        logger.warning(f"[GDP] KAPSARC API query failed: {e}")

    logger.warning(f"[GDP] Using fallback GDP: {_FALLBACK_GDP_M_SAR:,.0f} M SAR "
                   f"dated {_FALLBACK_GDP_PERIOD}")
    return [{
        "period":      _FALLBACK_GDP_PERIOD,
        "value":       _FALLBACK_GDP_M_SAR,
        "basis":       "TTM",
        "is_fallback": True,
        "reason":      "KAPSARC API returned no usable GDP row",
    }]


# ──────────────────────────────────────────────────────────────────────────────
# Unemployment Fetch — GaStat Open Data
# ──────────────────────────────────────────────────────────────────────────────

def fetch_unemployment() -> dict:
    """
    Saudi unemployment rate from GaStat Open Data, then KAPSARC's Labour Force
    Survey mirror. The publisher's own date is required: an API row whose
    period will not parse is skipped, never stamped with the run month.
    Returns: { unemployment_pct, period(date), is_fallback, source_status, reason? }
    """
    logger.info("[UNEMP] Fetching Saudi unemployment from GaStat Open Data...")
    try:
        res = requests.get(GASTAT_UNEMPLOYMENT_API, headers=HEADERS, timeout=15)
        if res.status_code == 200:
            for r in res.json().get("results", []):
                val = (
                    r.get("saudi_unemployment_rate") or
                    r.get("unemployment_rate") or
                    r.get("total_unemployment_rate") or
                    r.get("value")
                )
                if val is None:
                    continue
                pct = float(val)
                if not 0 < pct < 50:
                    continue
                try:
                    p = _to_date(r.get("date") or r.get("period"))
                except ValueError:
                    logger.warning(f"[UNEMP] GaStat row with unparseable period "
                                   f"{r.get('date')!r} — skipped")
                    continue
                logger.info(f"[UNEMP] Live GaStat unemployment: {pct}% — {p}")
                return {"unemployment_pct": pct, "period": p,
                        "is_fallback": False, "source_status": "live"}
    except Exception as e:
        logger.debug(f"[UNEMP] GaStat Open Data unavailable ({e}), "
                     f"trying KAPSARC Labor Force Survey...")

    try:
        logger.info("[UNEMP] Fetching Saudi unemployment from KAPSARC Labor Force Survey...")
        res = requests.get(KAPSARC_UNEMPLOYMENT_API, headers=HEADERS, timeout=15)
        if res.status_code == 200:
            results = res.json().get("results", [])
            if results:
                r = results[0]
                val = r.get("value")
                if val is not None:
                    pct = float(val)
                    try:
                        p = _to_date(r.get("time_period") or r.get("year"))
                    except ValueError:
                        logger.warning(f"[UNEMP] KAPSARC row with unparseable "
                                       f"period {r.get('time_period')!r} — skipped")
                    else:
                        logger.info(f"[UNEMP] Live KAPSARC/GaStat unemployment: "
                                    f"{pct}% — {p}")
                        return {"unemployment_pct": pct, "period": p,
                                "is_fallback": False, "source_status": "live"}
    except Exception as e:
        logger.warning(f"[UNEMP] KAPSARC unemployment API query failed: {e}")

    logger.warning(f"[UNEMP] Using fallback unemployment: "
                   f"{_FALLBACK_UNEMPLOY_PCT}% dated {_FALLBACK_UNEMP_PERIOD}")
    return {
        "unemployment_pct": _FALLBACK_UNEMPLOY_PCT,
        "period":           _FALLBACK_UNEMP_PERIOD,
        "is_fallback":      True,
        "source_status":    "fallback",
        "reason":           "All unemployment APIs returned no usable dated row",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Buffett Indicator Calculation
# ──────────────────────────────────────────────────────────────────────────────

def calculate_buffett_indicator(gdp_m_sar: float) -> dict:
    """
    Saudi Buffett Indicator = TASI Total Market Cap / Nominal GDP × 100

    Phase 5 rule: Do not attach buy/sell wording to the indicator.
    The indicator is a methodological reference only — not a trading signal.
    """
    try:
        from app.models.price import Price
        from sqlalchemy import func
        db = SessionLocal()
        try:
            latest_dt = db.query(func.max(Price.date)).scalar()
            total_raw = db.query(func.sum(Price.market_cap)).filter(Price.date == latest_dt).scalar()
            if total_raw and float(total_raw) > 0:
                total_mc = round(float(total_raw) / 1_000_000.0, 1)  # Convert SAR to M SAR
                mc_source = f"prices_table_live_{latest_dt}"
            else:
                total_mc = 9500000.0
                mc_source = "fallback_constant"
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[BUFFETT] Could not fetch live market cap: {e}")
        total_mc  = 9500000.0  # ~9.5T SAR TASI market cap fallback
        mc_source = "fallback_constant"

    ratio = (total_mc / gdp_m_sar) * 100.0 if gdp_m_sar > 0 else 0.0

    # Interpretation zones — neutral framing only (per Phase 5 rule)
    if ratio < 75.0:       zone = "Zone-1: Below Historical Average"
    elif ratio < 95.0:     zone = "Zone-2: Near Historical Average"
    elif ratio < 115.0:    zone = "Zone-3: At Historical Average"
    elif ratio < 140.0:    zone = "Zone-4: Above Historical Average"
    else:                  zone = "Zone-5: Significantly Above Historical Average"

    return {
        "tasi_market_cap_m_sar": round(total_mc, 1),
        "gdp_m_sar":             round(gdp_m_sar, 1),
        "buffett_ratio_pct":     round(ratio, 1),
        "zone":                  zone,
        "mc_source":             mc_source,
    }


# ──────────────────────────────────────────────────────────────────────────────
# DB Upsert — Historical Preservation
# ──────────────────────────────────────────────────────────────────────────────

_UPSERT_SQL = sa_text("""
    INSERT INTO saudi_economic_indicators
        (indicator_key, indicator_name, value, raw_value, unit, period, basis,
         frequency, source, source_url, is_fallback, retrieved_at, updated_at)
    VALUES
        (:indicator_key, :indicator_name, :value, :raw_value, :unit, :period,
         :basis, :frequency, :source, :source_url, :is_fallback, now(), now())
    ON CONFLICT (indicator_key, period, source) DO UPDATE SET
        indicator_name = EXCLUDED.indicator_name,
        value          = EXCLUDED.value,
        raw_value      = EXCLUDED.raw_value,
        unit           = EXCLUDED.unit,
        basis          = EXCLUDED.basis,
        frequency      = EXCLUDED.frequency,
        source_url     = EXCLUDED.source_url,
        is_fallback    = EXCLUDED.is_fallback,
        updated_at     = now()
""")


def _observation_params(key: str, meta: dict, obs: dict) -> dict:
    value = obs.get("value")
    return {
        "indicator_key":  key,
        "indicator_name": meta.get("name") or key,
        "value":          value,
        "raw_value":      str(value) if value is not None else None,
        "unit":           meta.get("unit"),
        "period":         obs["period"],
        "basis":          obs.get("basis") or meta.get("basis"),
        "frequency":      meta.get("frequency", "Monthly"),
        "source":         meta["source"],
        "source_url":     meta.get("source_url"),
        "is_fallback":    bool(obs.get("is_fallback", False)),
    }


def save_economic_indicators_to_db(observations: dict) -> int:
    """Bulk-upsert {indicator_key: meta + 'observations': [...]} into
    saudi_economic_indicators.

    ON CONFLICT (indicator_key, period, source) DO UPDATE: re-running heals a
    previously wrong value for the same business date instead of duplicating
    it (the old per-row ORM loop did the same thing in 236 round-trips).
    Returns the number of rows written; raises if the table has not been
    migrated to DATE periods yet.
    """
    if not _schema_is_migrated():
        raise RuntimeError(
            "saudi_economic_indicators.period is not a DATE column — run "
            "scripts/migrate_macro_period_date.py --execute first (the ORM "
            "would write text into a date column otherwise)")
    ensure_table_exists()
    written = 0
    db = SessionLocal()
    try:
        for key, meta in observations.items():
            rows = meta.get("observations") or []
            if not rows:
                continue
            for obs in rows:
                db.execute(_UPSERT_SQL, _observation_params(key, meta, obs))
                written += 1
            logger.info(f"[DB] {key}: {len(rows)} observations upserted.")
        db.commit()
        logger.info(f"[DB] Saved/Updated {written} Saudi economic indicator rows.")
    except Exception as e:
        db.rollback()
        logger.error(f"[DB] Error saving economic indicators: {e}")
        raise
    finally:
        db.close()
    return written


# ──────────────────────────────────────────────────────────────────────────────
# CSV export (dry-run verification without touching the database)
# ──────────────────────────────────────────────────────────────────────────────

def export_observations_csv(observations: dict, path: str) -> int:
    n = 0
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("indicator_key,period,basis,value,unit,frequency,source,"
                 "is_fallback,reason\n")
        for key, meta in observations.items():
            for obs in (meta.get("observations") or []):
                reason = (obs.get("reason") or "").replace("\n", " ")
                fh.write(f"{key},{obs['period']},{obs.get('basis') or ''},"
                         f"{obs['value']},{meta.get('unit') or ''},"
                         f"{meta.get('frequency', 'Monthly')},{meta['source']},"
                         f"{int(bool(obs.get('is_fallback')))},{reason}\n")
                n += 1
    return n


# ──────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ──────────────────────────────────────────────────────────────────────────────

def run_economic_sync(write_db: bool = True, history: bool = True) -> dict:
    """
    Full macroeconomic sync: SAMA + GaStat + Buffett Indicator.

    Returns {observations: {...}, written: int, degraded: [str], summary: {...}}
    Callers (daily_market_update.py) only require it not to raise; degraded is
    non-empty when a source was unreadable or fell back.
    """
    logger.info("=== Starting SAMA & GaStat Macroeconomic Sync ===")
    degraded: list = []
    observations: dict = {}

    # ── 1. SAMA Bulletin: full monthly rate history ──
    series, sama_error = fetch_saibor_from_sama_bulletin(history=history)
    if sama_error:
        logger.warning(f"[SAMA] {sama_error} — writing dated fallback "
                       f"for the newest observation only")
        degraded.append(f"sama_rates: {sama_error}")
        series = _fallback_rate_series(sama_error)
    for key, col, name in _RATE_COLUMNS:
        obs = series.get(key) or []
        if not obs:
            if key in _CORE_RATE_KEYS:
                degraded.append(f"{key}: no observations in sheet 5-6 — not written")
            continue
        observations[key] = {
            "name":        name,
            "unit":        "%",
            "frequency":   "Monthly",
            "source":      SOURCE_SAMA,
            "source_url":  SAMA_PAGE_URL,
            "observations": obs,
        }

    # ── 2. GDP (TTM history) ──
    gdp_obs = fetch_saudi_gdp()
    observations["saudi_gdp_annual"] = {
        "name":         "Saudi GDP at Current Prices (TTM)",
        "unit":         "M SAR",
        "frequency":    "Quarterly",
        "source":       SOURCE_GASTAT,
        "source_url":   KAPSARC_GDP_API,
        "observations": gdp_obs,
    }
    if gdp_obs and gdp_obs[-1].get("is_fallback"):
        degraded.append("gdp: KAPSARC unusable — fallback constant written, "
                        f"dated {gdp_obs[-1]['period']}")

    # ── 3. Unemployment ──
    unemp = fetch_unemployment()
    observations["saudi_unemployment"] = {
        "name":        "Saudi Unemployment Rate",
        "unit":        "%",
        "frequency":   "Quarterly",
        "source":      SOURCE_GASTAT_LFS,
        "source_url":  GASTAT_UNEMPLOYMENT_API,
        "observations": [{
            "period":      unemp["period"],
            "value":       unemp["unemployment_pct"],
            "is_fallback": unemp["is_fallback"],
            "reason":      unemp.get("reason"),
        }],
    }
    if unemp["is_fallback"]:
        degraded.append(f"unemployment: {unemp.get('reason')} — fallback written, "
                        f"dated {unemp['period']}")

    # ── 4. Buffett Indicator — current snapshot only, never backfilled ──
    gdp_latest = gdp_obs[-1] if gdp_obs else None
    if gdp_latest and gdp_latest["value"]:
        buffett = calculate_buffett_indicator(gdp_latest["value"])
        observations["saudi_buffett_indicator"] = {
            "name":       "Saudi Buffett Indicator (TASI / GDP)",
            "unit":       "%",
            "frequency":  "Quarterly",
            "source":     SOURCE_REBH,
            "basis":      gdp_latest.get("basis"),
            "observations": [{
                "period":      gdp_latest["period"],
                "value":       buffett["buffett_ratio_pct"],
                "is_fallback": bool(gdp_latest.get("is_fallback")),
            }],
        }
        logger.info(f"[BUFFETT] {buffett['buffett_ratio_pct']}% "
                    f"({buffett['zone']}, mc={buffett['mc_source']})")
    else:
        degraded.append("saudi_buffett_indicator: no GDP reference — not written")

    # ── Summary log ──
    for key, meta in observations.items():
        obs = meta["observations"]
        newest = obs[-1]
        flag = "[FALLBACK]" if newest.get("is_fallback") else "[LIVE]"
        logger.info(f"[PAYLOAD] {flag} {key}: {newest['value']} {meta.get('unit')} "
                    f"@ {newest['period']} ({len(obs)} obs)")

    written = 0
    if write_db:
        written = save_economic_indicators_to_db(observations)
    else:
        logger.info("[DRY] --execute not given — nothing written to the database.")

    logger.info("=== Finished SAMA & GaStat Macroeconomic Sync ===")
    return {
        "observations": observations,
        "written": written,
        "degraded": degraded,
        "summary": {k: len(m["observations"]) for k, m in observations.items()},
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="SAMA/GaStat macro sync — dry run by default")
    ap.add_argument("--execute", action="store_true",
                    help="write the observations to the database")
    ap.add_argument("--no-history", action="store_true",
                    help="write only the newest observation per series")
    ap.add_argument("--bulletin-file", metavar="XLSX",
                    help="parse a local bulletin XLSX instead of downloading "
                         "(offline dry runs)")
    ap.add_argument("--csv-out", metavar="CSV",
                    help="also dump every observation to a CSV file")
    ap.add_argument("--quiet", action="store_true", help="warnings and errors only")
    return ap


if __name__ == "__main__":
    args = _build_arg_parser().parse_args()
    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    if args.bulletin_file:
        # Offline mode: monkey-patch the downloader so the normal code path runs.
        content = open(args.bulletin_file, "rb").read()
        _download_bulletin = lambda: (content, args.bulletin_file)  # noqa: E731

    # job_runs is only written for real runs: a dry run must not make the
    # importers health page claim the macro sync just succeeded.
    if args.execute:
        from app.services.job_tracking import track_job
        with track_job("macro_importer", "manual") as run:
            res = run_economic_sync(write_db=True, history=not args.no_history)
            run.rows_written = res["written"]
    else:
        res = run_economic_sync(write_db=False, history=not args.no_history)

    if args.csv_out:
        n = export_observations_csv(res["observations"], args.csv_out)
        print(f"[CSV] {n} observations -> {args.csv_out}")

    print(f"\n[{'SUCCESS' if args.execute else 'DRY RUN'}] Macro Sync — "
          f"{res['written'] if args.execute else sum(res['summary'].values())} "
          f"rows {'written' if args.execute else 'ready'}")
    for key, count in sorted(res["summary"].items()):
        obs = res["observations"][key]["observations"]
        flag = "FALLBACK" if obs[-1].get("is_fallback") else "LIVE"
        print(f"  [{flag}] {key:<26} {obs[-1]['value']:>12} @ {obs[-1]['period']}"
              f"  ({count} obs)")
    for d in res["degraded"]:
        print(f"  [DEGRADED] {d}")
    if not args.execute:
        print("\nAdd --execute to write. Order matters: run "
              "scripts/migrate_macro_period_date.py --execute first if "
              "period is still a varchar column.")
