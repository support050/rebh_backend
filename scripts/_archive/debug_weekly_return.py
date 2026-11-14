"""
Debug: compare our weekly return calculation vs Aporia's correct values.
Correct values (from Aporia website):
  2222 (Aramco):  -0.6
  1180 (Al Rajhi): -1.5  (user said -2.2 earlier, now -1.5)
  1831 (Maharah): +6.99
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from app.core.database import SessionLocal
from app.services.weekly_report.data_loader import load_stocks_dataframe
from sqlalchemy import text

db = SessionLocal()

# The report is for week ending Jul 16, 2026 (week_start=Jul 12, week_end=Jul 16)
ws = "2026-07-12"
we = "2026-07-16"

# Load stock data
from datetime import date
df = load_stocks_dataframe(db, date(2026, 7, 16))
df["date"] = pd.to_datetime(df["date"])

test_tickers = ["2222", "1180", "1831", "2010", "7010", "2082", "4030"]

for ticker in test_tickers:
    sdf = df[df["symbol"] == ticker].sort_values("date")
    if sdf.empty:
        print(f"{ticker}: NO DATA")
        continue
    
    ws_dt = pd.to_datetime(ws)
    we_dt = pd.to_datetime(we)
    
    # Last close BEFORE week start
    prev = sdf[sdf["date"] < ws_dt]
    week = sdf[(sdf["date"] >= ws_dt) & (sdf["date"] <= we_dt)]
    
    if prev.empty or week.empty:
        print(f"{ticker}: Missing prev or week data")
        continue
    
    prev_close = prev.iloc[-1]["close"]
    prev_date = prev.iloc[-1]["date"]
    week_end_close = week.iloc[-1]["close"]
    week_end_date = week.iloc[-1]["date"]
    week_start_close = week.iloc[0]["close"]
    week_start_date = week.iloc[0]["date"]
    
    # Method 1: (last_of_week - last_before_week) / last_before_week
    ret1 = (week_end_close - prev_close) / prev_close * 100
    
    # Method 2: (last_of_week - first_of_week) / first_of_week
    ret2 = (week_end_close - week_start_close) / week_start_close * 100
    
    # Method 3: simple close-to-close using last 2 trading days before week end
    ret3 = "N/A"
    
    name = sdf.iloc[-1].get("stock_name", ticker)
    print(f"\n{ticker} ({name}):")
    print(f"  prev_close = {prev_close:.2f} on {prev_date.date()}")
    print(f"  week_start_close = {week_start_close:.2f} on {week_start_date.date()}")
    print(f"  week_end_close = {week_end_close:.2f} on {week_end_date.date()}")
    print(f"  Method1 (prev→end): {ret1:+.2f}%")
    print(f"  Method2 (start→end): {ret2:+.2f}%")
    
    # Show all week's closes
    print(f"  Week closes:")
    for _, r in week.iterrows():
        print(f"    {r['date'].date()}: {r['close']:.2f}")
    
    # Also show last 3 prev closes
    print(f"  Last 3 pre-week closes:")
    for _, r in prev.tail(3).iterrows():
        print(f"    {r['date'].date()}: {r['close']:.2f}")

db.close()
