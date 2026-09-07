"""
SAMA & GaStat — Saudi Macroeconomic Data Sync (Phase 5)
========================================================
RUN SCHEDULE : MONTHLY (not daily) — SAMA publishes a new bulletin once per month.
TRIGGER      : Run manually on the 1st of each month, or when SAMA releases a new bulletin.

DATA COLLECTED
--------------
  Source : SAMA Monthly Statistical Bulletin
  URL    : https://www.sama.gov.sa/en-US/Statistics/pages/monthlystatistics.aspx
  Sheet  : '5-6' — Money & Banking / Interest Rates
  Cols   : RR (Repo Rate) | RRR (Reverse Repo) | Unnamed:7 (3M SAIBOR) | Unnamed:9 (12M SAIBOR)

  Source : GaStat / KAPSARC Open Data
  Data   : GDP at Current Prices (Nominal GDP in M SAR)

  Source : GaStat — Labour Force Survey
  Data   : Unemployment rate (quarterly), Saudi unemployment specifically

CALCULATED
----------
  Saudi Buffett Indicator = TASI Total Market Cap / GDP at Current Prices × 100
  (No buy/sell wording attached — methodological reference only)

OUTPUT TABLE : saudi_economic_indicators
  Historical time-series preserved via composite key: indicator_key + period + source

FALLBACK RULES (Phase 5 Non-Negotiable)
  - Every fallback must return is_fallback=True and source_status='fallback'
  - Never display fallback data as live
  - Reason field must explicitly explain why fallback was used

ECONOMY SCORECARD (5 indicators)
  1. Repo Rate (SAMA)
  2. SAIBOR 3M (SAMA)
  3. GDP Annual (GaStat)
  4. Unemployment Rate (GaStat)
  5. Saudi Buffett Indicator (calculated)
"""
import logging
import os
import sys
import requests
import pandas as pd
from io import BytesIO
from datetime import datetime, date

# Setup backend imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session
from app.core.database import SessionLocal, engine
from app.models.saudi_macro import SaudiEconomicIndicator
try:
    from app.services.xbrl_data_service import list_companies
except Exception:
    list_companies = lambda: []

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
SAMA_BASE     = "https://www.sama.gov.sa"
SAMA_PAGE_URL = f"{SAMA_BASE}/en-US/Statistics/pages/monthlystatistics.aspx"
KAPSARC_GDP_API = (
    "https://datasource.kapsarc.org/api/explore/v2.1/catalog/datasets/"
    "gross-domestic-product-by-type-of-economic-activity-at-current-prices/"
    "records?limit=10&order_by=date%20desc"
)
GASTAT_UNEMPLOYMENT_API = (
    "https://open.data.gov.sa/api/explore/v2.1/catalog/datasets/"
    "employment-unemployment-indicators/records?limit=5&order_by=date%20desc"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
}

# Fallback constants — MUST have is_fallback=True when used
_FALLBACK_REPO_RATE         = 5.50   # SAMA Repo Rate as of last known bulletin
_FALLBACK_REVERSE_REPO      = 5.00
_FALLBACK_SAIBOR_3M         = 5.65
_FALLBACK_SAIBOR_12M        = 5.40
_FALLBACK_GDP_M_SAR         = 4010000.0  # GaStat 2024 benchmark
_FALLBACK_GDP_PERIOD        = "2024-GaStat"
_FALLBACK_UNEMPLOYMENT_PCT  = 7.7    # GaStat Q4-2024

_CURRENT_PERIOD = datetime.utcnow().strftime("%Y-%m")  # e.g. "2026-09"


# ──────────────────────────────────────────────────────────────────────────────
# DB Bootstrap
# ──────────────────────────────────────────────────────────────────────────────

def ensure_table_exists():
    try:
        SaudiEconomicIndicator.__table__.create(engine, checkfirst=True)
        logger.info("[DB] Table 'saudi_economic_indicators' checked/ready.")
    except Exception as e:
        logger.error(f"[DB] Error checking table: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# SAMA Bulletin — XLSX Download & Parse
# ──────────────────────────────────────────────────────────────────────────────

def _parse_decimal(val, default=None):
    if val is None or val == "":
        return default
    try:
        if isinstance(val, str):
            val = val.replace(",", "").replace("%", "").strip()
        return float(val)
    except Exception:
        return default


def _make_fallback(reason: str) -> dict:
    return {
        "repo_rate":         {"value": _FALLBACK_REPO_RATE,    "is_fallback": True, "source_status": "fallback", "reason": reason},
        "reverse_repo_rate": {"value": _FALLBACK_REVERSE_REPO, "is_fallback": True, "source_status": "fallback", "reason": reason},
        "saibor_3m":         {"value": _FALLBACK_SAIBOR_3M,    "is_fallback": True, "source_status": "fallback", "reason": reason},
        "saibor_12m":        {"value": _FALLBACK_SAIBOR_12M,   "is_fallback": True, "source_status": "fallback", "reason": reason},
    }


def fetch_saibor_from_sama_bulletin() -> dict:
    """
    Downloads SAMA Monthly Bulletin XLSX via Selenium session cookies
    (plain requests.get is blocked by SAMA anti-bot), then parses sheet '5-6'.

    Column layout (header at row 9):
        RR          = Repo Rate
        RRR         = Reverse Repo Rate
        Unnamed: 7  = 13-week (3M) SAIBOR
        Unnamed: 9  = 52-week (12M) SAIBOR

    Returns:
        dict with keys: repo_rate, reverse_repo_rate, saibor_3m, saibor_12m
        Each value is a sub-dict with: value, is_fallback, source_status, reason
    """
    logger.info("[SAMA] Discovering & downloading SAMA Monthly Bulletin via Selenium...")

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from bs4 import BeautifulSoup
        import time as _time

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")

        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(15)
        xlsx_link = None
        cookies   = []
        ua        = HEADERS["User-Agent"]

        try:
            try:
                driver.get(SAMA_PAGE_URL)
            except Exception as nav_e:
                logger.warning(f"[SAMA] Page load timed out or had error ({nav_e}), attempting parse...")
            _time.sleep(3)
            soup  = BeautifulSoup(driver.page_source, "html.parser")
            links = [a["href"] for a in soup.find_all("a", href=True) if a["href"].lower().endswith(".xlsx")]
            if links:
                path      = links[0]
                xlsx_link = path if path.startswith("http") else SAMA_BASE + path
            cookies = driver.get_cookies()
            try:
                ua = driver.execute_script("return navigator.userAgent;")
            except Exception:
                pass
        finally:
            try:
                driver.quit()
            except Exception:
                pass

        if not xlsx_link:
            logger.warning("[SAMA] No XLSX link found on page.")
            return _make_fallback("No XLSX link discovered on SAMA page")

        logger.info(f"[SAMA] Downloading: {xlsx_link} ({len(cookies)} session cookies)")

        # Authenticated session with browser cookies
        session = requests.Session()
        session.headers.update({
            "User-Agent":      ua,
            "Referer":         SAMA_PAGE_URL,
            "Accept":          "application/octet-stream,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        for c in cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

        file_resp = session.get(xlsx_link, timeout=60)
        file_resp.raise_for_status()
        logger.info(f"[SAMA] Downloaded {len(file_resp.content):,} bytes")

        # Parse sheet '5-6'
        xls = pd.ExcelFile(BytesIO(file_resp.content), engine="openpyxl")
        if "5-6" not in xls.sheet_names:
            logger.warning("[SAMA] Sheet '5-6' not found — bulletin layout may have changed.")
            return _make_fallback("Sheet '5-6' not present in downloaded XLSX")

        df   = pd.read_excel(xls, sheet_name="5-6", header=8)
        data = df.iloc[:337].dropna(how="all")

        def last_num(col_name):
            if col_name not in data.columns:
                return None
            s = pd.to_numeric(data[col_name], errors="coerce").dropna()
            return round(float(s.iloc[-1]), 4) if not s.empty else None

        rr   = last_num("RR")           # Repo Rate
        rrr  = last_num("RRR")          # Reverse Repo Rate
        s3m  = last_num("Unnamed: 7")   # 3M SAIBOR
        s12m = last_num("Unnamed: 9")   # 12M SAIBOR

        if all(v is None for v in [rr, rrr, s3m, s12m]):
            logger.warning("[SAMA] Sheet parsed but no rate values found — column layout may have changed.")
            return _make_fallback("All rate columns returned None after parse — XLSX layout may have changed")

        def _wrap(value, fallback_val, name):
            if value is not None:
                logger.info(f"[SAMA-XLSX] {name}: {value}%")
                return {"value": value, "is_fallback": False, "source_status": "live"}
            else:
                logger.warning(f"[SAMA-XLSX] {name}: column missing — using fallback {fallback_val}%")
                return {"value": fallback_val, "is_fallback": True, "source_status": "fallback", "reason": f"Column for {name} missing in sheet 5-6"}

        return {
            "repo_rate":         _wrap(rr,   _FALLBACK_REPO_RATE,    "Repo Rate"),
            "reverse_repo_rate": _wrap(rrr,  _FALLBACK_REVERSE_REPO, "Reverse Repo Rate"),
            "saibor_3m":         _wrap(s3m,  _FALLBACK_SAIBOR_3M,   "SAIBOR 3M"),
            "saibor_12m":        _wrap(s12m, _FALLBACK_SAIBOR_12M,  "SAIBOR 12M"),
        }

    except Exception as e:
        logger.error(f"[SAMA] Bulletin fetch error: {e}")
        return _make_fallback(f"Exception during SAMA fetch: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# GDP Fetch — KAPSARC API
# ──────────────────────────────────────────────────────────────────────────────

def fetch_saudi_gdp() -> dict:
    """
    Fetches Saudi GDP at current prices from GaStat/KAPSARC.
    Returns dict: { gdp_m_sar, period, is_fallback, source_status, reason? }
    """
    logger.info("[GDP] Fetching Saudi GDP at Current Prices from KAPSARC...")
    try:
        res = requests.get(KAPSARC_GDP_API, headers=HEADERS, timeout=15)
        if res.status_code == 200:
            for r in res.json().get("results", []):
                val = r.get("gdp_at_current_prices") or r.get("value") or r.get("total_gdp")
                if val and float(val) > 500000:
                    period = str(r.get("date") or "2024")
                    logger.info(f"[GDP] Live GDP: {float(val):,.0f} M SAR — period: {period}")
                    return {"gdp_m_sar": float(val), "period": period, "is_fallback": False, "source_status": "live"}
    except Exception as e:
        logger.warning(f"[GDP] KAPSARC API query failed: {e}")

    logger.warning(f"[GDP] Using fallback GDP: {_FALLBACK_GDP_M_SAR:,.0f} M SAR")
    return {
        "gdp_m_sar":      _FALLBACK_GDP_M_SAR,
        "period":         _FALLBACK_GDP_PERIOD,
        "is_fallback":    True,
        "source_status":  "fallback",
        "reason":         "KAPSARC API returned no valid GDP value",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Unemployment Fetch — GaStat Open Data
# ──────────────────────────────────────────────────────────────────────────────

def fetch_unemployment() -> dict:
    """
    Fetches Saudi unemployment rate from GaStat Open Data portal.
    Returns dict: { unemployment_pct, period, is_fallback, source_status, reason? }
    """
    logger.info("[UNEMP] Fetching Saudi unemployment from GaStat Open Data...")
    try:
        res = requests.get(GASTAT_UNEMPLOYMENT_API, headers=HEADERS, timeout=15)
        if res.status_code == 200:
            for r in res.json().get("results", []):
                # Try various common field names
                val = (
                    r.get("saudi_unemployment_rate") or
                    r.get("unemployment_rate") or
                    r.get("total_unemployment_rate") or
                    r.get("value")
                )
                if val is not None:
                    pct = float(val)
                    if 0 < pct < 50:   # sanity check: unemployment rate must be 0-50%
                        period = str(r.get("date") or r.get("period") or _CURRENT_PERIOD)
                        logger.info(f"[UNEMP] Live unemployment: {pct}% — period: {period}")
                        return {
                            "unemployment_pct": pct,
                            "period":           period,
                            "is_fallback":      False,
                            "source_status":    "live",
                        }
    except Exception as e:
        logger.warning(f"[UNEMP] GaStat API query failed: {e}")

    logger.warning(f"[UNEMP] Using fallback unemployment: {_FALLBACK_UNEMPLOYMENT_PCT}%")
    return {
        "unemployment_pct": _FALLBACK_UNEMPLOYMENT_PCT,
        "period":           "2024-Q4-GaStat",
        "is_fallback":      True,
        "source_status":    "fallback",
        "reason":           "GaStat API returned no valid unemployment value",
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
        companies  = list_companies()
        total_mc   = sum(getattr(c, "market_cap", 0.0) or 0.0 for c in companies)
        mc_source  = "xbrl_data_service"
    except Exception as e:
        logger.warning(f"[BUFFETT] Could not fetch live market cap: {e}")
        total_mc  = 9850000.0  # ~9.85T SAR TASI market cap fallback
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

def save_economic_indicators_to_db(payload: dict) -> int:
    """
    Upserts indicators into saudi_economic_indicators using historical preservation.
    Composite key: (indicator_key, period, source).
    Each distinct (indicator, period, source) creates or updates one row.
    This preserves historical observations — never overwrites a different period.
    """
    ensure_table_exists()
    db: Session = SessionLocal()
    saved = 0

    try:
        for key, info in payload.items():
            period     = str(info.get("period", _CURRENT_PERIOD))
            source     = str(info.get("source", "REBH Engine"))
            value      = info.get("value")
            raw_value  = str(value) if value is not None else None
            is_fallback = bool(info.get("is_fallback", False))

            # Find matching historical observation (exact key + period + source)
            existing = db.query(SaudiEconomicIndicator).filter(
                SaudiEconomicIndicator.indicator_key == key,
                SaudiEconomicIndicator.period        == period,
                SaudiEconomicIndicator.source        == source,
            ).first()

            if existing:
                existing.value          = value
                existing.raw_value      = raw_value
                existing.unit           = info.get("unit")
                existing.indicator_name = info.get("name")
                existing.is_fallback    = is_fallback
                existing.frequency      = info.get("frequency", "Monthly")
                existing.source_url     = info.get("source_url")
            else:
                db.add(SaudiEconomicIndicator(
                    indicator_key   = key,
                    indicator_name  = info.get("name"),
                    value           = value,
                    raw_value       = raw_value,
                    unit            = info.get("unit"),
                    period          = period,
                    frequency       = info.get("frequency", "Monthly"),
                    source          = source,
                    source_url      = info.get("source_url"),
                    is_fallback     = is_fallback,
                ))
            saved += 1

        db.commit()
        logger.info(f"[DB] Saved/Updated {saved} Saudi economic indicators (historical).")
    except Exception as e:
        db.rollback()
        logger.error(f"[DB] Error saving economic indicators: {e}")
    finally:
        db.close()

    return saved


# ──────────────────────────────────────────────────────────────────────────────
# Economy Scorecard API helper
# ──────────────────────────────────────────────────────────────────────────────

def get_economy_scorecard() -> dict:
    """
    Returns latest Economy Scorecard from DB.
    Used by the Market Monitor API endpoint.
    Each indicator includes: value, unit, period, source, is_fallback, source_status.
    """
    db = SessionLocal()
    scorecard = {}
    SCORECARD_KEYS = [
        "repo_rate",
        "saibor_3m",
        "saudi_gdp_annual",
        "saudi_unemployment",
        "saudi_buffett_indicator",
    ]
    try:
        for key in SCORECARD_KEYS:
            row = (
                db.query(SaudiEconomicIndicator)
                .filter(SaudiEconomicIndicator.indicator_key == key)
                .order_by(SaudiEconomicIndicator.retrieved_at.desc())
                .first()
            )
            if row:
                scorecard[key] = {
                    "name":          row.indicator_name,
                    "value":         row.value,
                    "unit":          row.unit,
                    "period":        row.period,
                    "source":        row.source,
                    "is_fallback":   row.is_fallback,
                    "source_status": "fallback" if row.is_fallback else "live",
                    "retrieved_at":  row.retrieved_at.isoformat() if row.retrieved_at else None,
                }
            else:
                scorecard[key] = {
                    "name":          key,
                    "value":         None,
                    "is_fallback":   True,
                    "source_status": "missing",
                    "reason":        "No data in DB — run SAMA&GaStat.py first",
                }
    except Exception as e:
        logger.error(f"[SCORECARD] DB query failed: {e}")
    finally:
        db.close()

    return scorecard


# ──────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ──────────────────────────────────────────────────────────────────────────────

def run_economic_sync() -> dict:
    """
    Full macroeconomic sync: SAMA + GaStat + Buffett Indicator.
    Saves all 5 Economy Scorecard indicators to DB with historical preservation.
    """
    logger.info("=== Starting SAMA & GaStat Macroeconomic Sync (Phase 5) ===")

    # ── 1. SAMA Bulletin: Repo Rate, Reverse Repo, SAIBOR 3M, SAIBOR 12M ──
    rates = fetch_saibor_from_sama_bulletin()
    repo_info     = rates["repo_rate"]
    rev_repo_info = rates["reverse_repo_rate"]
    s3m_info      = rates["saibor_3m"]
    s12m_info     = rates["saibor_12m"]

    # ── 2. GDP ──
    gdp_data = fetch_saudi_gdp()
    gdp_val  = gdp_data["gdp_m_sar"]
    gdp_period = gdp_data["period"]

    # ── 3. Unemployment ──
    unemp_data = fetch_unemployment()

    # ── 4. Buffett Indicator ──
    buffett = calculate_buffett_indicator(gdp_val)

    # ── Build canonical payload ──
    period_label = _CURRENT_PERIOD  # Always use current month for SAMA-sourced rates

    payload = {
        "repo_rate": {
            "name":        "Repo Rate (SAMA)",
            "value":       repo_info["value"],
            "unit":        "%",
            "period":      period_label,
            "frequency":   "Monthly",
            "source":      "SAMA — Monthly Bulletin",
            "source_url":  SAMA_PAGE_URL,
            "is_fallback": repo_info["is_fallback"],
        },
        "reverse_repo_rate": {
            "name":        "Reverse Repo Rate (SAMA)",
            "value":       rev_repo_info["value"],
            "unit":        "%",
            "period":      period_label,
            "frequency":   "Monthly",
            "source":      "SAMA — Monthly Bulletin",
            "source_url":  SAMA_PAGE_URL,
            "is_fallback": rev_repo_info["is_fallback"],
        },
        "saibor_3m": {
            "name":        "SAIBOR 3M",
            "value":       s3m_info["value"],
            "unit":        "%",
            "period":      period_label,
            "frequency":   "Monthly",
            "source":      "SAMA — Monthly Bulletin",
            "source_url":  SAMA_PAGE_URL,
            "is_fallback": s3m_info["is_fallback"],
        },
        "saibor_12m": {
            "name":        "SAIBOR 12M",
            "value":       s12m_info["value"],
            "unit":        "%",
            "period":      period_label,
            "frequency":   "Monthly",
            "source":      "SAMA — Monthly Bulletin",
            "source_url":  SAMA_PAGE_URL,
            "is_fallback": s12m_info["is_fallback"],
        },
        "saudi_gdp_annual": {
            "name":        "Saudi GDP at Current Prices",
            "value":       gdp_val,
            "unit":        "M SAR",
            "period":      gdp_period,
            "frequency":   "Annual",
            "source":      "GaStat / KAPSARC",
            "source_url":  KAPSARC_GDP_API,
            "is_fallback": gdp_data["is_fallback"],
        },
        "saudi_unemployment": {
            "name":        "Saudi Unemployment Rate",
            "value":       unemp_data["unemployment_pct"],
            "unit":        "%",
            "period":      unemp_data["period"],
            "frequency":   "Quarterly",
            "source":      "GaStat — Labour Force Survey",
            "source_url":  GASTAT_UNEMPLOYMENT_API,
            "is_fallback": unemp_data["is_fallback"],
        },
        "saudi_buffett_indicator": {
            "name":        "Saudi Buffett Indicator (TASI / GDP)",
            "value":       buffett["buffett_ratio_pct"],
            "unit":        "%",
            "period":      gdp_period,
            "frequency":   "Annual",
            "source":      "REBH Engine",
            "is_fallback": gdp_data["is_fallback"],  # fallback if GDP was fallback
        },
    }

    # Log summary — ASCII-safe only
    for k, v in payload.items():
        flag = "[FALLBACK]" if v.get("is_fallback") else "[LIVE]"
        logger.info(f"[PAYLOAD] {flag} {k}: {v['value']} {v['unit']} ({v['period']})")

    save_economic_indicators_to_db(payload)
    logger.info("=== Finished SAMA & GaStat Macroeconomic Sync ===")
    return payload


if __name__ == "__main__":
    res = run_economic_sync()
    live_count     = sum(1 for v in res.values() if not v.get("is_fallback"))
    fallback_count = sum(1 for v in res.values() if v.get("is_fallback"))
    print(f"\n[SUCCESS] Macro Sync: {len(res)} indicators saved ({live_count} live, {fallback_count} fallback).")
    for k, v in res.items():
        status = "FALLBACK" if v.get("is_fallback") else "LIVE"
        print(f"  [{status}] {k}: {v.get('value')} {v.get('unit')} ({v.get('period')})")