"""
📊 Charts Page Historical Data Coverage Checker
================================================
Checks which stocks have ALL the historical variables
used by the charts page (/stocks/charts).

Data comes from TWO tables:
  1. rs_daily_v2     → RS Ratings, Ranks (1m/3m/6m/9m/12m), Acc/Dis Rating
  2. stock_indicators → RSI, The Number, STAMP, CFG, CCI, Aroon, SMAs, etc.

Usage:
  python scripts/check_charts_coverage.py
  python scripts/check_charts_coverage.py --symbol 2222
  python scripts/check_charts_coverage.py --min-days 100
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import argparse

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

# ── The key columns used by the charts page ──

RS_COLUMNS = [
    'rs_rating', 'rank_1m', 'rank_3m', 'rank_6m', 'rank_9m', 'rank_12m',
    'acc_dis_rating',
    'sector_rs_rating', 'industry_group_rs_rating', 'industry_rs_rating', 'sub_industry_rs_rating',
]

INDICATOR_COLUMNS = [
    # RSI
    'rsi_14', 'sma9_rsi', 'wma45_rsi',
    # The Number
    'sma9_close', 'the_number', 'the_number_hl', 'the_number_ll',
    # STAMP
    'stamp_s9rsi', 'stamp_e45cfg', 'stamp_e45rsi', 'stamp_e20sma3',
    # CFG
    'cfg_daily', 'cfg_sma4', 'cfg_ema45',
    # Price MAs
    'sma4', 'sma9', 'sma18', 'wma45_close', 'ema10', 'ema21',
    # CCI
    'cci', 'cci_ema20',
    # Aroon
    'aroon_up', 'aroon_down',
    # Weekly RSI
    'rsi_w', 'sma9_rsi_w', 'wma45_rsi_w',
    # Weekly The Number
    'sma9_close_w', 'the_number_w', 'the_number_hl_w', 'the_number_ll_w',
    # Weekly STAMP
    'stamp_s9rsi_w', 'stamp_e45cfg_w', 'stamp_e45rsi_w', 'stamp_e20sma3_w',
    # Weekly CFG
    'cfg_w', 'cfg_sma4_w', 'cfg_ema45_w',
    # Weekly Price MAs
    'close_w', 'sma4_w', 'sma9_w', 'sma18_w', 'wma45_close_w',
    # Weekly CCI
    'cci_w', 'cci_ema20_w',
    # Weekly Aroon
    'aroon_up_w', 'aroon_down_w',
    # Market stats
    'sma_10', 'sma_50', 'sma_150', 'sma_200',
    'fifty_two_week_high', 'fifty_two_week_low',
    'percent_off_52w_high', 'percent_off_52w_low',
    'average_volume_50', 'vol_diff_50_percent',
]


def check_coverage(specific_symbol=None, min_days=1):
    engine = create_engine(DATABASE_URL)

    with engine.connect() as conn:
        # ─────────────────────────────────────
        # 1. RS Daily coverage
        # ─────────────────────────────────────
        print("=" * 70)
        print("📊 RS DAILY TABLE (rs_daily_v2) — Coverage")
        print("=" * 70)

        # Count total symbols
        rs_total = conn.execute(text("SELECT COUNT(DISTINCT symbol) FROM rs_daily_v2")).scalar()
        print(f"\nTotal symbols with ANY RS data: {rs_total}")

        # Build a query that checks for non-null values in all RS columns
        rs_non_null_conditions = " AND ".join([f"{col} IS NOT NULL" for col in RS_COLUMNS])
        
        if specific_symbol:
            rs_query = text(f"""
                SELECT symbol, 
                       COUNT(*) as total_days,
                       COUNT(CASE WHEN {rs_non_null_conditions} THEN 1 END) as complete_days,
                       MIN(date) as first_date,
                       MAX(date) as last_date
                FROM rs_daily_v2
                WHERE symbol = :sym
                GROUP BY symbol
            """)
            rs_results = conn.execute(rs_query, {"sym": specific_symbol}).fetchall()
        else:
            rs_query = text(f"""
                SELECT symbol, 
                       COUNT(*) as total_days,
                       COUNT(CASE WHEN {rs_non_null_conditions} THEN 1 END) as complete_days,
                       MIN(date) as first_date,
                       MAX(date) as last_date
                FROM rs_daily_v2
                GROUP BY symbol
                ORDER BY complete_days DESC
            """)
            rs_results = conn.execute(rs_query).fetchall()

        rs_full_coverage = []
        rs_partial = []
        rs_empty = []

        for row in rs_results:
            sym, total, complete, first_d, last_d = row
            if complete >= min_days:
                rs_full_coverage.append((sym, total, complete, first_d, last_d))
            elif complete > 0:
                rs_partial.append((sym, total, complete, first_d, last_d))
            else:
                rs_empty.append((sym, total, 0, first_d, last_d))

        print(f"\n✅ Symbols with COMPLETE RS data (≥{min_days} days): {len(rs_full_coverage)}")
        if rs_full_coverage:
            print(f"   {'Symbol':<10} {'Total Days':>12} {'Complete Days':>14} {'First Date':>14} {'Last Date':>14}")
            print(f"   {'─'*10} {'─'*12} {'─'*14} {'─'*14} {'─'*14}")
            for sym, total, complete, first_d, last_d in rs_full_coverage[:30]:
                print(f"   {sym:<10} {total:>12} {complete:>14} {str(first_d):>14} {str(last_d):>14}")
            if len(rs_full_coverage) > 30:
                print(f"   ... and {len(rs_full_coverage) - 30} more")

        print(f"\n⚠️  Symbols with PARTIAL RS data: {len(rs_partial)}")
        if rs_partial and not specific_symbol:
            for sym, total, complete, first_d, last_d in rs_partial[:10]:
                print(f"   {sym:<10} {total:>6} total, {complete:>6} complete")

        print(f"\n❌ Symbols with NO complete RS rows: {len(rs_empty)}")

        # ─────────────────────────────────────
        # 2. Stock Indicators coverage
        # ─────────────────────────────────────
        print("\n" + "=" * 70)
        print("📈 STOCK INDICATORS TABLE — Coverage")
        print("=" * 70)

        ind_total = conn.execute(text("SELECT COUNT(DISTINCT symbol) FROM stock_indicators")).scalar()
        print(f"\nTotal symbols with ANY indicator data: {ind_total}")

        # Check key columns coverage
        ind_non_null_conditions = " AND ".join([f"{col} IS NOT NULL" for col in INDICATOR_COLUMNS])

        if specific_symbol:
            ind_query = text(f"""
                SELECT symbol,
                       COUNT(*) as total_days,
                       COUNT(CASE WHEN {ind_non_null_conditions} THEN 1 END) as complete_days,
                       MIN(date) as first_date,
                       MAX(date) as last_date
                FROM stock_indicators
                WHERE symbol = :sym
                GROUP BY symbol
            """)
            ind_results = conn.execute(ind_query, {"sym": specific_symbol}).fetchall()
        else:
            ind_query = text(f"""
                SELECT symbol,
                       COUNT(*) as total_days,
                       COUNT(CASE WHEN {ind_non_null_conditions} THEN 1 END) as complete_days,
                       MIN(date) as first_date,
                       MAX(date) as last_date
                FROM stock_indicators
                GROUP BY symbol
                ORDER BY complete_days DESC
            """)
            ind_results = conn.execute(ind_query).fetchall()

        ind_full = []
        ind_partial = []
        ind_empty = []

        for row in ind_results:
            sym, total, complete, first_d, last_d = row
            if complete >= min_days:
                ind_full.append((sym, total, complete, first_d, last_d))
            elif complete > 0:
                ind_partial.append((sym, total, complete, first_d, last_d))
            else:
                ind_empty.append((sym, total, 0, first_d, last_d))

        print(f"\n✅ Symbols with COMPLETE indicator data (≥{min_days} days): {len(ind_full)}")
        if ind_full:
            print(f"   {'Symbol':<10} {'Total Days':>12} {'Complete Days':>14} {'First Date':>14} {'Last Date':>14}")
            print(f"   {'─'*10} {'─'*12} {'─'*14} {'─'*14} {'─'*14}")
            for sym, total, complete, first_d, last_d in ind_full[:30]:
                print(f"   {sym:<10} {total:>12} {complete:>14} {str(first_d):>14} {str(last_d):>14}")
            if len(ind_full) > 30:
                print(f"   ... and {len(ind_full) - 30} more")

        print(f"\n⚠️  Symbols with PARTIAL indicator data: {len(ind_partial)}")
        print(f"❌ Symbols with NO complete indicator rows: {len(ind_empty)}")

        # ─────────────────────────────────────
        # 3. Combined: stocks with BOTH tables complete
        # ─────────────────────────────────────
        print("\n" + "=" * 70)
        print("🎯 COMBINED — Stocks with ALL charts variables historically")
        print("=" * 70)

        rs_set = set(s[0] for s in rs_full_coverage)
        ind_set = set(s[0] for s in ind_full)
        both_complete = rs_set & ind_set
        only_rs = rs_set - ind_set
        only_ind = ind_set - rs_set

        print(f"\n✅ Stocks with BOTH RS + Indicators complete: {len(both_complete)}")
        if both_complete:
            sorted_both = sorted(both_complete)
            for sym in sorted_both[:50]:
                rs_info = next(r for r in rs_full_coverage if r[0] == sym)
                ind_info = next(i for i in ind_full if i[0] == sym)
                print(f"   {sym:<10}  RS: {rs_info[2]:>4} days ({rs_info[3]} → {rs_info[4]})  |  Ind: {ind_info[2]:>4} days ({ind_info[3]} → {ind_info[4]})")
            if len(sorted_both) > 50:
                print(f"   ... and {len(sorted_both) - 50} more")

        print(f"\n🔵 Only RS complete (missing indicators): {len(only_rs)}")
        if only_rs:
            print(f"   {', '.join(sorted(only_rs)[:20])}")

        print(f"\n🟠 Only Indicators complete (missing RS): {len(only_ind)}")
        if only_ind:
            print(f"   {', '.join(sorted(only_ind)[:20])}")

        # ─────────────────────────────────────
        # 4. Per-column NULL analysis (for a specific symbol or overall)
        # ─────────────────────────────────────
        if specific_symbol:
            print("\n" + "=" * 70)
            print(f"🔬 PER-COLUMN NULL ANALYSIS for {specific_symbol}")
            print("=" * 70)

            # RS columns
            print("\n  RS Daily columns:")
            for col in RS_COLUMNS:
                q = text(f"SELECT COUNT(*) as total, COUNT({col}) as filled FROM rs_daily_v2 WHERE symbol = :sym")
                r = conn.execute(q, {"sym": specific_symbol}).fetchone()
                total, filled = r
                pct = (filled / total * 100) if total > 0 else 0
                status = "✅" if pct > 95 else "⚠️" if pct > 0 else "❌"
                print(f"    {status} {col:<30} {filled:>6}/{total:<6} ({pct:5.1f}%)")

            # Indicator columns
            print("\n  Stock Indicator columns:")
            for col in INDICATOR_COLUMNS:
                q = text(f"SELECT COUNT(*) as total, COUNT({col}) as filled FROM stock_indicators WHERE symbol = :sym")
                r = conn.execute(q, {"sym": specific_symbol}).fetchone()
                total, filled = r
                pct = (filled / total * 100) if total > 0 else 0
                status = "✅" if pct > 95 else "⚠️" if pct > 0 else "❌"
                print(f"    {status} {col:<30} {filled:>6}/{total:<6} ({pct:5.1f}%)")

        # ─────────────────────────────────────
        # 5. Final summary
        # ─────────────────────────────────────
        print("\n" + "=" * 70)
        print("📋 FINAL SUMMARY")
        print("=" * 70)

        all_rs_symbols = set(r[0] for r in rs_results)
        all_ind_symbols = set(r[0] for r in ind_results)
        all_symbols = all_rs_symbols | all_ind_symbols

        print(f"""
  Total unique symbols across both tables:  {len(all_symbols)}
  Symbols in rs_daily_v2:                   {len(all_rs_symbols)}
  Symbols in stock_indicators:              {len(all_ind_symbols)}
  
  ✅ Full charts coverage (BOTH tables):    {len(both_complete)}
  🔵 RS only:                               {len(only_rs)}
  🟠 Indicators only:                       {len(only_ind)}
  ❌ Neither complete:                       {len(all_symbols) - len(both_complete) - len(only_rs) - len(only_ind)}
        """)

        if len(both_complete) == len(all_symbols):
            print("  🎉 ALL stocks have full historical coverage for charts page!")
        elif len(both_complete) < 5:
            print("  ⚠️  Very few stocks have complete coverage — likely only test stocks.")
        else:
            print(f"  📊 {len(both_complete)}/{len(all_symbols)} stocks ({len(both_complete)/len(all_symbols)*100:.0f}%) have full charts coverage.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check charts page historical data coverage")
    parser.add_argument('--symbol', type=str, help='Check a specific symbol (e.g. 2222)')
    parser.add_argument('--min-days', type=int, default=1, help='Minimum days to consider "complete" (default: 1)')
    args = parser.parse_args()

    if not DATABASE_URL:
        print("❌ DATABASE_URL not found in .env")
        sys.exit(1)

    check_coverage(specific_symbol=args.symbol, min_days=args.min_days)
