import gc
import logging
import time
from datetime import datetime
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from app.services.daily_detailed_scraper import build_driver

logger = logging.getLogger(__name__)

def parse_date_from_text(text):
    """
    Extracts date from 'Last Update Date : 2026-04-02'
    """
    try:
        parts = text.split(":")
        if len(parts) > 1:
            date_str = parts[-1].strip()
            return datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception as e:
        logger.error(f"Could not parse date from text: {text} - {e}")
    return datetime.now().date()

def extract_table_rows(driver, url, wait_time=5):
    """
    Navigates to URL, waits, finding the date and table data.
    Returns (report_date, headers, rows). On timeout returns empty data.
    """
    try:
        driver.get(url)
    except TimeoutException:
        logger.warning(f"Page load timed out for {url}, attempting partial parse...")
    except WebDriverException as e:
        logger.error(f"WebDriver error loading {url}: {e}")
        return datetime.now().date(), [], []
    # Wait for tables to render dynamically via JavaScript
    try:
        WebDriverWait(driver, max(wait_time, 15)).until(
            EC.presence_of_element_located((By.TAG_NAME, "table"))
        )
    except Exception:
        pass
    time.sleep(2)
    
    # Try finding the Last Update Date
    report_date = datetime.now().date()
    try:
        date_elements = driver.find_elements(By.XPATH, "//*[contains(text(), 'Last Update Date')]")
        for el in date_elements:
            if "Last Update Date" in el.text:
                parsed = parse_date_from_text(el.text)
                if parsed:
                    report_date = parsed
                    break
    except Exception as e:
        pass
        
    tables = driver.find_elements(By.TAG_NAME, "table")
    target_table = None
    max_rows = 0
    
    for tbl in tables:
        try:
            tbody_elems = tbl.find_elements(By.TAG_NAME, "tbody")
            rows = tbody_elems[0].find_elements(By.TAG_NAME, "tr") if tbody_elems else tbl.find_elements(By.TAG_NAME, "tr")
            if len(rows) > max_rows:
                max_rows = len(rows)
                target_table = tbl
        except:
            continue
            
    if not target_table or max_rows == 0:
        return report_date, [], []
        
    # Get table headers if needed (for Buyback dynamic columns)
    headers = []
    try:
        thead = target_table.find_element(By.TAG_NAME, "thead")
        header_rows = thead.find_elements(By.TAG_NAME, "tr")
        # Just grab the last header row text as column names for simplicity if needed
        if header_rows:
            th_elements = header_rows[-1].find_elements(By.TAG_NAME, "th")
            headers = [th.text.strip() for th in th_elements]
    except Exception as e:
        pass

    try:
        driver.execute_script("arguments[0].scrollIntoView();", target_table)
        time.sleep(1)
    except:
        pass

    tbody = target_table.find_element(By.TAG_NAME, "tbody")
    rows = tbody.find_elements(By.TAG_NAME, "tr")
    
    parsed_rows = []
    for row in rows:
        cols = row.find_elements(By.TAG_NAME, "td")
        col_texts = [col.text.strip().replace('\n', ' ') for col in cols]
        if any(col_texts): # Only add non-empty rows
            parsed_rows.append(col_texts)
            
    return report_date, headers, parsed_rows

def scrape_substantial_shareholders(driver):
    url = 'https://www.saudiexchange.sa/Resources/Reports-v2/MajorStakeHoldersPage_en.html'
    data = []
    try:
        try:
            driver.get(url)
        except TimeoutException:
            logger.warning("Page load timed out for Substantial Shareholders, attempting partial parse...")
        except WebDriverException as e:
            logger.error(f"WebDriver error loading Substantial Shareholders: {e}")
            return data
        # Wait for table to render dynamically
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.TAG_NAME, "table"))
            )
        except Exception:
            pass
        time.sleep(3)
        
        # Get report date
        report_date = datetime.now().date()
        try:
            date_elements = driver.find_elements(By.XPATH, "//*[contains(text(), 'Last Update Date')]")
            for el in date_elements:
                if "Last Update Date" in el.text:
                    parsed = parse_date_from_text(el.text)
                    if parsed:
                        report_date = parsed
                        break
        except:
            pass
        
        # Find the largest table
        tables = driver.find_elements(By.TAG_NAME, "table")
        target_table = None
        max_rows = 0
        for tbl in tables:
            try:
                tbody_elems = tbl.find_elements(By.TAG_NAME, "tbody")
                rows = tbody_elems[0].find_elements(By.TAG_NAME, "tr") if tbody_elems else tbl.find_elements(By.TAG_NAME, "tr")
                if len(rows) > max_rows:
                    max_rows = len(rows)
                    target_table = tbl
            except:
                continue
        
        if not target_table or max_rows == 0:
            logger.error("No table found for Substantial Shareholders")
            return data
        
        tbody = target_table.find_element(By.TAG_NAME, "tbody")
        rows = tbody.find_elements(By.TAG_NAME, "tr")
        
        current_company = ""
        for row in rows:
            cols = row.find_elements(By.TAG_NAME, "td")
            if not cols:
                continue
            
            if len(cols) == 1:
                # This is a company header row
                current_company = cols[0].text.strip().replace('\n', ' ')
            elif len(cols) >= 6 and current_company:
                # These are the shareholder rows under the current company
                data.append({
                    "report_date": report_date,
                    "company_name": current_company,
                    "shareholder_name": cols[0].text.strip().replace('\n', ' '),
                    "holding_percent_last_day": cols[1].text.strip(),
                    "holding_percent_previous_day": cols[2].text.strip(),
                    "change": cols[3].text.strip(),
                    "managed_by_authorized_trading_day": cols[4].text.strip(),
                    "managed_by_authorized_previous_day": cols[5].text.strip(),
                })
        
        logger.info(f"Scraped {len(data)} Substantial Shareholders records.")
    except Exception as e:
        logger.error(f"Error scraping Substantial Shareholders: {e}")
    finally:
        # Free page memory
        try:
            driver.get("about:blank")
        except Exception:
            pass
        gc.collect()
    return data

def scrape_net_short_positions(driver):
    url = 'https://www.saudiexchange.sa/Resources/Reports-v2/NetShortPositions_en.html'
    data = []
    try:
        report_date, _, rows = extract_table_rows(driver, url)
        for cols in rows:
            if len(cols) >= 5:
                data.append({
                    "report_date": report_date,
                    "symbol": cols[0],
                    "company": cols[1],
                    "percent_over_outstanding": cols[2],
                    "percent_over_free_float": cols[3],
                    "ratio_over_avg_daily": cols[4],
                })
        logger.info(f"Scraped {len(data)} Net Short Positions records.")
    except Exception as e:
        logger.error(f"Error scraping Net Short Positions: {e}")
    finally:
        try:
            driver.get("about:blank")
        except Exception:
            pass
        gc.collect()
    return data

def scrape_foreign_headroom(driver):
    url = 'https://www.saudiexchange.sa/wps/portal/saudiexchange/newsandreports/reports-publications/market-reports/foreign-headroom-main'
    data = []
    try:
        report_date, _, rows = extract_table_rows(driver, url, wait_time=10) # Portal takes longer
        for cols in rows:
            if len(cols) >= 5:
                data.append({
                    "report_date": report_date,
                    "symbol": cols[0],
                    "company": cols[1],
                    "foreign_limit": cols[2],
                    "actual_foreign_ownership": cols[3],
                    "ownership_room": cols[4],
                })
        logger.info(f"Scraped {len(data)} Foreign Headroom records.")
    except Exception as e:
        logger.error(f"Error scraping Foreign Headroom: {e}")
    finally:
        try:
            driver.get("about:blank")
        except Exception:
            pass
        gc.collect()
    return data

def scrape_share_buybacks(driver):
    url = 'https://www.saudiexchange.sa/Resources/Reports-v2/ShareBuyback_en.html'
    data = []
    try:
        report_date, headers, rows = extract_table_rows(driver, url)
        for cols in rows:
            if len(cols) >= 2:
                data.append({
                    "report_date": report_date,
                    "symbol": cols[0],
                    "company": cols[1],
                    "data": {
                        "headers": headers,
                        "values": list(cols)
                    }
                })
        logger.info(f"Scraped {len(data)} Share Buybacks records.")
    except Exception as e:
        logger.error(f"Error scraping Share Buybacks: {e}")
    finally:
        try:
            driver.get("about:blank")
        except Exception:
            pass
        gc.collect()
    return data

def scrape_sbl_positions(driver):
    url = 'https://www.saudiexchange.sa/Resources/Reports-v2/SBLReport_en.html'
    data = []
    try:
        report_date, _, rows = extract_table_rows(driver, url)
        for cols in rows:
            if len(cols) >= 5:
                data.append({
                    "report_date": report_date,
                    "symbol": cols[0],
                    "company": cols[1],
                    "total_issued_shares": cols[2],
                    "lent_asset_quantity": cols[3],
                    "percent_of_lent_asset": cols[4],
                })
        logger.info(f"Scraped {len(data)} SBL Positions records.")
    except Exception as e:
        logger.error(f"Error scraping SBL Positions: {e}")
    finally:
        try:
            driver.get("about:blank")
        except Exception:
            pass
        gc.collect()
    return data
