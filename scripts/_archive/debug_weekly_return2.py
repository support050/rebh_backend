"""Check: does our DataFrame have 'open' column? And does Sunday open match the correct reference?"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from app.core.database import SessionLocal
from app.services.weekly_report.data_loader import load_stocks_dataframe
from datetime import date

db = SessionLocal()
df = load_stocks_dataframe(db, date(2026, 7, 16))

print("DataFrame columns:")
print(list(df.columns))
print()

# Check if 'open' exists
if 'open' in df.columns:
    print("'open' column EXISTS!")
    for ticker in ["2222", "1180", "1831", "2010", "7010", "2082"]:
        sdf = df[df["symbol"] == ticker].sort_values("date")
        sun = sdf[sdf["date"].astype(str) == "2026-07-12"]
        thu = sdf[sdf["date"].astype(str) == "2026-07-16"]
        if not sun.empty and not thu.empty:
            o = sun.iloc[0]["open"]
            c = thu.iloc[0]["close"]
            ret = (c - o) / o * 100
            print(f"  {ticker}: Sunday open={o}, Thursday close={c}, return={ret:+.2f}%")
else:
    print("'open' column NOT found")
    print()
    # Alternative: check if using prev_df.iloc[-2] works
    print("Testing: using close from 2 trading days before week start (Wed Jul 8):")
    df["date"] = pd.to_datetime(df["date"])
    ws = pd.to_datetime("2026-07-12")
    we = pd.to_datetime("2026-07-16")
    
    for ticker in ["2222", "1180", "1831", "2010", "7010", "2082"]:
        sdf = df[df["symbol"] == ticker].sort_values("date")
        prev = sdf[sdf["date"] < ws]
        week = sdf[(sdf["date"] >= ws) & (sdf["date"] <= we)]
        if len(prev) >= 2 and not week.empty:
            ref_close = prev.iloc[-2]["close"]
            ref_date = prev.iloc[-2]["date"]
            end_close = week.iloc[-1]["close"]
            ret = (end_close - ref_close) / ref_close * 100
            print(f"  {ticker}: ref={ref_close} ({ref_date.date()}), end={end_close}, return={ret:+.2f}%")

db.close()
