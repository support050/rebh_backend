# -*- coding: utf-8 -*-
"""One-off diagnostic: check 6040 historical data adjustment and corporate actions."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import create_engine, text

url = [l.split("=", 1)[1].strip() for l in open(Path(__file__).parent / ".env", encoding="utf-8")
       if l.startswith("DATABASE_URL")][0]
e = create_engine(url)

with e.connect() as c:
    # 1. Check corporate_actions table schema
    print("=" * 70)
    print("CORPORATE_ACTIONS table columns:")
    print("=" * 70)
    rows = c.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name='corporate_actions' ORDER BY ordinal_position"
    )).fetchall()
    for r in rows:
        print(f"  {r[0]}")

    # 2. Corporate actions for 6040
    print("\n" + "=" * 70)
    print("CORPORATE ACTIONS for 6040:")
    print("=" * 70)
    try:
        rows = c.execute(text(
            "SELECT * FROM corporate_actions WHERE symbol='6040'"
        )).fetchall()
        if rows:
            cols = list(rows[0]._mapping.keys())
            print(f"  Columns: {cols}")
            for r in rows:
                print(f"  {tuple(r)}")
        else:
            print("  No records found in corporate_actions for 6040")
    except Exception as ex:
        print(f"  Error: {ex}")
        c.rollback()

    # 3. Historical prices around June 2026 (126 trading days before Nov 5)
    print("\n" + "=" * 70)
    print("Prices June 1-15 2026 (should be ~5x higher if properly split-adjusted):")
    print("=" * 70)
    rows = c.execute(text(
        "SELECT date, close, change_percent FROM prices "
        "WHERE symbol='6040' AND date BETWEEN '2026-06-01' AND '2026-06-15' ORDER BY date"
    )).fetchall()
    for r in rows:
        print(f"  {r[0]} | close={r[1]} | chg%={r[2]}")

    # 4. SMA200 validation
    print("\n" + "=" * 70)
    print("Price ~200 days ago (March 2026) - validates SMA200=11.36 anomaly:")
    print("=" * 70)
    row = c.execute(text(
        "SELECT date, close FROM prices WHERE symbol='6040' AND date <= '2026-03-15' ORDER BY date DESC LIMIT 1"
    )).fetchone()
    if row:
        print(f"  Price on {row[0]}: close={row[1]}")
        print(f"  SMA200 stored on Nov 5: 11.36")
        print(f"  If prices are NOT adjusted for ~1:5 split, pre-split prices would be ~6-12 range")
        print(f"  which makes SMA200=11.36 seem 'reasonable' for unadjusted data")
        print(f"  But post-split, the proper SMA200 should be ~55-60 (multiplied by ~5)")

    # 5. 126-day change validation
    print("\n" + "=" * 70)
    print("126-day change validation:")
    print("=" * 70)
    rows = c.execute(text(
        "SELECT date, close FROM prices WHERE symbol='6040' AND date <= '2026-05-10' ORDER BY date DESC LIMIT 1"
    )).fetchall()
    if rows:
        old_date, old_close = rows[0]
        print(f"  Price on {old_date}: close={old_close}")
        print(f"  Current close (Nov 5): 32.20")
        if old_close:
            pct = (32.20 / float(old_close) - 1) * 100
            print(f"  Computed 126d change from raw data: {pct:.1f}%")
            print(f"  DB stock_indicators reports: 378.45%")
            if abs(pct - 378.45) < 5:
                print("  CONFIRMED: The huge 126d change is from comparing post-split price to pre-split price!")
            else:
                print(f"  Note: slight difference ({pct:.1f}% vs 378.45%)")

    # 6. Adjusted price table check
    print("\n" + "=" * 70)
    print("Looking for adjusted price tables/columns:")
    print("=" * 70)
    try:
        rows = c.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE '%adjust%'"
        )).fetchall()
        for r in rows:
            print(f"  Table: {r[0]}")
        if not rows:
            print("  No adjusted_price tables found")
    except Exception as ex:
        print(f"  Error: {ex}")
        c.rollback()

    try:
        rows = c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='prices' AND column_name LIKE '%adj%'"
        )).fetchall()
        for r in rows:
            print(f"  Prices column: {r[0]}")
        if not rows:
            print("  No adjustment columns in prices table")
    except Exception as ex:
        print(f"  Error: {ex}")

    # 7. Verify what a client would see on the stock page
    print("\n" + "=" * 70)
    print("Full 6040 data picture (Aug-Sep 2026, the merger period):")
    print("=" * 70)
    rows = c.execute(text(
        "SELECT date, close, change_percent, volume_traded FROM prices "
        "WHERE symbol='6040' AND date BETWEEN '2026-09-20' AND '2026-10-05' ORDER BY date"
    )).fetchall()
    for r in rows:
        print(f"  {r[0]} | close={r[1]} | chg={r[2]}% | vol={r[3]}")

    # 8. Check recalculate_history or adjustment scripts
    print("\n" + "=" * 70)
    print("Screener: Is 6040 in Power Play on Nov 4 AND Nov 5?")
    print("=" * 70)
    for target_date in ['2026-11-04', '2026-11-05']:
        row = c.execute(text(f"""
            SELECT percent_change_20d, percent_change_15d, percent_change_126d,
                   price_vs_sma_50_percent, price_vs_sma_200_percent
            FROM stock_indicators
            WHERE symbol='6040' AND date='{target_date}'
        """)).fetchone()
        if row:
            chg20, chg15, chg126, pv50, pv200 = [float(x) if x else 0 for x in row]
            passes = (chg20 > -25 and -15 <= chg15 <= 5 and chg126 > 85 and pv50 > 0 and pv200 > 0)
            print(f"  {target_date}: {'YES - appears in Power Play' if passes else 'NO'}")
            print(f"    chg20d={chg20:.1f} chg15d={chg15:.1f} chg126d={chg126:.1f} pv50={pv50:.1f} pv200={pv200:.1f}")
        else:
            print(f"  {target_date}: NO DATA")
