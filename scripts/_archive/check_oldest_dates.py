import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.core.database import SessionLocal

def check_dates():
    db = SessionLocal()
    try:
        # Check oldest date in prices
        oldest_price = db.execute(text("SELECT MIN(date) FROM prices")).scalar()
        print(f"Oldest date in prices table: {oldest_price}")
        
        # Check oldest date where 5 Months Wide condition could be met
        res = db.execute(text("SELECT MIN(date) FROM stock_indicators WHERE sma_200_5m_ago IS NOT NULL")).scalar()
        print(f"Oldest date with sma_200_5m_ago: {res}")
        
        # Check oldest date with sma_40w (200 days)
        res = db.execute(text("SELECT MIN(date) FROM stock_indicators WHERE sma_40w IS NOT NULL")).scalar()
        print(f"Oldest date with sma_40w: {res}")
        
        # Check oldest date with Alrayan Screener condition met
        res = db.execute(text("SELECT MIN(date) FROM stock_indicators WHERE trend_signal = True")).scalar()
        print(f"Oldest date with Alrayan Screener (trend_signal=True): {res}")
        
    finally:
        db.close()

if __name__ == "__main__":
    check_dates()
