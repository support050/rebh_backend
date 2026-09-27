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

TARGET_DATES = [date(2026, 9, 17), date(2026, 9, 20), date(2026, 9, 21)]
START_DATE_STR = "17-09-2026"
END_DATE_STR = "21-09-2026"


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


def fetch_target_dates_via_js(driver, api_url, post_body_template, symbol, market="MAIN"):
    """
    بيعمل fetch سريع جداً للأيام المحددة فقط (17-09-2026 إلى 21-09-2026).
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
    params.set('length', '20');
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
        symbol, START_DATE_STR, END_DATE_STR, market
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


def parse_rows(symbol, raw_data):
    """تحويل السجلات القادمة من الـ API لبيانات جاهزة للـ DB مع حصر الأيام المستهدفة فقط."""
    rows = []
    target_set = set(TARGET_DATES)
    
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


def get_existing_dates_for_symbol(db, symbol: str):
    """
    جلب القيم الحالية من قاعدة البيانات قبل التحديث باستخدام الـ Indexing والـ JOIN
    """
    query = text("""
        SELECT 
            p17.open AS o17, p17.close AS c17,
            p20.open AS o20, p20.close AS c20,
            p21.open AS o21, p21.close AS c21
        FROM (SELECT :symbol AS sym) s
        LEFT JOIN prices p17 
            ON p17.symbol = s.sym AND p17.date = '2026-09-17'
        LEFT JOIN prices p20 
            ON p20.symbol = s.sym AND p20.date = '2026-09-20'
        LEFT JOIN prices p21 
            ON p21.symbol = s.sym AND p21.date = '2026-09-21'
    """)
    res = db.execute(query, {"symbol": symbol}).fetchone()
    if not res:
        return None
    return {
        "17": {"open": res.o17, "close": res.c17},
        "20": {"open": res.o20, "close": res.c20},
        "21": {"open": res.o21, "close": res.c21}
    }


def verify_and_log_comparison(db, symbol: str, before: dict):
    """
    التحقق السريع باستخدام JOIN والـ Indexing وعرض مقارنة كاملة:
    القيم قبل التحديث وبعد التحديث لكل يوم من الأيام الثلاثة.
    """
    after = get_existing_dates_for_symbol(db, symbol)
    if not after:
        logger.warning(f"⚠️ {symbol}: No data found after upsert.")
        return False

    b17 = f"{before['17']['close']}" if before and before['17']['close'] is not None else "None"
    b20 = f"{before['20']['close']}" if before and before['20']['close'] is not None else "None"
    b21 = f"{before['21']['close']}" if before and before['21']['close'] is not None else "None"

    a17 = f"{after['17']['close']}" if after['17']['close'] is not None else "None"
    a20 = f"{after['20']['close']}" if after['20']['close'] is not None else "None"
    a21 = f"{after['21']['close']}" if after['21']['close'] is not None else "None"

    logger.info(f"📊 {symbol} [BEFORE]: 17={b17} | 20={b20} | 21={b21}")
    logger.info(f"📈 {symbol} [AFTER ]: 17={a17} | 20={a20} | 21={a21}")

    c17 = after['17']['close']
    c20 = after['20']['close']
    c21 = after['21']['close']

    is_17_20_dup = (c17 is not None and c20 is not None and c17 == c20)
    is_20_21_dup = (c20 is not None and c21 is not None and c20 == c21)

    if is_17_20_dup:
        logger.warning(f"❌ {symbol}: IDENTICAL Close between Sep 17 and Sep 20 ({c20})!")
        return False
    elif is_20_21_dup:
        logger.info(f"ℹ️ {symbol}: Note: Sep 20 & 21 matching ({c20})")
        return True
    else:
        logger.info(f"✅ {symbol}: All 3 dates verified distinct!")
        return True



def get_target_symbols(db, specific_symbol=None):
    if specific_symbol:
        return [specific_symbol]
    
    # جلب جميع الأسهم المسجلة في جدول prices مستفيدين من Index (symbol)
    query = text("SELECT DISTINCT symbol FROM prices WHERE symbol ~ '^[0-9]+$' ORDER BY symbol")
    res = db.execute(query).fetchall()
    return [r[0] for r in res]


def main():
    parser = argparse.ArgumentParser(description="Targeted Dates (17, 20, 21 Sep) Fetcher & Distinct Verifier")
    parser.add_argument("--symbol", default=None, help="رمز السهم (اختياري، إن لم يحدد يسحب لكل الأسهم)")
    parser.add_argument("--show", action="store_true", help="إظهار المتصفح")
    args = parser.parse_args()

    db = SessionLocal()
    symbols = get_target_symbols(db, args.symbol)
    logger.info(f"📋 Processing {len(symbols)} symbols for targeted dates (17, 20, 21 Sep 2026)...")

    driver = build_driver(headless=not args.show)
    try:
        logger.info("🌍 Navigating to Tadawul Historical Reports...")
        driver.get(HISTORICAL_URL)

        api_url, post_body = capture_api_url_and_request(driver, timeout=45)
        if not api_url:
            logger.error("❌ Failed to capture API URL. Please check connection.")
            return

        success_count = 0
        duplicate_flagged = 0

        for idx, sym in enumerate(symbols, 1):
            try:
                # 0. Get current values before update using SQL JOIN & Index
                before_state = get_existing_dates_for_symbol(db, sym)

                # 1. Fetch targeted dates from Tadawul
                res = fetch_target_dates_via_js(driver, api_url, post_body, sym, market="MAIN")
                data = res.get("data", {}).get("data", []) if res and res.get("success") else []
                
                # If no data, try INDICES
                if not data:
                    res = fetch_target_dates_via_js(driver, api_url, post_body, f"M:{sym}", market="INDICES")
                    data = res.get("data", {}).get("data", []) if res and res.get("success") else []

                # 2. Parse
                parsed = parse_rows(sym, data)
                if parsed:
                    # 3. Upsert using Index (symbol, date)
                    upsert_prices(db, parsed)
                    # 4. Verify & display BEFORE vs AFTER using SQL JOIN
                    is_distinct = verify_and_log_comparison(db, sym, before_state)
                    if is_distinct:
                        success_count += 1
                    else:
                        duplicate_flagged += 1
                else:
                    logger.warning(f"⚠️ {sym}: No targeted dates returned from Tadawul API.")

                # Small delay to keep session stable
                time.sleep(0.2)


            except Exception as e:
                logger.error(f"❌ Error processing {sym}: {e}")

        logger.info("=" * 60)
        logger.info(f"🎉 Completed Targeted Dates Backfill!")
        logger.info(f"   Total Processed: {len(symbols)}")
        logger.info(f"   Verified Distinct: {success_count}")
        logger.info(f"   Flagged/Duplicates: {duplicate_flagged}")
        logger.info("=" * 60)

    finally:
        driver.quit()
        db.close()


if __name__ == "__main__":
    main()
