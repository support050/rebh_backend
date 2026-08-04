import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import create_engine, text
from app.core.config import settings

e = create_engine(str(settings.DATABASE_URL))
with e.connect() as c:
    # Check stock_rs_line_metrics columns
    r = c.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='stock_rs_line_metrics' ORDER BY ordinal_position"))
    print("=== stock_rs_line_metrics columns ===")
    for row in r.fetchall():
        print(f"  {row[0]}")
    
    # Check a sample row
    r2 = c.execute(text("SELECT * FROM stock_rs_line_metrics LIMIT 1"))
    cols = r2.keys()
    row = r2.fetchone()
    if row:
        print("\n=== Sample row ===")
        for c_name, val in zip(cols, row):
            print(f"  {c_name}: {val}")
    
    # Check how many have rsnhbp_today = true
    r3 = c.execute(text("SELECT COUNT(*) FROM stock_rs_line_metrics WHERE rsnhbp_today = true AND date = (SELECT MAX(date) FROM stock_rs_line_metrics)"))
    print(f"\nrsnhbp_today=true count (latest date): {r3.scalar()}")
    
    # Check rs_signal_today values
    r4 = c.execute(text("SELECT DISTINCT rs_signal_today FROM stock_rs_line_metrics WHERE date = (SELECT MAX(date) FROM stock_rs_line_metrics)"))
    print(f"Distinct rs_signal_today values: {[x[0] for x in r4.fetchall()]}")
    
    # Check if there's any distribution-related column in stock_indicators or other tables
    r5 = c.execute(text("SELECT column_name FROM information_schema.columns WHERE column_name LIKE '%dist%' OR column_name LIKE '%acc%dis%' ORDER BY table_name"))
    print(f"\nDistribution-related columns: {[(x[0]) for x in r5.fetchall()]}")
    
    # Check acc_dis_rating values
    r6 = c.execute(text("SELECT DISTINCT acc_dis_rating FROM rs_daily_v2 WHERE date = (SELECT MAX(date) FROM rs_daily_v2) ORDER BY acc_dis_rating"))
    print(f"A/D Ratings: {[x[0] for x in r6.fetchall()]}")
