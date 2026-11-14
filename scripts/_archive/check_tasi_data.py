import os
import sys

# Add the project root to sys.path so we can import app modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from app.core.database import SessionLocal

def check_data():
    db = SessionLocal()
    
    print("=== Checking TASI in prices ===")
    try:
        query = text("""
            SELECT symbol, min(date) as first_date, max(date) as last_date, count(*) as total_rows 
            FROM prices 
            WHERE symbol IN ('TASI', '^TASI', 'TASI.SR') 
            GROUP BY symbol
        """)
        res = db.execute(query).fetchall()
        db.commit()
        if not res:
            print("No rows found for TASI in prices")
        for row in res:
            print(f"Symbol: {row[0]}, First Date: {row[1]}, Last Date: {row[2]}, Rows: {row[3]}")
    except Exception as e:
        db.rollback()
        print(f"Error checking prices: {e}")

    print("\n=== Checking market_pulse ===")
    try:
        query = text("""
            SELECT min(date) as first_date, max(date) as last_date, count(*) as total_rows 
            FROM market_pulse
        """)
        res = db.execute(query).fetchone()
        db.commit()
        print(f"First Date: {res[0]}, Last Date: {res[1]}, Rows: {res[2]}")
    except Exception as e:
        db.rollback()
        print(f"Error checking market_pulse: {e}")

    print("\n=== Checking earliest dates for regular stocks in prices ===")
    try:
        query = text("""
            SELECT min(date) as earliest_date 
            FROM prices 
            WHERE symbol NOT IN ('TASI', '^TASI', 'TASI.SR')
        """)
        res = db.execute(query).fetchone()
        db.commit()
        print(f"Earliest stock data date: {res[0]}")
    except Exception as e:
        db.rollback()
        print(f"Error checking earliest stock dates: {e}")

    print("\n=== Checking historical_reports ===")
    try:
        query = text("""
            SELECT min(report_date) as first_date, max(report_date) as last_date, count(*) as total_rows 
            FROM historical_reports
        """)
        res = db.execute(query).fetchone()
        db.commit()
        print(f"First Date: {res[0]}, Last Date: {res[1]}, Rows: {res[2]}")
    except Exception as e:
        db.rollback()
        print(f"Error checking historical_reports: {e}")

if __name__ == "__main__":
    check_data()
