import sys
from pathlib import Path
from sqlalchemy import text

# Add parent directory to path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal

def check_latest_dates():
    db = SessionLocal()
    try:
        print("🔍 Checking latest dates in the database...\n")
        
        tables = [
            "prices",
            "rs_daily_v2",
            "market_pulse",
            "stock_indicators",
            "market_breadth",
            "industry_group_history"
        ]
        
        for table in tables:
            try:
                result = db.execute(text(f"SELECT MAX(date) FROM {table}")).scalar()
                print(f"✅ {table:<25}: {result}")
            except Exception as e:
                print(f"❌ {table:<25}: Error - {e}")
                
        print("\n🔄 Update Status Table:")
        try:
            status = db.execute(text("SELECT is_updating, latest_ready_date, started_at, completed_at FROM update_status WHERE id = 1")).fetchone()
            if status:
                print(f"   is_updating:       {status[0]}")
                print(f"   latest_ready_date: {status[1]}")
                print(f"   started_at:        {status[2]}")
                print(f"   completed_at:      {status[3]}")
            else:
                print("   No status record found.")
        except Exception as e:
            print(f"   Error checking status: {e}")
            
    finally:
        db.close()

if __name__ == "__main__":
    check_latest_dates()
