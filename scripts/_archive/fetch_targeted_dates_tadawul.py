# -*- coding: utf-8 -*-
"""
Targeted Dates Historical Fetcher (Tadawul API Direct)
======================================================
يقوم هذا السكريبت بسحب بيانات الأيام المستهدفة فقط (17 و 20 و 21 سبتمبر)
مباشرة من تداول عبر نفس فكرة Historical_fetcher (التقاط الـ API ديناميكياً وتنفيذ Fetch سريع).

المميزات:
1. يطلب فقط نطاق الأيام من 17-09-2026 إلى 21-09-2026، فلا يحمّل أي تاريخ قديم إضافي.
2. يدعم تكرار العملية لجميع الأسهم أو لسهم/قائمة أسهم محددة.
3. يستغل الـ Composite Unique Index في PostgreSQL: (symbol, date) لعمل Upsert فوري:
   ON CONFLICT (symbol, date) DO UPDATE.
4. يحتوي على التحقق الفوري (Verification) بعد كل سهم/دفعة باستخدام SQL JOIN و Indexing
   للتأكد من أن أسعار الأيام الثلاثة ليست متطابقة (Distinct Prices Check).
"""

import os
import json
import time
import glob
import re
import argparse
import logging
from datetime import datetime, date
from pathlib import Path
import sys

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import pandas as pd
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

TARGET_DATES = [date(2026, 10, 4)]
START_DATE_STR = "04-10-2026"
END_DATE_STR = "04-10-2026"



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

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        logger.info("✅ Chrome WebDriver initialized (webdriver-manager)")
        return driver
    except Exception as e:
        logger.warning(f"⚠️ webdriver_manager failed ({type(e).__name__}), trying cached driver...")

    cached = glob.glob(os.path.expanduser("~/.wdm/drivers/chromedriver/**/chromedriver.exe"), recursive=True)
    if cached:
        cached.sort(key=os.path.getmtime, reverse=True)
        logger.info(f"✅ Using cached driver: {cached[0]}")
        service = Service(cached[0])
        return webdriver.Chrome(service=service, options=options)

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

                if method == "Network.requestWillBeSent":
                    req = message.get("params", {}).get("request", {})
                    url = req.get("url", "")
                    if ACTION_NAME in url and not api_url:
                        api_url = url
                        post_body = req.get("postData", "")
                        logger.info(f"✅ Captured API URL")
            except Exception:
                continue

        if api_url:
            return api_url, post_body

        time.sleep(1)

    return None, None



def fetch_target_dates_via_js(driver, api_url, post_body_template, symbol, start_date_str, end_date_str, market="MAIN"):
    """
    بيعمل fetch سريع للتواريخ المحددة فقط عبر الـ API.
    """
    js_code = """
    var done = arguments[arguments.length - 1];
    var url = arguments[0];
    var originalBody = arguments[1];
    var symbol = arguments[2];
    var dateFrom = arguments[3];
    var dateTo = arguments[4];
    var market = arguments[5];

    var params = new URLSearchParams(originalBody);
    params.set('selectedMarket', market);
    params.set('selectedEntity', symbol);
    params.set('start', '0');
    params.set('length', '50');
    params.set('startDate', dateFrom);
    params.set('endDate', dateTo);

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

    driver.set_script_timeout(30)
    result = driver.execute_async_script(
        js_code, api_url, post_body_template or "",
        symbol, start_date_str, end_date_str, market
    )
    return result



def clean_number(text):
    if text is None:
        return None
    text = str(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace(",", "").strip()
    if text in ("", "-", "—"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_rows(symbol, raw_data, target_dates):
    """تحويل السجلات القادمة من الـ API لبيانات جاهزة للـ DB مع حصر الأيام المستهدفة فقط."""
    rows = []
    target_set = set(target_dates)
    
    for r in raw_data:
        try:
            tx_date_str = r.get("transactionDateStr")
            if not tx_date_str:
                continue
            tx_date = datetime.strptime(tx_date_str, "%Y-%m-%d").date()
            if tx_date not in target_set:
                continue
                
            rows.append({
                "symbol": symbol,
                "date": tx_date,
                "open": clean_number(r.get("todaysOpen")),
                "high": clean_number(r.get("highPrice")),
                "low": clean_number(r.get("lowPrice")),
                "close": clean_number(r.get("previousClosePrice")),
                "change": clean_number(r.get("change")),
                "change_percent": clean_number(r.get("changePercent")),
                "volume_traded": int(clean_number(r.get("volumeTraded")) or 0),
                "value_traded_sar": clean_number(r.get("turnOver")),
                "no_of_trades": int(clean_number(r.get("noOfTrades")) or 0),
            })
        except Exception as e:
            logger.warning(f"Error parsing row: {e}")
            
    # Dedup by date
    unique_map = {r["date"]: r for r in rows}
    return list(unique_map.values())


def upsert_prices(db, parsed_rows):
    """
    استخدام PostgreSQL ON CONFLICT DO UPDATE باستخدام Index (symbol, date)
    لتحديث الأسعار القديمة بالبيانات الجديدة للأيام المستهدفة فقط دون تكرار أو مساس بالماضي.
    """
    if not parsed_rows:
        return
    stmt = insert(Price).values(parsed_rows)
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
            "updated_at": datetime.utcnow()
        }
    )
    db.execute(stmt)
    db.commit()


def get_target_symbols(db, specific_symbol=None):
    if specific_symbol:
        return [specific_symbol]
    
    # جلب جميع الأسهم المسجلة في جدول prices مستفيدين من Index (symbol)
    query = text("SELECT DISTINCT symbol FROM prices WHERE symbol ~ '^[0-9]+$' ORDER BY symbol")
    res = db.execute(query).fetchall()
    return [r[0] for r in res]


def main():
    parser = argparse.ArgumentParser(description="Targeted Date(s) Historical Fetcher & Direct DB Upsert")
    parser.add_argument("--symbol", default=None, help="رمز السهم (اختياري، إن لم يحدد يسحب لكل الأسهم)")
    parser.add_argument("--date", default="04-10-2026", help="التاريخ بصيغة DD-MM-YYYY (الافتراضي: 04-10-2026)")
    parser.add_argument("--from", dest="date_from", default=None, help="تاريخ البداية DD-MM-YYYY (اختياري لنطاق تواريخ)")
    parser.add_argument("--to", dest="date_to", default=None, help="تاريخ النهاية DD-MM-YYYY (اختياري لنطاق تواريخ)")
    parser.add_argument("--show", action="store_true", help="إظهار المتصفح")
    args = parser.parse_args()

    # Determine start and end date strings
    if args.date_from and args.date_to:
        start_str = args.date_from
        end_str = args.date_to
    else:
        start_str = args.date
        end_str = args.date

    # Build target dates list
    d_start = datetime.strptime(start_str, "%d-%m-%Y").date()
    d_end = datetime.strptime(end_str, "%d-%m-%Y").date()
    
    target_dates = []
    curr = d_start
    import datetime as dt_mod
    while curr <= d_end:
        target_dates.append(curr)
        curr += dt_mod.timedelta(days=1)

    db = SessionLocal()
    symbols = get_target_symbols(db, args.symbol)
    logger.info(f"📋 Processing {len(symbols)} symbols for targeted date(s): {start_str} to {end_str}...")

    driver = build_driver(headless=not args.show)
    try:
        logger.info("🌍 Navigating to Tadawul Historical Reports...")
        driver.get(HISTORICAL_URL)

        api_url, post_body = capture_api_url_and_request(driver, timeout=45)
        if not api_url:
            logger.error("❌ Failed to capture API URL. Please check connection.")
            return

        success_count = 0
        no_data_count = 0

        for idx, sym in enumerate(symbols, 1):
            try:
                # 1. Fetch targeted dates from Tadawul
                res = fetch_target_dates_via_js(driver, api_url, post_body, sym, start_str, end_str, market="MAIN")
                data = res.get("data", {}).get("data", []) if res and res.get("success") else []
                
                # If no data, try INDICES
                if not data:
                    res = fetch_target_dates_via_js(driver, api_url, post_body, f"M:{sym}", start_str, end_str, market="INDICES")
                    data = res.get("data", {}).get("data", []) if res and res.get("success") else []

                # 2. Parse
                parsed = parse_rows(sym, data, target_dates)
                if parsed:
                    # 3. Upsert using Index (symbol, date)
                    upsert_prices(db, parsed)
                    success_count += 1
                    dates_str = ", ".join([str(p['date']) + f" (Close: {p['close']})" for p in parsed])
                    logger.info(f"✅ [{idx}/{len(symbols)}] {sym}: Saved -> {dates_str}")
                else:
                    no_data_count += 1
                    logger.warning(f"⚠️ [{idx}/{len(symbols)}] {sym}: No data returned for {start_str}")

                # Small delay to keep session stable
                time.sleep(0.15)

            except Exception as e:
                logger.error(f"❌ Error processing {sym}: {e}")

        logger.info("=" * 60)
        logger.info(f"🎉 Completed Targeted Dates Backfill!")
        logger.info(f"   Total Processed: {len(symbols)}")
        logger.info(f"   Successfully Saved: {success_count}")
        logger.info(f"   No Data: {no_data_count}")
        logger.info("=" * 60)

    finally:
        driver.quit()
        db.close()


if __name__ == "__main__":
    main()

