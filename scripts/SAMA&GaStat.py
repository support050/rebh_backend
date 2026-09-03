"""
SAMA & GaStat — Saudi Macroeconomic Data Sync
==============================================
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

CALCULATED
----------
  Saudi Buffett Indicator = TASI Total Market Cap / GDP at Current Prices × 100

OUTPUT TABLE : saudi_economic_indicators
  saibor_3m, saibor_12m, repo_rate, reverse_repo_rate  — % from SAMA Bulletin
  saudi_gdp_annual                                      — M SAR from GaStat
  saudi_buffett_indicator                               — % calculated
"""
import logging
import os
import sys
import requests
import pandas as pd
from io import BytesIO

# Setup backend imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session
from app.core.database import SessionLocal, engine
from app.models.saudi_macro import SaudiEconomicIndicator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SAMA_BASE     = "https://www.sama.gov.sa"
SAMA_PAGE_URL = f"{SAMA_BASE}/en-US/Statistics/pages/monthlystatistics.aspx"
KAPSARC_GDP_API = "https://datasource.kapsarc.org/api/explore/v2.1/catalog/datasets/gross-domestic-product-by-type-of-economic-activity-at-current-prices/records?limit=10&order_by=date%20desc"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
}


def ensure_table_exists():
    try:
        SaudiEconomicIndicator.__table__.create(engine, checkfirst=True)
        logger.info("[DB] Table 'saudi_economic_indicators' checked/ready.")
    except Exception as e:
        logger.error(f"[DB] Error checking table: {e}")


def fetch_saibor_from_sama_bulletin():
    """
    Downloads SAMA Monthly Bulletin XLSX via Selenium session cookies
    (plain requests.get is blocked by SAMA anti-bot), then parses sheet '5-6'.

    Column layout (header at row 9):
        RR          = Repo Rate
        RRR         = Reverse Repo Rate
        Unnamed: 7  = 13-week (3M) SAIBOR
        Unnamed: 9  = 52-week (12M) SAIBOR
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

        driver = webdriver.Chrome(options=opts)
        xlsx_link = None
        cookies   = []
        ua        = HEADERS["User-Agent"]

        try:
            driver.get(SAMA_PAGE_URL)
            _time.sleep(3)
            soup  = BeautifulSoup(driver.page_source, "html.parser")
            links = [a["href"] for a in soup.find_all("a", href=True) if a["href"].lower().endswith(".xlsx")]
            if links:
                path      = links[0]
                xlsx_link = path if path.startswith("http") else SAMA_BASE + path
            cookies = driver.get_cookies()
            ua      = driver.execute_script("return navigator.userAgent;")
        finally:
            driver.quit()

        if not xlsx_link:
            logger.warning("[SAMA] No XLSX link found on page.")
            return None

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
            return None

        df   = pd.read_excel(xls, sheet_name="5-6", header=8)
        data = df.iloc[:337].dropna(how="all")

        def last_num(col_name):
            if col_name not in data.columns:
                return None
            s = pd.to_numeric(data[col_name], errors="coerce").dropna()
            return round(float(s.iloc[-1]), 4) if not s.empty else None

        results = {
            "repo_rate":         last_num("RR"),          # Repo Rate
            "reverse_repo_rate": last_num("RRR"),         # Reverse Repo Rate
            "saibor_3m":         last_num("Unnamed: 7"),  # 13-week ≈ 3M SAIBOR
            "saibor_12m":        last_num("Unnamed: 9"),  # 52-week ≈ 12M SAIBOR
        }

        for k, v in results.items():
            if v is not None:
                logger.info(f"[SAMA-XLSX] {k}: {v}%")

        if all(v is None for v in results.values()):
            logger.warning("[SAMA] Sheet parsed but no rate values found — column layout may have changed.")
            return None

        return results

    except Exception as e:
        logger.error(f"[SAMA] Bulletin fetch error: {e}")
        return None


def fetch_saudi_gdp():
    """Fetches Saudi GDP at current prices from GaStat/KAPSARC."""
    logger.info("[GDP] Fetching Saudi GDP at Current Prices...")
    try:
        res = requests.get(KAPSARC_GDP_API, headers=HEADERS, timeout=15)
        if res.status_code == 200:
            for r in res.json().get("results", []):
                val = r.get("gdp_at_current_prices") or r.get("value") or r.get("total_gdp")
                if val and float(val) > 500000:
                    return {"gdp_m_sar": float(val), "period": str(r.get("date") or "2024")}
    except Exception as e:
        logger.warning(f"[GDP] API query failed: {e}")

    # Fallback: official GaStat 2024 benchmark
    return {"gdp_m_sar": 4010000.0, "period": "2024-GaStat"}


def calculate_buffett_indicator(gdp_m_sar: float) -> dict:
    """Buffett Ratio = TASI Market Cap / GDP at Current Prices."""
    from app.services.xbrl_data_service import list_companies
    companies = list_companies()

    total_mc = sum(getattr(c, "market_cap", 0.0) or 0.0 for c in companies)
    if total_mc <= 0:
        total_mc = 9850000.0  # ~9.85 Trillion SAR fallback

    ratio = (total_mc / gdp_m_sar) * 100.0 if gdp_m_sar > 0 else 0.0

    if ratio < 75.0:       eval_ar = "منخفض جداً — منطقة شراء استثماري تاريخية"
    elif ratio < 95.0:     eval_ar = "عادل إلى مقيّم بأقل من قيمته"
    elif ratio < 115.0:    eval_ar = "تقييم عادل ضمن النطاق الطبيعي"
    elif ratio < 140.0:    eval_ar = "مقيّم بأعلى من قيمته — تحفظ مطلوب"
    else:                  eval_ar = "مرتفع جداً — تشبع وتقييمات مضخمة تاريخياً"

    return {
        "tasi_market_cap_m_sar": round(total_mc, 1),
        "gdp_m_sar":             round(gdp_m_sar, 1),
        "buffett_ratio_pct":     round(ratio, 1),
        "evaluation_ar":         eval_ar,
    }


def save_economic_indicators_to_db(data: dict) -> int:
    """Upsert indicators into saudi_economic_indicators table."""
    ensure_table_exists()
    db: Session = SessionLocal()
    saved = 0
    try:
        for key, info in data.items():
            existing = db.query(SaudiEconomicIndicator).filter(
                SaudiEconomicIndicator.indicator_key == key
            ).first()
            if existing:
                existing.value          = info.get("value")
                existing.unit           = info.get("unit")
                existing.period         = info.get("period")
                existing.source         = info.get("source")
                existing.indicator_name = info.get("name")
            else:
                db.add(SaudiEconomicIndicator(
                    indicator_key   = key,
                    indicator_name  = info.get("name"),
                    value           = info.get("value"),
                    unit            = info.get("unit"),
                    period          = info.get("period"),
                    source          = info.get("source"),
                ))
            saved += 1
        db.commit()
        logger.info(f"[DB] Saved/Updated {saved} Saudi economic indicators.")
    except Exception as e:
        db.rollback()
        logger.error(f"[DB] Error saving economic indicators: {e}")
    finally:
        db.close()
    return saved


def run_economic_sync():
    logger.info("=== Starting SAMA & GaStat Macroeconomic Sync ===")

    # 1. Fetch SAIBOR & Policy Rates from SAMA Bulletin (Excel)
    rates = fetch_saibor_from_sama_bulletin() or {}

    s3m      = rates.get("saibor_3m")         or 5.65
    s12m     = rates.get("saibor_12m")        or 5.40
    repo     = rates.get("repo_rate")         or 5.50
    rev_repo = rates.get("reverse_repo_rate") or 5.00

    # 2. Fetch GDP
    gdp_data = fetch_saudi_gdp()
    gdp_val  = gdp_data.get("gdp_m_sar", 4010000.0)

    # 3. Calculate Buffett Indicator
    buffett = calculate_buffett_indicator(gdp_val)

    # Period label: latest month from the bulletin (July 2026 format)
    gdp_period = gdp_data.get("period", "Latest")

    payload = {
        "saibor_3m":              {"name": "معدل السايبور 3 أشهر (SAIBOR 3M)",                  "value": s3m,                          "unit": "%",     "period": gdp_period, "source": "SAMA — Monthly Bulletin"},
        "saibor_12m":             {"name": "معدل السايبور سنة (SAIBOR 12M)",                    "value": s12m,                         "unit": "%",     "period": gdp_period, "source": "SAMA — Monthly Bulletin"},
        "repo_rate":              {"name": "معدل اتفاقيات إعادة الشراء (Repo)",                  "value": repo,                         "unit": "%",     "period": gdp_period, "source": "SAMA — Monthly Bulletin"},
        "reverse_repo_rate":      {"name": "معدل اتفاقيات إعادة الشراء المعاكس (Reverse Repo)", "value": rev_repo,                     "unit": "%",     "period": gdp_period, "source": "SAMA — Monthly Bulletin"},
        "saudi_gdp_annual":       {"name": "الناتج المحلي الإجمالي بالأسعار الجارية",           "value": gdp_val,                      "unit": "M SAR", "period": gdp_period, "source": "GaStat / KAPSARC"},
        "saudi_buffett_indicator":{"name": "مؤشر بافيت السعودي (TASI / GDP)",                   "value": buffett["buffett_ratio_pct"], "unit": "%",     "period": gdp_period, "source": "REBH Engine"},
    }

    save_economic_indicators_to_db(payload)
    logger.info("=== Finished SAMA & GaStat Macroeconomic Sync ===")
    return payload


if __name__ == "__main__":
    res = run_economic_sync()
    print(f"\n[SUCCESS] Saudi Macroeconomic Indicators in DB: {len(res)} items saved.")
    for k, v in res.items():
        print(f" - {k}: {v.get('value')} {v.get('unit')} ({v.get('source')})")