# -*- coding: utf-8 -*-
"""
Historical Data Fetcher — Direct API Approach
==============================================
بيفتح صفحة التقارير التاريخية لتداول، بيمسك رابط الـ API الديناميكي
من أول طلب شبكة، وبعدين بيعمل fetch مباشر من المتصفح بالسهم والتاريخ
المطلوبين — من غير ما يحتاج يتعامل مع أي عناصر في الصفحة.

طريقة الاستخدام:
    python Historical_fetcher.py --symbol 1150 --from 01-05-2024 --to 30-06-2026
    python Historical_fetcher.py --symbol 1150 --show
"""

import os
import json
import time
import base64
import glob
import argparse
import logging
import pandas as pd
from datetime import datetime, date
import sys
from pathlib import Path

# Ensure stdout handles emoji/unicode safely when invoked as subprocess
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Add project root to path so we can import from app
sys.path.append(str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from app.core.database import SessionLocal
from app.models.price import Price

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

HISTORICAL_URL = "https://www.saudiexchange.sa/wps/portal/saudiexchange/newsandreports/reports-publications/historical-reports/"
ACTION_NAME = "populateCompanyDetails"


def build_driver(headless=True):
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

    # Try webdriver_manager first, fall back to cached driver
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        logger.info("✅ Chrome WebDriver initialized (webdriver-manager)")
        return driver
    except Exception as e:
        logger.warning(f"⚠️ webdriver_manager failed ({type(e).__name__}), trying cached driver...")

    # Fallback: find cached chromedriver
    cached = glob.glob(os.path.expanduser("~/.wdm/drivers/chromedriver/**/chromedriver.exe"), recursive=True)
    if cached:
        cached.sort(key=os.path.getmtime, reverse=True)
        logger.info(f"✅ Using cached driver: {cached[0]}")
        service = Service(cached[0])
        driver = webdriver.Chrome(service=service, options=options)
        return driver

    raise RuntimeError("❌ No ChromeDriver found.")


def capture_api_url_and_request(driver, timeout=45):
    """
    بيستنى لحد ما الصفحة تعمل أول طلب populateCompanyDetails
    وبيمسك الـ URL + الـ POST body بتاعه.
    """
    logger.info(f"📡 Waiting up to {timeout}s for initial '{ACTION_NAME}' request...")
    api_url = None
    post_body = None
    start = time.time()

    while time.time() - start < timeout:
        try:
            logs = driver.get_log("performance")
        except Exception:
            time.sleep(1)
            continue

        for entry in logs:
            try:
                message = json.loads(entry["message"])["message"]
                method = message.get("method", "")

                # Capture the request URL and POST body
                if method == "Network.requestWillBeSent":
                    req = message.get("params", {}).get("request", {})
                    url = req.get("url", "")
                    if ACTION_NAME in url and not api_url:
                        api_url = url
                        post_body = req.get("postData", "")
                        logger.info(f"✅ Captured API URL")
                        if post_body:
                            # Parse and log the key params
                            from urllib.parse import parse_qs
                            parsed = parse_qs(post_body)
                            entity_val = parsed.get('selectedEntity', ['?'])[0]
                            market_val = parsed.get('selectedMarket', ['?'])[0]
                            logger.info(f"   📋 Original request: market={market_val}, entity={entity_val}")

            except Exception:
                continue

        if api_url:
            return api_url, post_body

        time.sleep(1)

    return None, None


def fetch_page_via_js(driver, api_url, post_body_template, symbol, date_from, date_to,
                      start=0, length=100, market="MAIN"):
    """
    بيعمل fetch لصفحة واحدة من النتائج (100 صف).
    بيرجع الـ JSON payload كامل.
    """
    js_code = """
    var done = arguments[arguments.length - 1];
    var url = arguments[0];
    var originalBody = arguments[1];
    var symbol = arguments[2];
    var dateFrom = arguments[3] || '';
    var dateTo = arguments[4] || '';
    var startOffset = arguments[5];
    var pageLength = arguments[6];
    var market = arguments[7];

    var params = new URLSearchParams(originalBody);
    params.set('selectedMarket', market);
    params.set('selectedEntity', symbol);
    params.set('start', String(startOffset));
    params.set('length', String(pageLength));
    if (dateFrom) params.set('startDate', dateFrom);
    if (dateTo) params.set('endDate', dateTo);

    fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest'
        },
        body: params.toString()
    })
    .then(function(response) { return response.json(); })
    .then(function(jsonData) { done({success: true, data: jsonData}); })
    .catch(function(err) { done({success: false, error: err.toString()}); });
    """

    driver.set_script_timeout(60)
    result = driver.execute_async_script(
        js_code, api_url, post_body_template or "",
        symbol, date_from, date_to, start, length, market
    )
    return result


def fetch_all_pages(driver, api_url, post_body, symbol, date_from, date_to, market="MAIN"):
    """
    بيجيب كل الصفحات (pagination) لحد ما يخلّص كل الـ recordsTotal.
    """
    PAGE_SIZE = 500
    all_rows = []
    start = 0

    while True:
        result = fetch_page_via_js(
            driver, api_url, post_body, symbol, date_from, date_to,
            start=start, length=PAGE_SIZE, market=market
        )
        if not result or not result.get("success"):
            logger.error(f"❌ Fetch failed at offset {start}")
            break

        payload = result["data"]
        rows = payload.get("data", [])
        records_total = payload.get("recordsTotal", 0)

        if start == 0:
            logger.info(f"   📊 Total records available: {records_total}")

        all_rows.extend(rows)
        logger.info(f"   📥 Fetched {len(rows)} rows (offset {start}→{start+len(rows)}, total so far: {len(all_rows)}/{records_total})")

        if not rows or len(all_rows) >= records_total:
            break

        start += PAGE_SIZE
        time.sleep(0.1)  # Minimal delay to keep it fast while safe

    return all_rows, records_total


import re
def clean_number(text):
    if text is None:
        return None
    text = str(text)
    # Strip HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace(",", "").strip()
    if text in ("", "-", "—"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def main():
    parser = argparse.ArgumentParser(description="Direct Historical Data Fetcher for Tadawul")
    parser.add_argument("--symbol", required=True, help="رمز السهم (مثال: 1150)")
    parser.add_argument("--from", dest="date_from", default=None, help="تاريخ البداية DD-MM-YYYY")
    parser.add_argument("--to", dest="date_to", default=None, help="تاريخ النهاية DD-MM-YYYY")
    parser.add_argument("--show", action="store_true", help="شغّل المتصفح ظاهر")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        # 1. Check existing DB dates
        query = text("SELECT MIN(date) as min_date, MAX(date) as max_date FROM prices WHERE symbol = :symbol")
        res = db.execute(query, {"symbol": args.symbol}).fetchone()
        db_min = res.min_date if res else None
        db_max = res.max_date if res else None
        
        if db_min and db_max:
            logger.info(f"📊 DB Data for {args.symbol}: from {db_min} to {db_max}")
            if not args.date_from:
                args.date_from = db_min.strftime("%d-%m-%Y")
                logger.info(f"   ↳ Auto-setting start date to {args.date_from}")
            if not args.date_to:
                args.date_to = date.today().strftime("%d-%m-%Y")
        else:
            logger.warning(f"⚠️ No data found in DB for {args.symbol}")
            if not args.date_from:
                args.date_from = "01-01-2015"
            if not args.date_to:
                args.date_to = date.today().strftime("%d-%m-%Y")

        driver = build_driver(headless=not args.show)

        logger.info(f"🌍 Navigating to Tadawul Historical Reports...")
        driver.get(HISTORICAL_URL)

        # Wait for the page to fire its initial data request
        api_url, post_body = capture_api_url_and_request(driver, timeout=45)

        if not api_url:
            logger.error("❌ Could not capture the API URL after 45s. The page may have changed.")
            return

        # Fetch all pages for the requested symbol
        all_rows, records_total = fetch_all_pages(
            driver, api_url, post_body, args.symbol, args.date_from, args.date_to
        )

        # If no data with MAIN market, try as index
        if not all_rows:
            logger.info("   🔄 No data in MAIN market, trying INDICES...")
            all_rows, records_total = fetch_all_pages(
                driver, api_url, post_body, f"M:{args.symbol}", args.date_from, args.date_to,
                market="INDICES"
            )

        if not all_rows:
            logger.error("❌ No data returned for this symbol.")
            return

        parsed_rows = []
        for r in all_rows:
            try:
                tx_date_str = r.get("transactionDateStr")
                if not tx_date_str:
                    continue
                tx_date = datetime.strptime(tx_date_str, "%Y-%m-%d").date()
                parsed_rows.append({
                    "symbol": args.symbol,
                    "date": tx_date,
                    "open": clean_number(r.get("todaysOpen")),
                    "high": clean_number(r.get("highPrice")),
                    "low": clean_number(r.get("lowPrice")),
                    "close": clean_number(r.get("previousClosePrice")),
                    "change": clean_number(r.get("change")),
                    "change_percent": clean_number(r.get("changePercent")),
                    "volume_traded": clean_number(r.get("volumeTraded")),
                    "value_traded_sar": clean_number(r.get("turnOver")),
                    "no_of_trades": clean_number(r.get("noOfTrades")),
                })
            except Exception as e:
                logger.warning(f"Skipping row due to error: {e}")

        if not parsed_rows:
            logger.error("❌ No valid rows after parsing.")
            return
            
        # Deduplicate by date
        unique_rows = {}
        for row in parsed_rows:
            if row["date"] not in unique_rows:
                unique_rows[row["date"]] = row
        parsed_rows = list(unique_rows.values())

        df = pd.DataFrame(parsed_rows)
        
        if df.empty:
            logger.error("❌ DataFrame is empty after parsing.")
            return
            
        scraped_min = df["date"].min()
        scraped_max = df["date"].max()
        logger.info(f"📥 Scraped Data: {len(df)} rows, from {scraped_min} to {scraped_max}")

        # Save to Database
        logger.info(f"💾 Updating database table 'prices' for {args.symbol}...")
        chunk_size = 500
        for i in range(0, len(parsed_rows), chunk_size):
            chunk = parsed_rows[i:i+chunk_size]
            stmt = insert(Price).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=['symbol', 'date'],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "change": stmt.excluded.change,
                    "change_percent": stmt.excluded.change_percent,
                    "volume_traded": stmt.excluded.volume_traded,
                    "value_traded_sar": stmt.excluded.value_traded_sar,
                    "no_of_trades": stmt.excluded.no_of_trades,
                }
            )
            db.execute(stmt)
        db.commit()
        logger.info(f"✅ Database prices updated successfully!")

        # Sort by date descending
        try:
            df["_sort"] = pd.to_datetime(df["Date"])
            df = df.sort_values("_sort", ascending=False).drop(columns="_sort")
        except Exception:
            pass

        out_name = f"historical_{args.symbol}_direct.csv"
        df.to_csv(out_name, index=False, encoding="utf-8-sig")
        logger.info(f"✅ Downloaded {len(df)} rows → {out_name}")

    finally:
        if 'driver' in locals():
            driver.quit()
        db.close()


if __name__ == "__main__":
    main()

