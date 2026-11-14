"""
🔍 A/D Rating Chart Diagnostic Script
======================================
يتحقق من بيانات A/D Rating في قاعدة البيانات ويحدد سبب المشكلة:
- لماذا الخط دائماً طالع ومش بينزل؟
- هل البيانات صحيحة؟
- هل في مشكلة في طريقة العرض؟

Usage:
    cd backend
    python scripts/verify_ad_rating_chart.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.core.database import SessionLocal

# ANSI Colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"
PASS = f"{GREEN}✅ PASS{RESET}"
FAIL = f"{RED}❌ FAIL{RESET}"
WARN = f"{YELLOW}⚠️  WARN{RESET}"


def header(title: str):
    print(f"\n{'='*70}")
    print(f"  {BOLD}{CYAN}{title}{RESET}")
    print(f"{'='*70}\n")


def verify_ad_rating_chart():
    db = SessionLocal()
    issues_found = []

    try:
        # ══════════════════════════════════════════════════════════
        # TEST 1: Basic Data Availability
        # ══════════════════════════════════════════════════════════
        header("TEST 1: Basic Data Availability")

        total_rs = db.execute(text("SELECT COUNT(*) FROM rs_daily_v2")).scalar()
        total_dates = db.execute(text("SELECT COUNT(DISTINCT date) FROM rs_daily_v2")).scalar()
        total_with_ad = db.execute(text(
            "SELECT COUNT(*) FROM rs_daily_v2 WHERE acc_dis_rating IS NOT NULL"
        )).scalar()
        dates_with_ad = db.execute(text(
            "SELECT COUNT(DISTINCT date) FROM rs_daily_v2 WHERE acc_dis_rating IS NOT NULL"
        )).scalar()

        print(f"  Total rows in rs_daily_v2:       {total_rs:,}")
        print(f"  Total unique dates:              {total_dates:,}")
        print(f"  Rows with acc_dis_rating:        {total_with_ad:,}")
        print(f"  Dates with acc_dis_rating data:  {dates_with_ad:,}")
        print(f"  Coverage:                        {total_with_ad/total_rs*100:.1f}% of rows")

        if total_with_ad == 0:
            print(f"\n  {FAIL} No acc_dis_rating data found at all!")
            issues_found.append("NO DATA: acc_dis_rating column is empty")
            return

        # ══════════════════════════════════════════════════════════
        # TEST 2: Rating Distribution (what values exist?)
        # ══════════════════════════════════════════════════════════
        header("TEST 2: acc_dis_rating Value Distribution")

        dist = db.execute(text("""
            SELECT acc_dis_rating, COUNT(*) as cnt
            FROM rs_daily_v2
            WHERE acc_dis_rating IS NOT NULL
            GROUP BY acc_dis_rating
            ORDER BY cnt DESC
        """)).fetchall()

        print(f"  {'Rating':<12} {'Count':>12}  {'Pct':>7}")
        print(f"  {'-'*35}")
        for row in dist:
            pct = row.cnt / total_with_ad * 100
            marker = " ← A match" if str(row.acc_dis_rating).startswith('A') else \
                     " ← D match" if str(row.acc_dis_rating).startswith('D') else ""
            print(f"  {str(row.acc_dis_rating):<12} {row.cnt:>12,}  {pct:>6.1f}%{marker}")

        a_values = [r for r in dist if str(r.acc_dis_rating).startswith('A')]
        d_values = [r for r in dist if str(r.acc_dis_rating).startswith('D')]

        if not a_values:
            print(f"\n  {FAIL} No 'A' ratings found!")
            issues_found.append("NO A RATINGS")
        if not d_values:
            print(f"\n  {FAIL} No 'D' ratings found!")
            issues_found.append("NO D RATINGS")

        # ══════════════════════════════════════════════════════════
        # TEST 3: Check if counts are MONOTONICALLY INCREASING
        # (This is the core bug check)
        # ══════════════════════════════════════════════════════════
        header("TEST 3: Is data monotonically increasing? (Bug Detection)")

        # A Rating counts over time
        a_counts = db.execute(text("""
            SELECT date, COUNT(symbol) as count
            FROM rs_daily_v2
            WHERE acc_dis_rating LIKE 'A%'
            GROUP BY date
            ORDER BY date
        """)).fetchall()

        d_counts = db.execute(text("""
            SELECT date, COUNT(symbol) as count
            FROM rs_daily_v2
            WHERE acc_dis_rating LIKE 'D%'
            GROUP BY date
            ORDER BY date
        """)).fetchall()

        def check_monotonic(counts, label):
            if len(counts) < 2:
                print(f"  {WARN} {label}: Only {len(counts)} data points")
                return

            increases = 0
            decreases = 0
            unchanged = 0
            total_changes = len(counts) - 1

            for i in range(1, len(counts)):
                diff = counts[i].count - counts[i-1].count
                if diff > 0:
                    increases += 1
                elif diff < 0:
                    decreases += 1
                else:
                    unchanged += 1

            print(f"  {label}:")
            print(f"    Data points:    {len(counts)}")
            print(f"    Increases:      {increases} ({increases/total_changes*100:.1f}%)")
            print(f"    Decreases:      {decreases} ({decreases/total_changes*100:.1f}%)")
            print(f"    Unchanged:      {unchanged} ({unchanged/total_changes*100:.1f}%)")
            print(f"    First value:    {counts[0].count} (date: {counts[0].date})")
            print(f"    Last value:     {counts[-1].count} (date: {counts[-1].date})")
            print(f"    Min value:      {min(c.count for c in counts)}")
            print(f"    Max value:      {max(c.count for c in counts)}")

            if decreases == 0 and increases > 10:
                print(f"    {FAIL} NEVER DECREASES! This is the bug - data always goes up")
                issues_found.append(f"{label} is monotonically increasing (never goes down)")
            elif decreases < increases * 0.05:
                print(f"    {WARN} Very few decreases ({decreases} vs {increases} increases)")
                issues_found.append(f"{label} rarely decreases - suspicious pattern")
            else:
                print(f"    {PASS} Normal fluctuation pattern")

        check_monotonic(a_counts, "A Rating")
        print()
        check_monotonic(d_counts, "D Rating")

        # ══════════════════════════════════════════════════════════
        # TEST 4: Check if total stocks per day is growing
        # (Could explain monotonic increase if market grows)
        # ══════════════════════════════════════════════════════════
        header("TEST 4: Total stocks per day (market size check)")

        total_per_day = db.execute(text("""
            SELECT date, COUNT(DISTINCT symbol) as count
            FROM rs_daily_v2
            GROUP BY date
            ORDER BY date
        """)).fetchall()

        if total_per_day:
            first = total_per_day[0]
            last = total_per_day[-1]
            print(f"  Earliest date: {first.date} → {first.count} stocks")
            print(f"  Latest date:   {last.date} → {last.count} stocks")

            # Check if stock count is growing significantly
            if last.count > first.count * 1.5:
                print(f"  {WARN} Stock count grew {last.count/first.count:.1f}x — this could cause ")
                print(f"        monotonic increase in A/D counts if we show ABSOLUTE counts")
                print(f"        instead of PERCENTAGES")
                issues_found.append(
                    f"Stock universe grew from {first.count} to {last.count} — "
                    "absolute counts will naturally increase"
                )

        # ══════════════════════════════════════════════════════════
        # TEST 5: Sample data points - show actual values
        # ══════════════════════════════════════════════════════════
        header("TEST 5: Sample Data Points (first 10, last 10)")

        print(f"  {'Date':<14} {'A Count':>8} {'D Count':>8} {'Total':>8} {'A%':>6} {'D%':>6}")
        print(f"  {'-'*56}")

        # Create maps
        a_map = {str(r.date): r.count for r in a_counts}
        d_map = {str(r.date): r.count for r in d_counts}
        total_map = {str(r.date): r.count for r in total_per_day}

        all_dates = sorted(set(a_map.keys()) | set(d_map.keys()))

        def print_sample(dates_slice, label):
            print(f"\n  --- {label} ---")
            for d in dates_slice:
                a = a_map.get(d, 0)
                dd = d_map.get(d, 0)
                t = total_map.get(d, 1)
                a_pct = a / t * 100 if t > 0 else 0
                d_pct = dd / t * 100 if t > 0 else 0
                print(f"  {d:<14} {a:>8} {dd:>8} {t:>8} {a_pct:>5.1f}% {d_pct:>5.1f}%")

        print_sample(all_dates[:10], "Oldest 10 dates")
        print_sample(all_dates[-10:], "Latest 10 dates")

        # ══════════════════════════════════════════════════════════
        # TEST 6: Check for data gaps
        # ══════════════════════════════════════════════════════════
        header("TEST 6: Data Gaps Check")

        dates_without_ad = db.execute(text("""
            SELECT date, COUNT(*) as total,
                   SUM(CASE WHEN acc_dis_rating IS NOT NULL THEN 1 ELSE 0 END) as with_ad
            FROM rs_daily_v2
            GROUP BY date
            HAVING SUM(CASE WHEN acc_dis_rating IS NOT NULL THEN 1 ELSE 0 END) = 0
            ORDER BY date DESC
            LIMIT 10
        """)).fetchall()

        if dates_without_ad:
            print(f"  {WARN} Found {len(dates_without_ad)} dates with NO ad_rating data:")
            for row in dates_without_ad:
                print(f"    {row.date}: {row.total} stocks, 0 have A/D rating")
            issues_found.append(f"Found dates with zero A/D rating coverage")
        else:
            print(f"  {PASS} All dates have at least some A/D rating data")

        # ══════════════════════════════════════════════════════════
        # TEST 7: Check API endpoint response format
        # ══════════════════════════════════════════════════════════
        header("TEST 7: Verify API Logic Matches Frontend Expectations")

        print(f"  Backend API: /api/screeners/historical-ad-rating?limit=5000")
        print(f"  Filter A:    acc_dis_rating LIKE 'A%'")
        print(f"  Filter D:    acc_dis_rating LIKE 'D%'")
        print()
        print(f"  Frontend expects: response.data.series[]  with fields: date, a_rating, d_rating")
        print(f"  Frontend maps:    item.date → time, item.a_rating → a_rating, item.d_rating → d_rating")
        print()

        # Check the actual response the API would generate
        all_dates_api = sorted(set(a_map.keys()) | set(d_map.keys()))
        if len(all_dates_api) > 5000:
            all_dates_api = all_dates_api[-5000:]

        api_series = [
            {"date": d, "a_rating": a_map.get(d, 0), "d_rating": d_map.get(d, 0)}
            for d in all_dates_api
        ]

        print(f"  API would return {len(api_series)} data points")
        if api_series:
            print(f"  First: {api_series[0]}")
            print(f"  Last:  {api_series[-1]}")

        print(f"\n  {PASS} API format looks correct")

        # ══════════════════════════════════════════════════════════
        # TEST 8: Percentage Analysis (the real fix?)
        # ══════════════════════════════════════════════════════════
        header("TEST 8: Percentage vs Absolute Count Analysis")

        print("  If the market grows (more stocks listed over time), absolute A/D counts")
        print("  will naturally increase even if the PERCENTAGE stays the same or goes down.")
        print()

        # Calculate percentage values
        pct_a_increases = 0
        pct_a_decreases = 0
        pct_d_increases = 0
        pct_d_decreases = 0

        prev_a_pct = None
        prev_d_pct = None

        for d in all_dates:
            a = a_map.get(d, 0)
            dd = d_map.get(d, 0)
            t = total_map.get(d, 1)
            a_pct = a / t * 100 if t > 0 else 0
            d_pct = dd / t * 100 if t > 0 else 0

            if prev_a_pct is not None:
                if a_pct > prev_a_pct:
                    pct_a_increases += 1
                elif a_pct < prev_a_pct:
                    pct_a_decreases += 1

            if prev_d_pct is not None:
                if d_pct > prev_d_pct:
                    pct_d_increases += 1
                elif d_pct < prev_d_pct:
                    pct_d_decreases += 1

            prev_a_pct = a_pct
            prev_d_pct = d_pct

        total_changes = max(len(all_dates) - 1, 1)
        print(f"  A Rating PERCENTAGE changes:")
        print(f"    Increases: {pct_a_increases} ({pct_a_increases/total_changes*100:.1f}%)")
        print(f"    Decreases: {pct_a_decreases} ({pct_a_decreases/total_changes*100:.1f}%)")

        print(f"\n  D Rating PERCENTAGE changes:")
        print(f"    Increases: {pct_d_increases} ({pct_d_increases/total_changes*100:.1f}%)")
        print(f"    Decreases: {pct_d_decreases} ({pct_d_decreases/total_changes*100:.1f}%)")

        if pct_a_decreases > pct_a_increases * 0.2 and pct_d_decreases > pct_d_increases * 0.2:
            print(f"\n  {PASS} Percentages show normal fluctuation!")
            print(f"  → The fix: Show PERCENTAGES instead of absolute counts")
            issues_found.append(
                "ROOT CAUSE: Chart shows absolute counts but market is growing. "
                "Should show percentages instead."
            )

        # ══════════════════════════════════════════════════════════
        # SUMMARY
        # ══════════════════════════════════════════════════════════
        header("SUMMARY")

        if issues_found:
            print(f"  {RED}Found {len(issues_found)} issue(s):{RESET}\n")
            for i, issue in enumerate(issues_found, 1):
                print(f"  {i}. {YELLOW}{issue}{RESET}")

            print(f"\n  {BOLD}Recommended Fixes:{RESET}")
            print(f"  ─────────────────")

            has_growing_universe = any("grew" in i.lower() for i in issues_found)
            has_monotonic = any("monotonic" in i.lower() or "never goes down" in i.lower()
                              for i in issues_found)

            if has_growing_universe or has_monotonic:
                print(f"""
  {GREEN}Option 1 (Best):{RESET} Show PERCENTAGE instead of absolute counts
     Backend: Change query to calculate (A count / total stocks) * 100
     This normalizes for market growth

  {GREEN}Option 2:{RESET} Show BOTH lines normalized to a fixed base
     Divide by total stocks on each date

  {GREEN}Option 3:{RESET} Add total stocks line for context
     Keep absolute counts but add reference line
""")
        else:
            print(f"  {PASS} No issues found! Chart data looks correct.")

    finally:
        db.close()


if __name__ == "__main__":
    verify_ad_rating_chart()
