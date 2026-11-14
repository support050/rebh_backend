import sys, os
from sqlalchemy import create_engine, text
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import settings

def main():
    print("🔍 Inspecting Market Breadth Data Quality...")
    engine = create_engine(str(settings.DATABASE_URL))
    
    with engine.connect() as conn:
        print("\n--- Zero and Null Counts ---")
        for col in ['pct_above_20', 'pct_above_50', 'pct_above_150', 'pct_above_200']:
            zeros = conn.execute(text(f'SELECT COUNT(*) FROM market_breadth WHERE {col} = 0')).scalar()
            nulls = conn.execute(text(f'SELECT COUNT(*) FROM market_breadth WHERE {col} IS NULL')).scalar()
            print(f'{col}: zeros={zeros}, nulls={nulls}')
        
        print("\n--- Date Ranges for Zero vs Non-Zero Data ---")
        for col in ['pct_above_20', 'pct_above_50', 'pct_above_150', 'pct_above_200']:
            first_nonzero = conn.execute(text(f'SELECT MIN(date) FROM market_breadth WHERE {col} > 0')).scalar()
            last_zero = conn.execute(text(f'SELECT MAX(date) FROM market_breadth WHERE {col} = 0')).scalar()
            print(f'{col}:')
            print(f'  First non-zero date: {first_nonzero}')
            print(f'  Last zero date:      {last_zero}')

        print("\n--- Inspecting pct_above_20 specifically ---")
        # Let's see the first few rows of the database overall
        df_head = pd.read_sql(text("SELECT * FROM market_breadth ORDER BY date ASC LIMIT 5"), conn)
        print("\nFirst 5 rows in DB:")
        print(df_head.to_string(index=False))

if __name__ == "__main__":
    main()
