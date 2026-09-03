import sys
import os
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from app.core.database import SessionLocal
from sqlalchemy import text

TARGET_DATE = "2026-09-03"

def purge_date_data(target_date: str = TARGET_DATE):
    print(f"[*] Starting cleanup for market date: {target_date}...")
    db = SessionLocal()
    
    # Candidate tables that store dated market records
    tables = [
        ("prices", "date"),
        ("rs_daily_v2", "date"),
        ("stock_indicators", "date"),
        ("industry_group_daily", "date"),
        ("market_pulse", "date"),
        ("historical_reports", "report_date"),
        ("substantial_shareholders", "report_date"),
        ("net_short_positions", "report_date"),
        ("foreign_headroom", "report_date"),
        ("share_buybacks", "report_date"),
        ("sbl_positions", "report_date"),
    ]
    
    deleted_summary = {}
    
    try:
        for table, col in tables:
            # Check if table exists
            table_check = db.execute(
                text("SELECT to_regclass(:tbl)"),
                {"tbl": f"public.{table}"}
            ).scalar()
            
            if not table_check:
                continue
                
            # Count records for target date
            count_query = text(f"SELECT count(1) FROM {table} WHERE {col} = :d")
            count = db.execute(count_query, {"d": target_date}).scalar() or 0
            
            if count > 0:
                delete_query = text(f"DELETE FROM {table} WHERE {col} = :d")
                db.execute(delete_query, {"d": target_date})
                deleted_summary[table] = count
                print(f"  [-] Deleted {count} records from table '{table}'")
            else:
                print(f"  [i] Table '{table}': 0 records found for {target_date}")
                
        # Also clean update_status latest_ready_date if it was set to target_date
        db.execute(text("""
            UPDATE update_status 
            SET latest_ready_date = '2026-09-02', 
                is_updating = FALSE 
            WHERE latest_ready_date = :d OR is_updating = TRUE
        """), {"d": target_date})
        
        db.commit()
        print(f"\n[OK] Cleanup successfully committed! Total tables affected: {len(deleted_summary)}")
        for t, c in deleted_summary.items():
            print(f"   * {t}: {c} rows deleted")
            
    except Exception as e:
        db.rollback()
        print(f"\n[ERROR] Error during cleanup: {e}")
        raise
    finally:
        db.close()

if __name__ == "__main__":
    purge_date_data(TARGET_DATE)
