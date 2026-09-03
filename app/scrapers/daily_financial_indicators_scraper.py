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
import re
from datetime import datetime

# إعداد الـ Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def build_driver(headless=True):
    """
    إعداد متصفح كروم بنفس إعدادات السكريبت القديم
    مع دعم Render/Docker deployment
    """
    import shutil

    chrome_bin_env = os.environ.get('CHROME_BIN')
    chromedriver_env = os.environ.get('CHROMEDRIVER_PATH')
    logger.info(f"🔍 DEBUG: CHROME_BIN env = {chrome_bin_env}")
    logger.info(f"🔍 DEBUG: CHROMEDRIVER_PATH env = {chromedriver_env}")

    render_chrome = '/opt/render/project/.chrome/chrome-linux64/chrome'
    render_driver = '/opt/render/project/.chrome/chromedriver-linux64/chromedriver'
    logger.info(f"🔍 DEBUG: Render Chrome exists = {os.path.exists(render_chrome)}")
    logger.info(f"🔍 DEBUG: Render ChromeDriver exists = {os.path.exists(render_driver)}")

    options = Options()

    # إعدادات أساسية للاستقرار
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
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    if headless:
        options.add_argument("--headless=new")

    # البحث عن Chrome binary
    chrome_bin = None

    for env_var in ['CHROME_BIN', 'GOOGLE_CHROME_BIN', 'CHROMIUM_BIN']:
        env_path = os.environ.get(env_var)
        if env_path and os.path.exists(env_path):
            chrome_bin = env_path
            logger.info(f"📍 Found Chrome from env {env_var}: {chrome_bin}")
            break

    if not chrome_bin:
        for chrome_name in ['google-chrome-stable', 'google-chrome', 'chromium-browser', 'chromium', 'chrome']:
            found_path = shutil.which(chrome_name)
            if found_path:
                chrome_bin = found_path
                logger.info(f"📍 Found Chrome in PATH: {chrome_bin}")
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
            '/snap/bin/chromium',
            '/app/.apt/usr/bin/google-chrome',
        ]
        for path in linux_chrome_paths:
            if os.path.exists(path):
                chrome_bin = path
                logger.info(f"📍 Found Chrome at common path: {chrome_bin}")
                break

    if chrome_bin:
        options.binary_location = chrome_bin
        logger.info(f"✅ Using Chrome binary: {chrome_bin}")
    else:
        logger.warning("⚠️ Chrome binary not found! Will try default ChromeDriver behavior.")

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
        try:
            logger.info(f"📍 Found explicit ChromeDriver at: {chromedriver_path}")
            service = Service(chromedriver_path)
            driver = webdriver.Chrome(service=service, options=options)
            logger.info("✅ Chrome WebDriver initialized successfully (via explicit path)")
            return driver
        except Exception as e:
            logger.warning(f"⚠️ Explicit ChromeDriver failed, falling back to manager: {e}")

    try:
        logger.info("🔄 Attempting to use webdriver-manager...")
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        logger.info("✅ Chrome WebDriver initialized successfully (via webdriver-manager)")
        return driver
    except Exception as e:
        logger.error(f"❌ All methods failed to initialize Chrome: {e}")
        raise


def clean_number(text):
    """
    تنظيف الأرقام من الفواصل والنسب المئوية
    القيم زي '-' أو 'M' أو الفاضية بترجع None
    """
    if text is None:
        return None
    text = text.strip()
    if text in ("", "-", "—", "M", "*", "**"):
        return None
    text = text.replace(',', '').replace('%', '').strip()
    # إزالة علامات * الخاصة بالإيقاف/الشطب لو اتلزقت بالرقم
    text = text.replace('*', '').strip()
    try:
        return float(text)
    except ValueError:
        return None


def clean_symbol_or_company(text):
    """تنظيف اسم الشركة/القطاع من علامات * (موقوفة) و ** (مشطوبة)"""
    if text is None:
        return ""
    return re.sub(r'\s*\*+\s*$', '', text.strip()).strip()


def scrape_daily_financial_indicators(headless=True):
    """
    سحب بيانات صفحة Daily Financial Indicators من موقع تداول السعودية.
    ترجع 3 قوائم: بيانات الشركات، ملخص القطاعات، وإجمالي السوق.
    """
    url = "https://www.saudiexchange.sa/Resources/Reports-v2/DailyFinancialIndicators_en.html"
    driver = None

    companies = []
    sectors = []
    market_total = None
    report_date = None

    try:
        logger.info("🚀 Starting Daily Financial Indicators Scraper...")
        driver = build_driver(headless)

        logger.info(f"🌍 Navigating to {url}")
        driver.get(url)

        # استنى لحد ما الجدول يظهر — 3 محاولات مع تأخير متزايد
        wait = WebDriverWait(driver, 45)
        table_found = False
        for attempt in range(3):
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "table")))
                table_found = True
                break
            except Exception:
                if attempt < 2:
                    logger.warning(f"⚠️ Attempt {attempt+1}/3 timed out waiting for table. Refreshing after {10*(attempt+1)}s sleep...")
                    time.sleep(10 * (attempt + 1))
                    driver.refresh()
                else:
                    logger.error("❌ All 3 attempts to find table failed. Aborting scrape.")
                    raise
        time.sleep(2)  # هامش أمان لتحميل الصفوف بالكامل


        # محاولة استخراج تاريخ التقرير (مثال: "Main Market 2026/07/01")
        try:
            page_text = driver.find_element(By.TAG_NAME, "body").text
            date_match = re.search(r'Main Market\s+(\d{4}/\d{2}/\d{2})', page_text)
            if date_match:
                report_date = date_match.group(1)
                logger.info(f"📅 Report date found: {report_date}")
        except Exception:
            pass

        tables = driver.find_elements(By.TAG_NAME, "table")
        logger.info(f"🔍 Found {len(tables)} tables on the page.")

        target_table = None
        max_rows = 0

        for tbl in tables:
            try:
                rows = tbl.find_elements(By.TAG_NAME, "tr")
                row_count = len(rows)
                logger.info(f"Table with {row_count} rows found.")

                if row_count > 50 and ("Symbol" in tbl.text or "Company" in tbl.text):
                    if row_count > max_rows:
                        max_rows = row_count
                        target_table = tbl
            except Exception:
                continue

        if not target_table:
            logger.error("❌ Could not identify the Financial Indicators table.")
            return companies, sectors, market_total, report_date

        logger.info(f"✅ Target table identified with {max_rows} rows.")

        try:
            driver.execute_script("arguments[0].scrollIntoView();", target_table)
            time.sleep(1)
        except Exception:
            pass

        # استخراج الصفوف
        try:
            tbody = target_table.find_element(By.TAG_NAME, "tbody")
            rows = tbody.find_elements(By.TAG_NAME, "tr")
        except Exception:
            rows = target_table.find_elements(By.TAG_NAME, "tr")

        logger.info(f"📊 Processing {len(rows)} rows from target table...")

        columns_map = [
            "Close Price", "Issued Shares", "Net Income", "Shareholders Equity",
            "Market Cap", "Market Cap %", "EPS", "P/E Ratio", "Book Value", "P/B Ratio"
        ]

        for i, row in enumerate(rows):
            cols = row.find_elements(By.TAG_NAME, "td")
            if len(cols) < 11:
                continue  # صف هيدر أو صف غير مكتمل

            try:
                col0_text = cols[0].text.strip()
                if not col0_text:
                    continue

                # -------- صف الشركة (أول عمود رقم = Symbol) --------
                if col0_text.isdigit():
                    symbol = col0_text
                    company = clean_symbol_or_company(cols[1].text)
                    suspended = '*' in cols[1].text and '**' not in cols[1].text
                    delisted = '**' in cols[1].text

                    entry = {
                        "Symbol": symbol,
                        "Company": company,
                        "Suspended": suspended,
                        "Delisted": delisted,
                    }
                    for name, cell in zip(columns_map, cols[2:12]):
                        entry[name] = clean_number(cell.text)
                    companies.append(entry)

                    if len(companies) == 1:
                        print(f"DEBUG FIRST COMPANY ROW: {entry}")

                # -------- صف "Market" الإجمالي --------
                elif col0_text.strip().lower() == "market":
                    market_total = {}
                    for name, cell in zip(columns_map, cols[2:12]):
                        market_total[name] = clean_number(cell.text)
                    logger.info(f"📈 Market total row captured: {market_total}")

                # -------- صف ملخص قطاع (اسم قطاع بدل رقم) --------
                else:
                    sector_name = clean_symbol_or_company(col0_text)
                    entry = {"Sector": sector_name}
                    for name, cell in zip(columns_map, cols[2:12]):
                        entry[name] = clean_number(cell.text)
                    sectors.append(entry)

            except Exception as e:
                logger.warning(f"Skipping row {i}: {e}")
                continue

        logger.info(f"✅ Successfully scraped {len(companies)} companies and {len(sectors)} sector summaries.")

    except Exception as e:
        logger.error(f"❌ Error during scraping: {e}")
        import traceback
        traceback.print_exc()

    finally:
        if driver:
            driver.quit()

    return companies, sectors, market_total, report_date


def save_to_csv(companies, sectors, market_total, report_date, output_dir="."):
    """حفظ النتائج في ملفات CSV"""
    date_tag = (report_date or datetime.now().strftime("%Y-%m-%d")).replace("/", "-")

    companies_path = os.path.join(output_dir, f"daily_financial_indicators_companies_{date_tag}.csv")
    sectors_path = os.path.join(output_dir, f"daily_financial_indicators_sectors_{date_tag}.csv")

    if companies:
        pd.DataFrame(companies).to_csv(companies_path, index=False, encoding="utf-8-sig")
        logger.info(f"💾 Saved companies data to {companies_path}")

    if sectors:
        pd.DataFrame(sectors).to_csv(sectors_path, index=False, encoding="utf-8-sig")
        logger.info(f"💾 Saved sector summary data to {sectors_path}")

    if market_total:
        market_row = pd.DataFrame([market_total])
        market_path = os.path.join(output_dir, f"daily_financial_indicators_market_total_{date_tag}.csv")
        market_row.to_csv(market_path, index=False, encoding="utf-8-sig")
        logger.info(f"💾 Saved market total row to {market_path}")

    return companies_path, sectors_path


def save_to_db(companies):
    """حفظ النتائج في قاعدة البيانات لتغذية صفحات Valuation"""
    import sys
    import os
    # Add backend directory to sys.path if not there
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
    
    from app.core.database import SessionLocal
    from app.models.tasi_components import TasiComponent

    db = SessionLocal()
    try:
        updated = 0
        added = 0
        
        # 1. Fetch all existing components in one single roundtrip query
        existing_components = {c.symbol: c for c in db.query(TasiComponent).all()}
        
        for comp in companies:
            symbol = comp.get("Symbol")
            if not symbol:
                continue

            db_comp = existing_components.get(symbol)
            if not db_comp:
                db_comp = TasiComponent(symbol=symbol)
                db.add(db_comp)
                existing_components[symbol] = db_comp
                added += 1
            else:
                updated += 1

            db_comp.company_name = comp.get("Company", db_comp.company_name)
            
            # Map scraped keys to DB columns
            cp = comp.get("Close Price")
            if cp is not None:
                db_comp.current_price = cp
                
            mc = comp.get("Market Cap")
            if mc is not None:
                db_comp.market_cap = mc
                
            mcp = comp.get("Market Cap %")
            if mcp is not None:
                db_comp.weight_in_index = mcp
                
            eps = comp.get("EPS")
            if eps is not None:
                db_comp.eps = eps
                
            pe = comp.get("P/E Ratio")
            if pe is not None:
                db_comp.pe_ratio = pe
                
        db.commit()
        logger.info(f"✅ DB Update Complete: Added {added}, Updated {updated} companies.")
    except Exception as e:
        db.rollback()
        logger.error(f"❌ DB Error: {e}")
    finally:
        db.close()


def run_scraper_and_save_to_db():
    companies, sectors, market_total, report_date = scrape_daily_financial_indicators(headless=True)
    if companies:
        save_to_db(companies)
    return True


if __name__ == "__main__":
    companies, sectors, market_total, report_date = scrape_daily_financial_indicators(headless=True)

    print(f"\nReport date: {report_date}")
    print(f"Companies scraped: {len(companies)}")
    print(f"Sector summaries scraped: {len(sectors)}")
    print(f"Market total: {market_total}")

    if companies:
        print("\nSample company rows:")
        for c in companies[:3]:
            print(c)

    save_to_csv(companies, sectors, market_total, report_date, output_dir=".")
    
    # تحديث قاعدة البيانات 
    if companies:
        save_to_db(companies)