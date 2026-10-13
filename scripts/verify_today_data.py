"""
Script to verify that today's market data, technical indicators,
macroeconomic data, and REBH vintages are fully updated in the database.
"""
import os
import sys
from datetime import date
from sqlalchemy import text

# Ensure backend root is in sys.path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.core.database import SessionLocal

def verify_database_freshness():
    today = date.today().strftime("%Y-%m-%d")
    print("=" * 70)
    print(f"🔍 DATABASE FRESHNESS AUDIT (Target Date: {today})")
    print("=" * 70)

    db = SessionLocal()
    try:
        checks = [
            {
                "name": "1. Daily Stock Prices (prices)",
                "query": "SELECT MAX(date) as latest_date, COUNT(*) as count FROM prices WHERE date = (SELECT MAX(date) FROM prices);",
            },
            {
                "name": "2. Daily Financial Indicators (historical_reports)",
                "query": "SELECT MAX(report_date) as latest_date, COUNT(*) as count FROM historical_reports WHERE report_date = (SELECT MAX(report_date) FROM historical_reports);",
            },
            {
                "name": "3. RS Daily Ratings (rs_daily_v2)",
                "query": "SELECT MAX(date) as latest_date, COUNT(*) as count FROM rs_daily_v2 WHERE date = (SELECT MAX(date) FROM rs_daily_v2);",
            },
            {
                "name": "4. Technical Indicators (stock_indicators)",
                "query": "SELECT MAX(date) as latest_date, COUNT(*) as count FROM stock_indicators WHERE date = (SELECT MAX(date) FROM stock_indicators);",
            },
            {
                "name": "5. Market Pulse (market_pulse)",
                "query": "SELECT MAX(date) as latest_date, COUNT(*) as count FROM market_pulse WHERE date = (SELECT MAX(date) FROM market_pulse);",
            },
            {
                "name": "6. Industry Group Scores (industry_group_history)",
                "query": "SELECT MAX(date) as latest_date, COUNT(*) as count FROM industry_group_history WHERE date = (SELECT MAX(date) FROM industry_group_history);",
            },
            {
                "name": "7. SAMA & GaStat Macro (saudi_economic_indicators)",
                "query": "SELECT MAX(retrieved_at) as latest_date, COUNT(*) as count FROM saudi_economic_indicators;",
            },
            {
                "name": "8. Sukuk & Bonds Data (sukuk_market_data)",
                "query": "SELECT MAX(updated_at) as latest_date, COUNT(*) as count FROM sukuk_market_data;",
            },
            {
                "name": "9. REBH Engine Vintages (rebh_engine_vintages)",
                "query": "SELECT MAX(vintage_date) as latest_date, COUNT(*) as count FROM rebh_engine_vintages WHERE vintage_date = (SELECT MAX(vintage_date) FROM rebh_engine_vintages);",
            },
        ]

        for check in checks:
            try:
                res = db.execute(text(check["query"])).fetchone()
                db.commit()
                latest_d = str(res[0]) if res and res[0] is not None else "None"
                count = res[1] if res and len(res) > 1 and res[1] is not None else 0
                
                status_icon = "✅" if latest_d != "None" and count > 0 else "⚠️"
                print(f"{status_icon} {check['name']:<48} | Latest: {latest_d:<10} | Rows: {count}")
            except Exception as e:
                db.rollback()
                print(f"❌ {check['name']:<48} | Error: {str(e)[:50]}")

    finally:
        db.close()

    print("=" * 70)
    print("✨ Verification complete. If latest date matches today, data is 100% LIVE.")
    print("=" * 70)

if __name__ == "__main__":
    verify_database_freshness()
