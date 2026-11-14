import sys, os
from sqlalchemy import create_engine, inspect, text
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import settings

def main():
    print("🔍 Starting Market Breadth Data Verification...")
    engine = create_engine(str(settings.DATABASE_URL))
    inspector = inspect(engine)
    
    # 1. Check if table exists
    if 'market_breadth' not in inspector.get_table_names():
        print("❌ ERROR: 'market_breadth' table does not exist in the database.")
        return
    print("✅ Table 'market_breadth' exists.")

    # 2. Check schema for correct columns
    columns = [col['name'] for col in inspector.get_columns('market_breadth')]
    print(f"📌 Columns found: {columns}")
    
    if 'pct_above_100' in columns:
        print("❌ ERROR: 'pct_above_100' column STILL EXISTS. It should have been removed.")
    else:
        print("✅ 'pct_above_100' is correctly removed.")
        
    if 'pct_above_150' not in columns:
        print("❌ ERROR: 'pct_above_150' column is MISSING. It should have been added.")
    else:
        print("✅ 'pct_above_150' column exists.")

    if 'pct_above_100' in columns or 'pct_above_150' not in columns:
        print("🚨 Schema verification failed. Stopping data check.")
        return

    # 3. Check data health
    with engine.connect() as conn:
        # Check total rows
        count_res = conn.execute(text("SELECT COUNT(*) FROM market_breadth")).scalar()
        print(f"📊 Total historical records in 'market_breadth': {count_res:,}")
        if count_res == 0:
            print("❌ ERROR: Table is empty. You need to run 'calculate_market_breadth_historical.py'.")
            return
            
        # Check latest date
        latest_date = conn.execute(text("SELECT MAX(date) FROM market_breadth")).scalar()
        print(f"📅 Latest record date: {latest_date}")
        
        # Check for NULLs or zeros in pct_above_150
        null_count = conn.execute(text("SELECT COUNT(*) FROM market_breadth WHERE pct_above_150 IS NULL")).scalar()
        zero_count = conn.execute(text("SELECT COUNT(*) FROM market_breadth WHERE pct_above_150 = 0")).scalar()
        print(f"⚠️ Records with NULL in pct_above_150: {null_count}")
        print(f"⚠️ Records with exactly 0% in pct_above_150: {zero_count}")
        
        # Sample the latest 5 rows
        print("\n🔎 Latest 5 records for verification:")
        df = pd.read_sql(text("SELECT * FROM market_breadth ORDER BY date DESC LIMIT 5"), conn)
        print(df.to_string(index=False))
        
    print("\n✅ Verification complete!")

if __name__ == "__main__":
    main()
