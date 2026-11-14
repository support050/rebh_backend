import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.core.database import SessionLocal

def debug_ad_rating():
    db = SessionLocal()
    try:
        # Check total rows in RSDaily
        total_rs = db.execute(text("SELECT COUNT(*) FROM rs_daily_v2")).scalar()
        print(f"Total rows in rs_daily: {total_rs}")
        
        # Check total unique dates
        total_dates = db.execute(text("SELECT COUNT(DISTINCT date) FROM rs_daily_v2")).scalar()
        print(f"Total unique dates in rs_daily: {total_dates}")
        
        # Count A ratings by date for a few sample dates
        print("\nSample counts of 'A' rating by date (Latest 10 dates):")
        result = db.execute(text("""
            SELECT date, COUNT(symbol) as count 
            FROM rs_daily_v2 
            WHERE acc_dis_rating LIKE 'A%' 
            GROUP BY date 
            ORDER BY date DESC 
            LIMIT 10
        """)).fetchall()
        for row in result:
            print(f"  {row.date}: {row.count} stocks")
            
        print("\nSample counts of 'A' rating by date (Oldest 10 dates):")
        result = db.execute(text("""
            SELECT date, COUNT(symbol) as count 
            FROM rs_daily_v2 
            WHERE acc_dis_rating LIKE 'A%' 
            GROUP BY date 
            ORDER BY date ASC 
            LIMIT 10
        """)).fetchall()
        for row in result:
            print(f"  {row.date}: {row.count} stocks")
            
        # Check if RSDaily has missing dates or sparse data
        print("\nTotal stocks per day (Latest 5 dates):")
        result = db.execute(text("""
            SELECT date, COUNT(symbol) as count 
            FROM rs_daily_v2 
            GROUP BY date 
            ORDER BY date DESC 
            LIMIT 5
        """)).fetchall()
        for row in result:
            print(f"  {row.date}: {row.count} total stocks")

    finally:
        db.close()

if __name__ == "__main__":
    debug_ad_rating()
