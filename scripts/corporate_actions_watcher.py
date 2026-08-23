# -*- coding: utf-8 -*-
"""
Corporate Actions Watcher — Saudi Exchange (إجراءات الشركات / المصدر)
=====================================================================
Monitors:
    https://www.saudiexchange.sa/wps/portal/saudiexchange/newsandreports/issuer-financial-calendars/corporate-actions?locale=en

Detects new Corporate Actions (Capital Reduction, Bonus Shares, Stock Splits, etc.)
and automatically triggers the historical price update and indicators recalculation pipeline.
"""

import sys
import os
import time
import json
import glob
import logging
import subprocess
from datetime import datetime, date
from pathlib import Path
from typing import List, Dict, Any, Optional

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Setup Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal, engine
from app.models.Corporate_actions import CorporateAction

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

URL = (
    "https://www.saudiexchange.sa/wps/portal/saudiexchange/newsandreports/"
    "issuer-financial-calendars/corporate-actions?locale=en"
)

AUTO_ADJUST_TYPES = {
    "Capital Reduction",
    "Bonus Shares",
    "Forward Shares Split",
    "Reverse Shares Split",
    "Stock Split",
}

NEEDS_REVIEW_TYPES = {
    "Rights Issue",
    "Capital Increase",
    "Capital Increase – Debt Conversion",
    "Capital Increase - Offering Shares with Suspension of Right Issue",
    "Acquisition",
    "Merge",
    "Fund Units Cancellation",
    "Unit Splits",
    "Increase of The Total Value of The Fund Assets",
}


def classify(issue_type: str) -> str:
    issue_type = (issue_type or "").strip()
    if issue_type in AUTO_ADJUST_TYPES:
        return "AUTO_ADJUST"
    return "NEEDS_REVIEW"


def parse_date(text: str) -> Optional[date]:
    text = (text or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_capital(text: str) -> Optional[int]:
    if not text:
        return None
    cleaned = text.replace("^", "").replace(",", "").replace("﷼", "").replace("SAR", "").strip()
    try:
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def build_driver(headless: bool = True) -> webdriver.Chrome:
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    if headless:
        options.add_argument("--headless=new")

    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        return driver
    except Exception as e:
        logger.warning(f"webdriver_manager failed ({e}), searching cache...")

    cached = glob.glob(os.path.expanduser("~/.wdm/drivers/chromedriver/**/chromedriver.exe"), recursive=True)
    if cached:
        cached.sort(key=os.path.getmtime, reverse=True)
        return webdriver.Chrome(service=Service(cached[0]), options=options)

    return webdriver.Chrome(options=options)


def fetch_corporate_actions() -> List[Dict[str, Any]]:
    logger.info(f"🌍 Opening Saudi Exchange Corporate Actions page: {URL}")
    driver = build_driver(headless=True)
    rows_out: List[Dict[str, Any]] = []
    
    try:
        driver.get(URL)
        time.sleep(6)
        
        # Click on Corporate Actions tab if it is a tab
        try:
            tab_elements = driver.find_elements(By.XPATH, "//*[contains(text(), 'Corporate Actions')]")
            for elem in tab_elements:
                if elem.is_displayed():
                    try:
                        elem.click()
                        time.sleep(3)
                        break
                    except Exception:
                        pass
        except Exception:
            pass

        # Scroll to ensure table loads
        driver.execute_script("window.scrollTo(0, 500);")
        time.sleep(3)

        # 1. First try: Read from DOM rows directly
        row_elements = driver.find_elements(By.XPATH, "//table//tr[td]")
        logger.info(f"🔍 Found {len(row_elements)} raw table rows in DOM")
        
        for tr in row_elements:
            cells = tr.find_elements(By.TAG_NAME, "td")
            if len(cells) < 6:
                continue
            
            cell_texts = [c.text.strip() for c in cells]
            symbol = cell_texts[0]
            if not (symbol.isdigit() and len(symbol) in (4, 5)):
                continue

            company_name = cell_texts[1]
            announcement_date = parse_date(cell_texts[2]) if len(cell_texts) >= 7 else None
            issue_type = cell_texts[3] if len(cell_texts) >= 7 else cell_texts[2]
            eligibility_date = parse_date(cell_texts[4]) if len(cell_texts) >= 7 else parse_date(cell_texts[3])
            
            new_cap_idx = 5 if len(cell_texts) >= 7 else 4
            prev_cap_idx = 6 if len(cell_texts) >= 7 else 5
            
            new_cap = parse_capital(cell_texts[new_cap_idx]) if len(cell_texts) > new_cap_idx else None
            prev_cap = parse_capital(cell_texts[prev_cap_idx]) if len(cell_texts) > prev_cap_idx else None

            if not eligibility_date:
                continue

            rows_out.append({
                "symbol": symbol,
                "company_name": company_name,
                "recommendation_announcement_date": announcement_date,
                "issue_type": issue_type,
                "eligibility_date": eligibility_date,
                "new_capital": new_cap,
                "previous_capital": prev_cap,
                "classification": classify(issue_type),
            })

        # 2. Second try: Intercept network logs for JSON payload if DOM was empty
        if not rows_out:
            logger.info("📡 Checking browser performance network logs for JSON data...")
            try:
                logs = driver.get_log('performance')
                for entry in logs:
                    try:
                        msg = json.loads(entry['message'])['message']
                        if msg['method'] == 'Network.responseReceived':
                            resp_url = msg['params']['response']['url']
                            if 'corporate-actions' in resp_url or 'CompaniesDividendsPortlet' in resp_url or 'populate' in resp_url:
                                req_id = msg['params']['requestId']
                                try:
                                    body_res = driver.execute_cdp_cmd('Network.getResponseBody', {'requestId': req_id})
                                    body = body_res.get('body', '')
                                    if body and body.startswith('{'):
                                        data_json = json.loads(body)
                                        data_list = data_json.get('data') or data_json.get('aaData') or []
                                        for item in data_list:
                                            if isinstance(item, dict):
                                                sym = str(item.get('symbol') or item.get('companySymbol') or '')
                                                if sym.isdigit():
                                                    rows_out.append({
                                                        "symbol": sym,
                                                        "company_name": item.get('companyName') or item.get('companyNameEn'),
                                                        "recommendation_announcement_date": parse_date(str(item.get('recommendationDate', ''))),
                                                        "issue_type": item.get('issueType') or item.get('actionType'),
                                                        "eligibility_date": parse_date(str(item.get('eligibilityDate', ''))),
                                                        "new_capital": parse_capital(str(item.get('newCapital', ''))),
                                                        "previous_capital": parse_capital(str(item.get('previousCapital', ''))),
                                                        "classification": classify(item.get('issueType') or item.get('actionType', '')),
                                                    })
                                except Exception:
                                    pass
                    except Exception:
                        continue
            except Exception as e:
                logger.warning(f"Network log search exception: {e}")

        # Deduplicate rows_out by (symbol, issue_type, eligibility_date)
        unique_map = {}
        for r in rows_out:
            k = (r["symbol"], r["issue_type"], r["eligibility_date"])
            if k not in unique_map and r["eligibility_date"] is not None:
                unique_map[k] = r

        return list(unique_map.values())
    finally:
        driver.quit()


def ensure_table_exists():
    try:
        CorporateAction.__table__.create(engine, checkfirst=True)
        logger.info("✅ Database table 'corporate_actions' is verified/created.")
    except Exception as e:
        logger.error(f"❌ Error creating table: {e}")


def get_known_keys(db) -> set:
    results = db.query(
        CorporateAction.symbol,
        CorporateAction.issue_type,
        CorporateAction.eligibility_date,
    ).all()
    return {(str(r[0]), str(r[1]), r[2]) for r in results}


def save_new_actions(db, new_rows: List[Dict[str, Any]], mark_processed: bool = False):
    for row in new_rows:
        db.add(CorporateAction(
            symbol=row["symbol"],
            company_name=row["company_name"],
            recommendation_announcement_date=row["recommendation_announcement_date"],
            issue_type=row["issue_type"],
            eligibility_date=row["eligibility_date"],
            new_capital=row["new_capital"],
            previous_capital=row["previous_capital"],
            classification=row["classification"],
            processed=mark_processed,
            detected_at=datetime.utcnow(),
        ))
    db.commit()


def trigger_historical_pipeline(symbol: str):
    """Runs Historical_fetcher.py which updates prices and recalculates indicators"""
    logger.info(f"🔄 [Auto-Pipeline] Updating historical prices and indicators for {symbol}...")
    py_exec = sys.executable
    script_path = str(Path(__file__).resolve().parent / "Historical_fetcher.py")
    res = subprocess.run([py_exec, script_path, "--symbol", symbol], capture_output=True, text=True)
    if res.returncode == 0:
        logger.info(f"✅ Successfully updated historical data & indicators for {symbol}!")
    else:
        logger.error(f"❌ Failed to update {symbol}: {res.stderr}")


def trigger_rs_and_ibd_recalc():
    """Recalculates RS ranking and IBD metrics for the market"""
    logger.info("🔄 [Auto-Pipeline] Recalculating RS Rating for all stocks...")
    py_exec = sys.executable
    rs_script = str(Path(__file__).resolve().parent / "recalculate_all_rs.py")
    ibd_script = str(Path(__file__).resolve().parent / "calculate_ibd_metrics.py")
    
    subprocess.run([py_exec, rs_script], check=False)
    subprocess.run([py_exec, ibd_script], check=False)
    logger.info("✅ Finished RS & IBD recalculation.")


def run_watcher(dry_run: bool = False, init_baseline: bool = False):
    logger.info(f"🚀 === Corporate Actions Watcher Started ===")
    ensure_table_exists()
    db = SessionLocal()

    try:
        known_keys = get_known_keys(db)
        is_first_run = len(known_keys) == 0
        logger.info(f"📊 DB currently has {len(known_keys)} recorded corporate action(s).")

        all_rows = fetch_corporate_actions()
        logger.info(f"📥 Parsed {len(all_rows)} action(s) from Saudi Exchange.")

        new_rows = [
            r for r in all_rows
            if (str(r["symbol"]), str(r["issue_type"]), r["eligibility_date"]) not in known_keys
        ]

        if not new_rows:
            logger.info("✅ No new corporate actions detected. System is up to date!")
            return all_rows

        logger.info(f"🚨 Found {len(new_rows)} NEW corporate action(s):")
        for nr in new_rows:
            logger.info(
                f"   ↳ {nr['symbol']} ({nr['company_name']}) | {nr['issue_type']} "
                f"| Date: {nr['eligibility_date']} | Class: {nr['classification']}"
            )

        if dry_run:
            logger.info("ℹ️ Dry-run mode enabled: skipping DB writes and pipelines.")
            return all_rows

        # If it's the very first run or --init-baseline is used, seed the past data as processed
        if is_first_run or init_baseline:
            logger.info("🌱 Initializing baseline corporate actions table (marking past actions as processed)...")
            save_new_actions(db, new_rows, mark_processed=True)
            logger.info(f"✅ Baseline initialized with {len(new_rows)} historical corporate actions.")
            return all_rows

        save_new_actions(db, new_rows, mark_processed=False)

        auto_symbols = sorted({
            r["symbol"] for r in new_rows if r["classification"] == "AUTO_ADJUST"
        })
        review_symbols = sorted({
            r["symbol"] for r in new_rows if r["classification"] == "NEEDS_REVIEW"
        })

        if review_symbols:
            logger.warning(f"⚠️ Flagged for manual review (no price-split needed): {review_symbols}")

        for symbol in auto_symbols:
            trigger_historical_pipeline(symbol)
            # Mark processed in DB
            db.query(CorporateAction).filter(
                CorporateAction.symbol == symbol,
                CorporateAction.eligibility_date.in_(
                    [r["eligibility_date"] for r in new_rows if r["symbol"] == symbol]
                ),
            ).update({"processed": True}, synchronize_session=False)
        db.commit()

        if auto_symbols:
            trigger_rs_and_ibd_recalc()

        logger.info(f"🎉 Watcher completed successfully! Processed {len(auto_symbols)} stocks.")
        return all_rows

    except Exception as e:
        logger.error(f"❌ Watcher encountered an error: {e}", exc_info=True)
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Only check and display without saving/recalculating")
    parser.add_argument("--init-baseline", action="store_true", help="Initialize and record all historical actions without re-triggering pipelines")
    args = parser.parse_args()
    run_watcher(dry_run=args.dry_run, init_baseline=args.init_baseline)
