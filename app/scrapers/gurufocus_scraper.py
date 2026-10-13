from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException
from bs4 import BeautifulSoup
import time
from datetime import datetime, timedelta
import json
import logging

from app.core.database import SessionLocal
from app.models.economic_indicators import EconomicIndicator

logger = logging.getLogger(__name__)

def _guru_fail(error: str) -> dict:
    """Failure shape understood by job_tracking.with_result_handling."""
    return {"ok": False, "rows": 0, "stage_failures": [
        {"stage": "gurufocus", "error": error[:400]}]}


def scrape_gurufocus_indicator(url: str, indicator_code: str, mode: str = "incremental", max_pages: int = None) -> dict:
    """
    Generic scraper for GuruFocus indicators using Selenium.
    Returns {"ok": bool, "rows": n_written} for job_tracking.
    
    mode:
      - "incremental": سحب أحدث البيانات فقط (آخر 3 صفحات ≈ شهرين)
      - "full": سحب كل البيانات التاريخية
    """
    # ── تحديد max_pages بناءً على الوضع ──
    if max_pages is None:
        if mode == "incremental":
            max_pages = 3  # آخر 3 صفحات ≈ شهرين من البيانات
            logger.info(f"📋 وضع incremental: سيتم سحب أحدث {max_pages} صفحات فقط")
        else:  # full
            logger.info("📋 وضع full: سيتم سحب كل الصفحات التاريخية")
    logger.info(f"🚀 Starting GuruFocus Scraper for {indicator_code} (mode={mode})...")

    # 2026-10-13: use the shared hardened driver — page_load_strategy='eager'
    # + 300s chromedriver client timeout + Render Chrome/Driver paths
    # (app/services/daily_detailed_scraper.build_driver). The old inline
    # Options() relied on Selenium Manager's own Chrome discovery with the
    # default 'normal' load strategy, which is exactly the combination that
    # tripped 120s read timeouts on the other Selenium jobs.
    from app.services.daily_detailed_scraper import build_driver
    driver = build_driver(headless=True)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(30)
    
    try:
        try:
            driver.get(url)
        except TimeoutException:
            logger.warning("⚠️ Page load timed out, parsing whatever rendered...")
        except Exception as e:
            # urllib3 ReadTimeoutError is neither TimeoutException nor
            # WebDriverException — do not abort; the WebDriverWait below
            # decides whether the table actually appeared.
            logger.warning(f"Navigation error ({type(e).__name__}: {e}), attempting partial parse...")
        logger.info("⏳ Waiting for the historical data table to load...")
        
        # Wait until the history table is present (Checking for rows)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//table//tr[td[contains(text(), '-')]]"))
        )
        
        time.sleep(3) # Wait for page hydration
        
        # 1. Attempt to extract ALL Historical Data directly from the global Javascript state (__NUXT__ or similar)
        logger.info("🕵️‍♂️ Attempting to extract full history from JS window object (__NUXT__)...")
        
        extract_script = """
        function findSeries(obj, depth) {
            if(depth > 12 || !obj) return [];
            var best = [];
            if(Array.isArray(obj)) {
                // Check if this array is our target: array of arrays [timestamp, value]
                if(obj.length > 50 && Array.isArray(obj[0]) && obj[0].length >= 2 && typeof obj[0][0] === 'number' && obj[0][0] > 10000000000) {
                    return obj;
                }
                for(var i=0; i<obj.length; i++) {
                    var res = findSeries(obj[i], depth+1);
                    if(res.length > best.length) best = res;
                }
            } else if(typeof obj === 'object') {
                for(var k in obj) {
                    var res = findSeries(obj[k], depth+1);
                    if(res.length > best.length) best = res;
                }
            }
            return best;
        }
        
        try {
            var data1 = findSeries(window.__NUXT__, 0);
            if(data1 && data1.length > 100) return data1;
            
            var data2 = findSeries(window.Highcharts, 0);
            if(data2 && data2.length > 100) return data2;
            
            return null;
        } catch(e) { return null; }
        """
        
        chart_data = driver.execute_script(extract_script)
        
        data_records = []
        if chart_data and isinstance(chart_data, list) and len(chart_data) > 20:
            logger.info(f"✅ Extracted {len(chart_data)} records directly from the hidden Javascript state!")
            for point in chart_data:
                try:
                    ts, val = point[0], point[1]
                    if val is not None:
                        # Some timestamps are in seconds, some in milliseconds
                        ts_val = ts / 1000.0 if ts > 100000000000 else ts
                        dt = datetime.fromtimestamp(ts_val).date()
                        data_records.append({"date": dt, "value": float(val)})
                except Exception as e:
                    continue
        else:
            logger.warning("⚠️ Deep extraction failed. Falling back to parsing ALL pages of the HTML table via pagination.")
            
            # Scroll down to trigger lazy-loaded content (Historical Data section)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
            time.sleep(3)
            
            # Try to find and click on "Historical Data" heading or tab if present
            try:
                hist_section = driver.find_element(By.XPATH, "//*[contains(text(), 'Historical Data')]")
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", hist_section)
                time.sleep(2)
            except:
                pass
            data_records_dict = {} # Use dict to ensure uniqueness by date: {date: {value, yoy}}
            
            page_num = 1
            while True:
                # Fallback to simple table scraping
                html = driver.page_source
                soup = BeautifulSoup(html, 'html.parser')
                
                tables = soup.find_all("table")
                target_table = None
                for table in tables:
                    headers = [th.text.strip().lower() for th in table.find_all("th")]
                    has_date = any("date" in h for h in headers)
                    has_enough_cols = len(headers) >= 2
                    if has_date and has_enough_cols:
                        target_table = table
                        break
                
                # Fallback: if no table found by <th>, try finding a table with rows containing dates
                if not target_table:
                    for table in tables:
                        first_row = table.find("tr")
                        if first_row:
                            cells = first_row.find_all(["td", "th"])
                            cell_texts = [c.text.strip().lower() for c in cells]
                            if any("date" in c for c in cell_texts):
                                target_table = table
                                break
                        
                if target_table:
                    # Handle tables with or without <tbody>
                    tbody = target_table.find("tbody")
                    rows = tbody.find_all("tr") if tbody else target_table.find_all("tr")
                    for row in rows:
                        cols = row.find_all("td")
                        if len(cols) >= 2:
                            date_str = cols[0].text.strip()
                            val_str = cols[1].text.strip().replace('%', '').replace(',', '')
                            yoy_val = None
                            if len(cols) >= 3:
                                yoy_str = cols[2].text.strip().replace('%', '').replace('+', '')
                                try:
                                    yoy_val = float(yoy_str)
                                except:
                                    yoy_val = None
                            try:
                                dt = datetime.strptime(date_str, "%Y-%m-%d").date()
                                val = float(val_str)
                                data_records_dict[dt] = {"value": val, "yoy": yoy_val}
                            except Exception as e:
                                continue
                else:
                    logger.warning(f"⚠️ No matching table found on page {page_num}!")
                                
                logger.info(f"📄 Parsed page {page_num}... Total unique records so far: {len(data_records_dict)}")
                
                # Check if we reached the maximum pages requested
                if max_pages and page_num >= max_pages:
                    logger.info(f"🛑 Reached max_pages limit ({max_pages}). Stopping pagination for incremental update.")
                    break
                
                # Check for Next button to go to the next page
                try:
                    # Close any random popups that might appear and block the DOM
                    try:
                        close_btn = driver.find_element(By.CSS_SELECTOR, "i.el-icon-close")
                        driver.execute_script("arguments[0].click();", close_btn)
                    except:
                        pass
                        
                    # GuruFocus uses Element UI or similar class 'btn-next' for pagination
                    next_btn = driver.find_element(By.CSS_SELECTOR, "button.btn-next")
                    
                    # If button is disabled or we hit the end
                    if next_btn.get_attribute("disabled"):
                        logger.info("Next button is disabled. Reached the end of pagination.")
                        break
                        
                    # Click next page via Javascript to avoid overlay issues
                    driver.execute_script("arguments[0].click();", next_btn)
                    
                    import random
                    sleep_time = 1.5 + random.uniform(0.5, 1.5)
                    time.sleep(sleep_time) # Wait for table to reload with a bit of jitter
                    page_num += 1
                    
                    # Failsafe if it loops > 2000 pages
                    if page_num > 2000:
                        break
                except Exception as e:
                    logger.warning(f"Stopped pagination at page {page_num}. Reason: {str(e)[:100]}")
                    break
                    
            # We will calculate YOY later using the database history
            for dt, record in data_records_dict.items():
                data_records.append({"date": dt, "value": record["value"], "yoy": record.get("yoy")})

                    
        if not data_records:
            logger.warning("⚠️ No valid records parsed from table.")
            return _guru_fail("no valid records parsed from the GuruFocus page")
            
        logger.info(f"✅ Successfully parsed {len(data_records)} records.")
        
        # Save to Database (UPSERT: insert new + update existing with YOY)
        db = SessionLocal()
        try:
            existing_records = {
                r.report_date: r for r in db.query(EconomicIndicator).filter(EconomicIndicator.indicator_code == indicator_code).all()
            }
            
            # Build a lookup dictionary combining DB records and newly scraped records
            # so we can calculate YOY using full historical context.
            full_history_lookup = {}
            for dt, r in existing_records.items():
                full_history_lookup[dt] = r.value
            for record in data_records:
                full_history_lookup[record["date"]] = record["value"]
                
            new_objs = []
            updated_count = 0
            
            for record in data_records:
                dt = record["date"]
                current_value = record["value"]
                yoy_val = record.get("yoy")
                
                # If YOY is missing, calculate it using the full history lookup
                if yoy_val is None:
                    try:
                        target_date = dt.replace(year=dt.year - 1)
                    except ValueError:
                        target_date = dt.replace(year=dt.year - 1, day=28)
                        
                    last_year_value = None
                    for i in range(10): # Check up to 10 days before target date
                        check_date = target_date - timedelta(days=i)
                        if check_date in full_history_lookup:
                            last_year_value = full_history_lookup[check_date]
                            break
                            
                    if last_year_value and last_year_value != 0:
                        yoy_val = round(((current_value - last_year_value) / last_year_value) * 100, 2)
                
                if dt in existing_records:
                    existing = existing_records[dt]
                    # Update existing record if value changed OR if YOY changed/was calculated
                    if existing.value != current_value:
                        existing.value = current_value
                    if yoy_val is not None and existing.yoy_pct != yoy_val:
                        existing.yoy_pct = yoy_val
                        updated_count += 1
                else:
                    new_objs.append(EconomicIndicator(
                        report_date=dt,
                        indicator_code=indicator_code,
                        value=current_value,
                        yoy_pct=yoy_val
                    ))
            
            if new_objs:
                db.bulk_save_objects(new_objs)
            
            db.commit()
            logger.info(f"💾 Inserted {len(new_objs)} NEW records. Updated YOY for {updated_count} existing records.")
            return {"ok": True, "rows": len(new_objs) + updated_count,
                    "inserted": len(new_objs), "yoy_updated": updated_count}
        finally:
            db.close()

    except Exception as e:
        logger.error(f"❌ Scraper Failed: {e}")
        return _guru_fail(str(e))
    finally:
        driver.quit()

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    parser = argparse.ArgumentParser(description="GuruFocus Indicator Scraper")
    parser.add_argument("--url", required=True, help="رابط المؤشر على GuruFocus")
    parser.add_argument("--code", required=True, help="كود المؤشر (مثلاً SP500_EY)")
    parser.add_argument("--mode", default="incremental", choices=["incremental", "full"],
                        help="incremental: آخر صفحات | full: كل التاريخ")
    args = parser.parse_args()
    scrape_gurufocus_indicator(url=args.url, indicator_code=args.code, mode=args.mode)
