# -*- coding: utf-8 -*-
"""Verify which affected stocks were processed and which still have bad data."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import create_engine, text

url = [l.split("=", 1)[1].strip() for l in open(Path(__file__).parent / ".env", encoding="utf-8")
       if l.startswith("DATABASE_URL")][0]
e = create_engine(url)

# All AUTO_ADJUST symbols detected by the watcher (sorted alphabetically):
AFFECTED = ["1030", "1325", "2200", "2360", "2382", "4004", "4270",
            "4321", "4328", "6040", "7202", "8010"]

# The watcher crashed at 2360, so only 1030, 1325, 2200, 2360 were attempted.
# Let's verify each symbol's state.

with e.connect() as c:
    print("=" * 80)
    print("CORPORATE ACTIONS TABLE: Were these events recorded as processed?")
    print("=" * 80)
    for sym in AFFECTED:
        row = c.execute(text(
            "SELECT symbol, issue_type, eligibility_date, processed "
            "FROM corporate_actions WHERE symbol=:s"
        ), {"s": sym}).fetchall()
        if row:
            for r in row:
                status = "PROCESSED" if r[3] else "NOT PROCESSED"
                print(f"  {sym} | {r[1]:<25} | date={r[2]} | {status}")
        else:
            print(f"  {sym} | NOT in corporate_actions table")

    print("\n" + "=" * 80)
    print("PRICE DATA CHECK: Latest 126-day change in stock_indicators")
    print("=" * 80)
    print(f"  {'Symbol':<8} {'Latest Date':<14} {'Close':>8} {'SMA200':>8} {'Chg126d%':>10} {'Verdict'}")
    print("  " + "-" * 70)
    for sym in AFFECTED:
        row = c.execute(text(
            "SELECT date, close, sma_200, percent_change_126d "
            "FROM stock_indicators WHERE symbol=:s ORDER BY date DESC LIMIT 1"
        ), {"s": sym}).fetchone()
        if row:
            chg126 = float(row[3]) if row[3] else 0
            # If change126d is wildly high (> 200%), it's likely unadjusted
            if abs(chg126) > 200:
                verdict = "STILL BROKEN (unadjusted CA)"
            elif abs(chg126) > 150:
                verdict = "SUSPICIOUS"
            else:
                verdict = "OK"
            print(f"  {sym:<8} {str(row[0]):<14} {float(row[1]):>8.2f} {float(row[2] or 0):>8.2f} {chg126:>10.1f} {verdict}")
        else:
            print(f"  {sym:<8} {'NO DATA':<14}")

    print("\n" + "=" * 80)
    print("RAW PRICES CHECK: Pre vs post event range (detects unadjusted jumps)")
    print("=" * 80)
    for sym in AFFECTED:
        # Check if there's a >20% overnight jump in recent months
        rows = c.execute(text(
            "SELECT date, close FROM prices WHERE symbol=:s "
            "AND date >= '2026-08-01' ORDER BY date"
        ), {"s": sym}).fetchall()
        if len(rows) > 2:
            jumps = []
            for i in range(1, len(rows)):
                prev = float(rows[i-1][1])
                curr = float(rows[i][1])
                if prev > 0:
                    pct_change = (curr / prev - 1) * 100
                    if abs(pct_change) > 25:
                        jumps.append((str(rows[i][0]), prev, curr, pct_change))
            if jumps:
                for j in jumps:
                    print(f"  {sym}: {j[0]} | {j[1]:.2f} -> {j[2]:.2f} ({j[3]:+.1f}%) -- UNADJUSTED JUMP")
            else:
                print(f"  {sym}: No >25% jumps found (likely OK or already adjusted)")
        else:
            print(f"  {sym}: Insufficient recent data to check")

    print("\n" + "=" * 80)
    print("SUMMARY: What was processed vs what still needs running")
    print("=" * 80)
    # The sorted order the watcher processes symbols
    sorted_syms = sorted(AFFECTED)
    crashed_at = "2360"  # from log line 52
    idx = sorted_syms.index(crashed_at) if crashed_at in sorted_syms else 0
    print(f"  Watcher sorted order: {sorted_syms}")
    print(f"  Crashed at symbol: {crashed_at} (index {idx})")
    print(f"  Attempted (partial): {sorted_syms[:idx+1]}")
    print(f"  Never reached: {sorted_syms[idx+1:]}")
    print(f"")
    print(f"  Even for attempted symbols: prices were updated BUT indicators crashed")
    print(f"  (UnicodeEncodeError in recalc_full_history.py line 18)")
