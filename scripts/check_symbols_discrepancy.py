# -*- coding: utf-8 -*-
"""
Quick Discrepancy Checker
=========================
بيفحص الأسهم المطلوبة بمقارنة أسعار الإغلاق التاريخية الموجودة في قاعدة البيانات (DB)
مع الأسعار المعدلة اللحظية من تداول (Tadawul Official Historical Data).
لو لقى اختلاف في السعر القديم، معناه إن السهم حصله تجزئة أو تخفيض/زيادة رأس مال ويحتاج إعادة سحب.
"""

import os
import sys
import time
import json
from pathlib import Path
from datetime import datetime

# Add project root
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from sqlalchemy import text
from scripts.Historical_fetcher import build_driver, capture_api_url_and_request, fetch_page_via_js, HISTORICAL_URL

SYMBOLS_TO_CHECK = ['2001', '1831', '8230', '1810', '4016', '4090', '4083', '1820']

def check_discrepancy(symbols=None):
    if not symbols:
        symbols = SYMBOLS_TO_CHECK

    db = SessionLocal()
    
    # 1. First, check corporate_actions table in DB
    print("=" * 65)
    print("📊 1. Checking 'corporate_actions' DB table for recorded events:")
    print("=" * 65)
    for s in symbols:
        cas = db.execute(
            text("SELECT eligibility_date, issue_type, classification FROM corporate_actions WHERE symbol = :s ORDER BY eligibility_date DESC"),
            {"s": s}
        ).fetchall()
        if cas:
            print(f"👉 Symbol {s}: Found {len(cas)} Corporate Action(s):")
            for ca in cas:
                print(f"    - Date: {ca[0]} | Type: {ca[1]} | Class: {ca[2]}")
        else:
            print(f"⚪ Symbol {s}: No corporate action recorded in corporate_actions table.")

    print("\n" + "=" * 65)
    print("🌐 2. Initializing Chrome to verify live adjusted prices from Tadawul...")
    print("=" * 65)
    
    driver = build_driver(headless=True)
    try:
        driver.get(HISTORICAL_URL)
        api_url, post_body = capture_api_url_and_request(driver, timeout=30)
        if not api_url:
            print("❌ Failed to capture Tadawul Historical API URL.")
            return

        print("✅ Captured Tadawul API URL successfully!\n")

        results = []
        for s in symbols:
            # Pick a date from ~2-3 years ago to ensure pre-split verification
            row = db.execute(
                text("SELECT date, close FROM prices WHERE symbol = :s AND date <= '2023-01-01' ORDER BY date DESC LIMIT 1"),
                {"s": s}
            ).fetchone()
            
            if not row:
                # Might be newly listed after 2023, get oldest row
                row = db.execute(
                    text("SELECT date, close FROM prices WHERE symbol = :s ORDER BY date ASC LIMIT 1"),
                    {"s": s}
                ).fetchone()

            if not row:
                print(f"⚠️ Symbol {s}: No data found in DB!")
                results.append({"symbol": s, "status": "NO_DB_DATA"})
                continue

            ref_date = row[0]
            db_close = float(row[1])
            date_str = ref_date.strftime("%d-%m-%Y")

            # Fetch 1 record for this specific date from Tadawul
            res = fetch_page_via_js(
                driver, api_url, post_body,
                symbol=s, date_from=date_str, date_to=date_str,
                start=0, length=10, market="MAIN"
            )

            if not res or not res.get("success") or not res.get("data", {}).get("data"):
                # Try Nomu if MAIN had no records
                res = fetch_page_via_js(
                    driver, api_url, post_body,
                    symbol=s, date_from=date_str, date_to=date_str,
                    start=0, length=10, market="NOMU"
                )

            data_list = res.get("data", {}).get("data", []) if (res and res.get("success")) else []
            
            if not data_list:
                print(f"❓ Symbol {s}: Could not find date {date_str} on Tadawul.")
                results.append({"symbol": s, "status": "UNKNOWN"})
                continue

            tadawul_close_raw = data_list[0].get("closingPrice") or data_list[0].get("lastTradePrice")
            try:
                tadawul_close = float(str(tadawul_close_raw).replace(",", "").strip())
            except Exception:
                tadawul_close = None

            if tadawul_close is None:
                print(f"❓ Symbol {s}: Invalid price from Tadawul: {tadawul_close_raw}")
                continue

            diff = abs(db_close - tadawul_close)
            is_mismatch = diff > 0.05

            if is_mismatch:
                status_msg = f"❌ MISMATCH! DB={db_close} vs Tadawul={tadawul_close} (Date: {date_str})"
                needs_refetch = True
            else:
                status_msg = f"✅ MATCH! DB={db_close} == Tadawul={tadawul_close} (Date: {date_str})"
                needs_refetch = False

            print(f"Symbol {s:4s} -> {status_msg}")
            results.append({
                "symbol": s,
                "ref_date": str(ref_date),
                "db_close": db_close,
                "tadawul_close": tadawul_close,
                "needs_refetch": needs_refetch
            })

        print("\n" + "=" * 65)
        print("🎯 FINAL SUMMARY & VERDICT:")
        print("=" * 65)
        needs_update = [r["symbol"] for r in results if r.get("needs_refetch")]
        up_to_date = [r["symbol"] for r in results if not r.get("needs_refetch") and r.get("status") not in ("NO_DB_DATA", "UNKNOWN")]

        print(f"✅ المتطابقة والسليمة (لا تحتاج سحب): {up_to_date}")
        print(f"🚨 التي حدث فيها تعديل وتتطلب إعادة سحب: {needs_update}")

        if needs_update:
            print(f"\nللتحديث السريع، شغل الأمر لكل سهم منهم:\n")
            for sym in needs_update:
                print(f"  ..\\venv\\Scripts\\python.exe .\\scripts\\Historical_fetcher.py --symbol {sym}")

    finally:
        driver.quit()
        db.close()

if __name__ == "__main__":
    check_discrepancy()
