"""
Historical Reports Scraper - Saudi Exchange (TASI Index)
Bypasses the buggy frontend UI entirely by using the DataTables AJAX endpoint.
"""
import logging
import os
import sys
import time
import json
import re
import requests as http_requests
from datetime import datetime, date

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# Setup path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session
from app.core.database import SessionLocal, engine
from app.models.market_reports import HistoricalReport

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

URL = "https://www.saudiexchange.sa/wps/portal/saudiexchange/newsandreports/reports-publications/historical-reports?locale=en"
BATCH_SIZE = 100


def ensure_table_exists():
    """Create the historical_reports table if it doesn't exist"""
    try:
        HistoricalReport.__table__.create(engine, checkfirst=True)
        logger.info("✅ Table 'historical_reports' is ready.")
    except Exception as e:
        logger.error(f"Error creating table: {e}")


def setup_driver():
    """Setup Chrome driver"""
    import os
    import shutil
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    chrome_options.set_capability('unhandledPromptBehavior', 'accept')
    chrome_options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})

    # Try to find Chrome binary
    chrome_bin = None
    for env_var in ['CHROME_BIN', 'GOOGLE_CHROME_BIN', 'CHROMIUM_BIN']:
        env_path = os.environ.get(env_var)
        if env_path and os.path.exists(env_path):
            chrome_bin = env_path
            break
            
    if not chrome_bin:
        for chrome_name in ['google-chrome-stable', 'google-chrome', 'chromium-browser', 'chromium', 'chrome']:
            found_path = shutil.which(chrome_name)
            if found_path:
                chrome_bin = found_path
                break
                
    if not chrome_bin:
        linux_chrome_paths = [
            '/opt/render/project/.chrome/chrome-linux64/chrome',
            '/usr/bin/google-chrome-stable',
            '/usr/bin/google-chrome',
            '/usr/bin/chromium-browser',
            '/usr/bin/chromium',
            '/opt/google/chrome/chrome',
            '/opt/google/chrome/google-chrome',
            '/app/.apt/usr/bin/google-chrome',
        ]
        for path in linux_chrome_paths:
            if os.path.exists(path):
                chrome_bin = path
                break

    if chrome_bin:
        chrome_options.binary_location = chrome_bin
        logger.info(f"✅ Using Chrome binary: {chrome_bin}")

    # Try to locate ChromeDriver manually first
    chromedriver_path = None
    env_chromedriver = os.environ.get('CHROMEDRIVER_PATH')
    if env_chromedriver and os.path.exists(env_chromedriver):
        chromedriver_path = env_chromedriver
        
    if not chromedriver_path:
        possible_driver_paths = [
            '/opt/render/project/.chrome/chromedriver-linux64/chromedriver',
            '/app/.chromedriver/bin/chromedriver',
            '/usr/bin/chromedriver',
            '/usr/local/bin/chromedriver'
        ]
        for path in possible_driver_paths:
            if os.path.exists(path):
                chromedriver_path = path
                break

    if chromedriver_path:
        logger.info(f"📍 Found explicit ChromeDriver at: {chromedriver_path}")
        service = Service(chromedriver_path)
    else:
        service = Service(ChromeDriverManager().install())
        
    return webdriver.Chrome(service=service, options=chrome_options)


def dismiss_alerts(driver):
    """Dismiss any JavaScript alert dialogs"""
    try:
        alert = driver.switch_to.alert
        alert.accept()
    except Exception:
        pass


def parse_date(date_text: str) -> date | None:
    """Try multiple date formats"""
    if not date_text: return None
    for fmt in ("%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def get_existing_dates(db: Session) -> set:
    """Get all dates already in DB to skip duplicates"""
    results = db.query(HistoricalReport.report_date).all()
    return {r[0] for r in results}


def discover_ajax_url(driver):
    """
    Capture the DataTable's AJAX data source URL from the browser's network log or page source.
    """
    logs = driver.get_log('performance')
    ajax_url = None
    
    # 1. Search Network Logs
    for entry in logs:
        try:
            log = json.loads(entry['message'])['message']
            if log['method'] == 'Network.requestWillBeSent':
                req_url = log['params']['request']['url']
                if 'populateCompanyDetails' in req_url or ('historical-reports/!ut/p/' in req_url and 'http' in req_url):
                    ajax_url = req_url
                    logger.info(f"Found AJAX URL in Network Logs!")
                    break
        except Exception:
            continue
            
    # 2. Search Page Source Action URLs
    if not ajax_url:
        page_source = driver.page_source
        match = re.search(r'[\'"]([^\'"]+populateCompanyDetails[^\'"]*)[\'"]', page_source)
        if match:
            ajax_url = match.group(1)
            # Ensure it is an absolute URL
            if ajax_url.startswith('/'):
                ajax_url = "https://www.saudiexchange.sa" + ajax_url
            logger.info("Found AJAX URL in Page Source!")

    # 3. Form action fallback
    if not ajax_url:
        try:
            # Often WebSphere puts the resource URL in a form or hidden input
            forms = driver.find_elements(By.TAG_NAME, "form")
            for f in forms:
                action = f.get_attribute("action")
                if action and 'historical-reports/!ut/p/' in action:
                    ajax_url = action
                    logger.info("Found AJAX URL in Form Action!")
                    break
        except Exception:
            pass

    return ajax_url


def scrape_with_api(ajax_url, driver, db, existing_dates):
    """
    Bypass the UI and scrape data directly using the DataTables AJAX API.
    Since F5 BIG-IP blocks Python 'requests', we use the browser context via JS fetch().
    """
    logger.info("Starting direct API scraping via browser fetch()...")
    import urllib.parse
    
    start_date = "06-01-2007"
    end_date = datetime.now().strftime("%d-%m-%Y")
    
    base_data = {
        "draw": 1,
        "selectedMarket": "INDICES",
        "selectedSector": "M",
        "selectedEntity": "M:TASI",
        "startDate": start_date,
        "endDate": end_date,
        "tableTabId": 0,
        "length": BATCH_SIZE,
        "columns[0][data]": "transactionDateStr",
        "columns[1][data]": "todaysOpen",
        "columns[2][data]": "highPrice",
        "columns[3][data]": "lowPrice",
        "columns[4][data]": "previousClosePrice",  # Close
        "columns[5][data]": "change",
        "columns[6][data]": "changePercent",
        "columns[7][data]": "volumeTraded",
        "columns[8][data]": "turnOver",
        "columns[9][data]": "noOfTrades",
    }

    total_added = 0
    total_scraped = 0
    start_index = 0
    
    # Set timeout for async script execution
    driver.set_script_timeout(30)
    
    js_fetch_code = """
    var url = arguments[0];
    var payloadStr = arguments[1];
    var callback = arguments[2];

    fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'application/json, text/javascript, */*; q=0.01'
        },
        body: payloadStr
    })
    .then(response => {
        if (!response.ok) {
            throw new Error('HTTP status ' + response.status);
        }
        return response.json();
    })
    .then(data => callback({success: true, data: data}))
    .catch(error => callback({success: false, error: error.toString()}));
    """
    
    while True:
        payload = base_data.copy()
        payload["start"] = start_index
        payload["startIndex"] = start_index
        payload["endIndex"] = start_index + BATCH_SIZE - 1
        
        payloadStr = urllib.parse.urlencode(payload)
        logger.info(f"Fetching API Offset = {start_index} via Selenium fetch()...")
        
        try:
            result = driver.execute_async_script(js_fetch_code, ajax_url, payloadStr)
            
            if not result.get("success"):
                logger.error(f"JavaScript fetch failed: {result.get('error')}")
                break
                
            data = result.get("data", {})
            records = data.get("data", [])
            
            if not records:
                logger.info("No more records found in API response. Finished.")
                break
                
            buffer = []
            new_in_batch = 0
            for item in records:
                date_str = item.get("transactionDateStr", "")
                d = parse_date(date_str)
                if not d:
                    continue
                if d in existing_dates:
                    continue
                    
                new_in_batch += 1
                buffer.append(HistoricalReport(
                    report_date=d,
                    open_price=str(item.get("todaysOpen", 0)),
                    high_price=str(item.get("highPrice", 0)),
                    low_price=str(item.get("lowPrice", 0)),
                    close_price=str(item.get("previousClosePrice", 0)),
                    volume_traded=str(item.get("volumeTraded", 0)),
                    value_traded=str(item.get("turnOver", 0)),
                    no_of_trades=str(item.get("noOfTrades", 0)),
                ))
            
            if records and new_in_batch == 0:
                logger.info("All records in this batch already exist in the database. Incremental scrape complete!")
                break
                
            total_scraped += len(records)
            
            if buffer:
                db.bulk_save_objects(buffer)
                db.commit()
                total_added += len(buffer)
                for r in buffer:
                    existing_dates.add(r.report_date)
                    
            logger.info(f"API Scraped: {total_scraped} | New Added: {total_added}")
            
            start_index += BATCH_SIZE
            time.sleep(1) # Be courteous to the server
            
        except Exception as e:
            logger.error(f"API Error at offset {start_index}: {e}")
            break
            
    return total_scraped, total_added


def main():
    logger.info(f"=== Historical Reports API Scraper started at {datetime.now()} ===")
    
    ensure_table_exists()
    db = SessionLocal()
    existing_dates = get_existing_dates(db)
    logger.info(f"DB currently has {len(existing_dates)} records.")
    
    driver = setup_driver()
    try:
        logger.info(f"Loading page: {URL}")
        driver.get(URL)
        
        wait = WebDriverWait(driver, 45)
        
        # Wait for the page to fully load and the first default fetch to happen
        wait.until(EC.presence_of_element_located((By.ID, "perfSummary")))
        time.sleep(5)
        dismiss_alerts(driver)
        
        # STEP 1: Discover AJAX URL
        ajax_url = discover_ajax_url(driver)
        
        if ajax_url:
            logger.info(f"AJAX API Endpoint Discovered: {ajax_url}")
            total_scraped, total_added = scrape_with_api(ajax_url, driver, db, existing_dates)
            logger.info(f"=== API Scrape Finished: {total_scraped} fetched, {total_added} saved ===")
        else:
            logger.error("Could not discover the AJAX URL from network logs or source.")
            logger.info("Since UI manipulation breaks the page filters, manual API interception is the only reliable way.")
    except Exception as e:
        logger.error(f"Scraper failed: {e}")
    finally:
        db.close()
        driver.quit()


if __name__ == "__main__":
    main()