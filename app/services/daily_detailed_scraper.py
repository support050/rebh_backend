from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import pandas as pd
import time
import logging
import os

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Selenium's urllib3 client that talks to the LOCAL chromedriver server defaults
# to a 120s read timeout. On 2026-10-13 the Render run of market_reports died
# exactly there — "HTTPConnectionPool(host='localhost', port=39099): Read timed
# out. (read timeout=120)" — while Chrome was still loading the Tadawul page.
# 300s sits above any legitimate command duration once page_load_strategy is
# 'eager' (RemoteConnection.set_timeout() is deprecated/broken in Selenium 4.39,
# so we set timeout on the driver's own ClientConfig after creation).
_SELENIUM_CLIENT_TIMEOUT = 300


def _apply_client_timeout(driver):
    """Raise the per-driver HTTP read timeout away from the 120s default."""
    try:
        driver.command_executor._client_config.timeout = _SELENIUM_CLIENT_TIMEOUT
    except Exception as e:
        logger.warning(f"\u26a0\ufe0f Could not raise Selenium client timeout: {e}")
    return driver

def build_driver(headless=True):
    """
    Build the Chrome WebDriver with the same options as the legacy script,
    plus Render/Docker deployment support.
    """
    import shutil
    
    # Environment diagnostics are kept visible on purpose: they are the first
    # thing you read when a driver fails to start on a new machine.
    chrome_bin_env = os.environ.get('CHROME_BIN')
    chromedriver_env = os.environ.get('CHROMEDRIVER_PATH')
    logger.info(f"DEBUG: CHROME_BIN env = {chrome_bin_env}")
    logger.info(f"DEBUG: CHROMEDRIVER_PATH env = {chromedriver_env}")
    
    # Check whether the Render Chrome/ChromeDriver paths exist
    render_chrome = '/opt/render/project/.chrome/chrome-linux64/chrome'
    render_driver = '/opt/render/project/.chrome/chromedriver-linux64/chromedriver'
    logger.info(f"DEBUG: Render Chrome exists = {os.path.exists(render_chrome)}")
    logger.info(f"DEBUG: Render ChromeDriver exists = {os.path.exists(render_driver)}")
    
    options = Options()
    
    # 'eager': navigation resolves when the DOM is ready, not when every
    # sub-resource finishes. The WebDriverWait(table) calls in the scrapers
    # are what actually guard data readiness, so this only sheds the parts
    # of the page load that were blowing the 120s chromedriver read timeout.
    options.page_load_strategy = "eager"
    
    # Baseline stability flags
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    # Windows compatibility: avoid aggressive flags that break Chrome renderer communication
    if os.name != 'nt':
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-sync")
        options.add_argument("--disable-translate")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-ipc-flooding-protection")
        options.add_argument("--js-flags=--max-old-space-size=128")
        options.add_argument("--memory-pressure-off")
    else:
        options.add_argument("--remote-allow-origins=*")
        options.add_argument("--disable-features=OptimizationGuideModelDownloading,OptimizationHintsFetching,OptimizationTargetPrediction,OptimizationHints")
    
    if headless:
        options.add_argument("--headless=new")
    
    # Locate the Chrome binary
    chrome_bin = None
    
    # 1. Check environment variables first
    for env_var in ['CHROME_BIN', 'GOOGLE_CHROME_BIN', 'CHROMIUM_BIN']:
        env_path = os.environ.get(env_var)
        if env_path and os.path.exists(env_path):
            chrome_bin = env_path
            logger.info(f"📍 Found Chrome from env {env_var}: {chrome_bin}")
            break
    
    # 2. Try shutil.which() to find Chrome in PATH
    if not chrome_bin:
        for chrome_name in ['google-chrome-stable', 'google-chrome', 'chromium-browser', 'chromium', 'chrome']:
            found_path = shutil.which(chrome_name)
            if found_path:
                chrome_bin = found_path
                logger.info(f"📍 Found Chrome in PATH: {chrome_bin}")
                break
    
    # 3. Try common Linux/Docker/Render paths
    if not chrome_bin:
        linux_chrome_paths = [
            # Render Chrome for Testing (from build script)
            '/opt/render/project/.chrome/chrome-linux64/chrome',
            # Standard Linux paths
            '/usr/bin/google-chrome-stable',
            '/usr/bin/google-chrome',
            '/usr/bin/chromium-browser',
            '/usr/bin/chromium',
            '/opt/google/chrome/chrome',
            '/opt/google/chrome/google-chrome',
            '/snap/bin/chromium',
            # Render apt buildpack path
            '/app/.apt/usr/bin/google-chrome',
            # Windows default installs (Chrome is rarely on PATH there)
            r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        ]
        for path in linux_chrome_paths:
            if os.path.exists(path):
                chrome_bin = path
                logger.info(f"📍 Found Chrome at common path: {chrome_bin}")
                break
    
    # Set binary location if found
    if chrome_bin:
        options.binary_location = chrome_bin
        logger.info(f"✅ Using Chrome binary: {chrome_bin}")
    else:
        logger.warning("⚠️ Chrome binary not found! Will try default ChromeDriver behavior.")
    
    # Try to locate ChromeDriver manually first (Best for Render/Docker)
    chromedriver_path = None
    
    # Check environment variable
    env_chromedriver = os.environ.get('CHROMEDRIVER_PATH')
    if env_chromedriver and os.path.exists(env_chromedriver):
        chromedriver_path = env_chromedriver
    
    # Check common paths if env var not set
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

    # [Perf] Resolve the driver from the LOCAL webdriver-manager cache before any
    # network call. ChromeDriverManager().install() asks Google for the latest
    # driver version on every cold call; the binaries are already on disk under
    # ~/.wdm from previous runs, and the version-matched pick avoids the
    # "session not created" mismatch error.
    if not chromedriver_path:
        try:
            from app.core.chromedriver_cache import resolve_cached_chromedriver
            chromedriver_path = resolve_cached_chromedriver(chrome_bin)
        except Exception as cache_err:
            logger.debug("Local ChromeDriver cache lookup failed: %s", cache_err)
            chromedriver_path = None

    # If we found a specific ChromeDriver, try using it first
    if chromedriver_path:
        try:
            logger.info(f"📍 Found explicit ChromeDriver at: {chromedriver_path}")
            service = Service(chromedriver_path)
            driver = webdriver.Chrome(service=service, options=options)
            logger.info("✅ Chrome WebDriver initialized successfully (via explicit path)")
            return _apply_client_timeout(driver)
        except Exception as e:
            logger.warning(f"⚠️ Explicit ChromeDriver failed, falling back to manager: {e}")

    # Last resort: webdriver-manager (network). Reached only when nothing is
    # cached locally; the resolved path is memoized to avoid repeat lookups.
    global _CACHED_CHROMEDRIVER_PATH
    try:
        if '_CACHED_CHROMEDRIVER_PATH' not in globals() or not _CACHED_CHROMEDRIVER_PATH or not os.path.exists(_CACHED_CHROMEDRIVER_PATH):
            logger.info("🔄 Attempting to use webdriver-manager...")
            from webdriver_manager.chrome import ChromeDriverManager
            _CACHED_CHROMEDRIVER_PATH = ChromeDriverManager().install()
        service = Service(_CACHED_CHROMEDRIVER_PATH)
        driver = webdriver.Chrome(service=service, options=options)
        logger.info("✅ Chrome WebDriver initialized successfully (via cached driver path)")
        return _apply_client_timeout(driver)
    except Exception as e:
        logger.error(f"❌ All methods failed to initialize Chrome: {e}")
        raise

def clean_number(text):
    """
    Strip thousands separators and percent signs, return a float.
    """
    if not text:
        return 0.0
    text = text.replace(',', '').replace('%', '').strip()
    try:
        return float(text)
    except:
        return 0.0

def scrape_daily_details(headless=True):
    """
    Scrape the daily details table from the Tadawul DetailedDaily report page.
    """
    url = "https://www.saudiexchange.sa/Resources/Reports-v2/DetailedDaily_en.html"
    driver = None
    data = []

    try:
        logger.info("🚀 Starting Daily Details Scraper...")
        driver = build_driver(headless)
        
        logger.info(f"🌍 Navigating to {url}")
        driver.get(url)
        
        # Wait for the table to render (the page is loaded by JavaScript)
        wait = WebDriverWait(driver, 45)
        for attempt in range(3):
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "table")))
                time.sleep(3)  # safety margin so all rows finish rendering
                tables = driver.find_elements(By.TAG_NAME, "table")
                if tables and len(tables) > 0:
                    break
            except Exception:
                if attempt < 2:
                    logger.warning(f"⚠️ Attempt {attempt+1}/3: Table not loaded yet. Refreshing after 5s...")
                    time.sleep(5)
                    driver.refresh()
                else:
                    logger.error("❌ All attempts timed out waiting for table to appear.")

        tables = driver.find_elements(By.TAG_NAME, "table")
        logger.info(f"🔍 Found {len(tables)} tables on the page.")
        
        target_table = None
        max_rows = 0
        
        # Find the companies table (usually the largest one)
        for tbl in tables:
            try:
                rows = tbl.find_elements(By.TAG_NAME, "tr")
                row_count = len(rows)
                logger.info(f"Table with {row_count} rows found.")
                
                # The companies table must have many rows (more than ~50)
                if row_count > 50:
                    # Extra check: does it contain the word Symbol or Company?
                    if "Symbol" in tbl.text or "Company" in tbl.text:
                        if row_count > max_rows:
                            max_rows = row_count
                            target_table = tbl
            except:
                continue
        
        if not target_table:
            logger.error("❌ Could not identify the Companies List table.")
            return []
            
        logger.info(f"✅ Target table identified with {max_rows} rows.")
        
        # Try scrolling the target table into view
        try:
            driver.execute_script("arguments[0].scrollIntoView();", target_table)
            time.sleep(1)
        except:
            pass

        # Extract rows from the selected table
        tbody = target_table.find_element(By.TAG_NAME, "tbody")
        rows = tbody.find_elements(By.TAG_NAME, "tr")
        
        logger.info(f"📊 Processing {len(rows)} rows from target table...")
        
        for i, row in enumerate(rows):
            cols = row.find_elements(By.TAG_NAME, "td")
            
            # Check if row has enough columns (at least 11 for Market Cap)
            if len(cols) < 11: 
                continue
            
            try:
                # First column is either the symbol or the company name
                col0_text = cols[0].text.strip()
                
                # If the first column is numeric, it is the Symbol
                if col0_text.isdigit():
                    symbol = col0_text
                    company = cols[1].text.strip()
                    
                    entry = {
                        "Symbol": symbol,
                        "Company": company,
                        "Open": clean_number(cols[2].text),
                        "Highest": clean_number(cols[3].text),
                        "Lowest": clean_number(cols[4].text),
                        "Close": clean_number(cols[5].text),
                        "Change %": clean_number(cols[6].text),
                        "Volume Traded": clean_number(cols[7].text),
                        "Value Traded": clean_number(cols[8].text),
                        "No. of Trades": clean_number(cols[9].text),
                        "Market Cap": clean_number(cols[10].text)
                    }
                    data.append(entry)
                    
                    # Debug sample
                    if len(data) == 1:
                        print(f"DEBUG FIRST ROW: {entry}")
            except Exception as e:
                # logger.warning(f"Skipping row {i}: {e}")
                continue
            
        logger.info(f"✅ Successfully scraped {len(data)} stocks.")
        
    except Exception as e:
        logger.error(f"❌ Error during scraping: {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        if driver:
            driver.quit()
            
    return data

if __name__ == "__main__":
    # Test the scraper
    results = scrape_daily_details(headless=False)  # headless=False so you can watch the browser work
    print(f"Sample data: {results[:2] if results else 'No data'}")
