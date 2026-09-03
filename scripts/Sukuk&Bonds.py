"""
Sukuk & Bonds Scraper & DB Ingestion — Saudi Exchange (TASI)
Extracts listed Sukuk and Government Bonds data and saves/updates them into DB with Upsert logic.
Can be safely run repeatedly (idempotent): updates existing records and inserts new ones without duplication.
"""
import logging
import os
import sys
import time
import json
import re
import requests

# Setup path for backend imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session
from app.core.database import SessionLocal, engine
from app.models.sukuk_bonds import SukukMarketData


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_PAGE = "https://www.saudiexchange.sa/wps/portal/saudiexchange/ourmarkets/sukuk-market-watch?locale=en"

STATIC_AJAX_URL = (
    "https://www.saudiexchange.sa/wps/portal/saudiexchange/ourmarkets/sukuk-market-watch"
    "/!ut/p/z1/04_Sj9CPykssy0xPLMnMz0vMAfIjo8ziTR3NDIw8LAz8LVxcnA0C3bwtPLwM_I0MXMz0w9EU-LqbGQT6OQb6G5mbGhgEG-lHkaTfIDjAFKggwNfYxyDIwN3AjDj9BjiAowFh_VFoSjB9gKoAixPBCvC4ITg1T78gNzQ0wiAzIN1RUREAdewi3A!!"
    "/p0/IZ7_5A602H80OOMQC0604RU6VD1091=CZ6_5A602H80O8DDC0QFK8HJ0O20D6=NJgetSukukMarketDetails=/"
)


def ensure_table_exists():
    """Ensure the sukuk_market_data table exists."""
    try:
        SukukMarketData.__table__.create(engine, checkfirst=True)
        logger.info("[DB] Table 'sukuk_market_data' checked/ready.")
    except Exception as e:
        logger.error(f"[DB] Error checking table: {e}")


def fetch_sukuk_via_requests():
    """Attempt fast direct requests."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE_PAGE,
    }
    session = requests.Session()
    session.headers.update(headers)

    try:
        logger.info("[NET] Requesting base page for cookies...")
        r0 = session.get(BASE_PAGE, timeout=15)
        params = {
            "sectorParameter": "all",
            "iswatchListSelected": "NO",
            "requestLocale": "en",
            "_": int(time.time() * 1000),
        }
        logger.info("[NET] Fetching Sukuk data via static AJAX URL...")
        r1 = session.get(STATIC_AJAX_URL, params=params, timeout=20)
        
        if r1.status_code == 200:
            data = r1.json()
            items = data.get("sukukList") or data.get("data") or data.get("aaData") or []
            if items:
                logger.info(f"[NET] Successfully fetched {len(items)} items via requests.")
                return items
    except Exception as e:
        logger.warning(f"[NET] Requests failed: {e}. Falling back to Browser Fetch...")

    return None


def fetch_sukuk_via_browser():
    """Dynamic Selenium discovery with in-browser fetch fallback."""
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from webdriver_manager.chrome import ChromeDriverManager

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    try:
        logger.info("[BROWSER] Opening Sukuk page in headless Chrome...")
        driver.get(BASE_PAGE)
        time.sleep(4)

        ajax_url = None
        logs = driver.get_log("performance")
        for entry in logs:
            try:
                log = json.loads(entry["message"])["message"]
                if log["method"] == "Network.requestWillBeSent":
                    req_url = log["params"]["request"]["url"]
                    if "getSukukMarketDetails" in req_url or ("sukuk-market-watch/!ut/p/" in req_url and "http" in req_url):
                        ajax_url = req_url
                        break
            except Exception:
                continue

        if not ajax_url:
            match = re.search(r'[\'"]([^\'"]+getSukukMarketDetails[^\'"]*)[\'"]', driver.page_source)
            if match:
                ajax_url = match.group(1)
                if ajax_url.startswith("/"):
                    ajax_url = "https://www.saudiexchange.sa" + ajax_url

        if not ajax_url:
            ajax_url = STATIC_AJAX_URL

        js_code = f"""
        const done = arguments[0];
        fetch('{ajax_url}?sectorParameter=all&iswatchListSelected=NO&requestLocale=en&_=' + Date.now(), {{
            headers: {{
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'X-Requested-With': 'XMLHttpRequest'
            }}
        }})
        .then(res => res.json())
        .then(data => done({{ success: true, data: data }}))
        .catch(err => done({{ success: false, error: err.toString() }}));
        """
        result = driver.execute_async_script(js_code)
        if result.get("success"):
            data = result.get("data", {})
            return data.get("sukukList") or data.get("data") or data.get("aaData") or []
    finally:
        driver.quit()

    return None


def save_sukuk_to_db(items: list) -> int:
    """
    Saves or updates Sukuk records in PostgreSQL (Upsert logic).
    If a record with the same symbol exists, it updates its values.
    """
    ensure_table_exists()
    db: Session = SessionLocal()
    saved_count = 0
    try:
        for it in items:
            sym = str(it.get("symbol", "")).strip()
            if not sym:
                continue

            issuer = it.get("issuerName")
            parent_sym = str(it.get("parentCompnaySymbol", "")).strip() or None
            b_type = it.get("bondType")
            coupon = str(it.get("couponRate", ""))
            maturity = str(it.get("maturityDateStr") or it.get("maturityDate", ""))
            amount = str(it.get("outstandingAmountModified") or it.get("outstandingAmount", ""))
            sector = it.get("sectorName")

            existing = db.query(SukukMarketData).filter(SukukMarketData.symbol == sym).first()
            if existing:
                existing.issuer_name = issuer
                existing.parent_company_symbol = parent_sym
                existing.bond_type = b_type
                existing.coupon_rate = coupon
                existing.maturity_date = maturity
                existing.outstanding_amount = amount
                existing.sector_name = sector
            else:
                new_row = SukukMarketData(
                    symbol=sym,
                    issuer_name=issuer,
                    parent_company_symbol=parent_sym,
                    bond_type=b_type,
                    coupon_rate=coupon,
                    maturity_date=maturity,
                    outstanding_amount=amount,
                    sector_name=sector
                )
                db.add(new_row)
            saved_count += 1

        db.commit()
        logger.info(f"[DB] Upserted {saved_count} Sukuk & Bonds records successfully.")
    except Exception as e:
        db.rollback()
        logger.error(f"[DB] Error upserting Sukuk data: {e}")
    finally:
        db.close()

    return saved_count


def run_sukuk_sync():
    """Main execution entry point."""
    logger.info("=== Starting Sukuk & Bonds Data Sync ===")
    items = fetch_sukuk_via_requests()
    if not items:
        items = fetch_sukuk_via_browser()

    if items:
        count = save_sukuk_to_db(items)
        logger.info(f"=== Sync Finished: {count} instruments updated ===")
        return count
    else:
        logger.error("=== Sync Failed: No data fetched ===")
        return 0


if __name__ == "__main__":
    run_sukuk_sync()