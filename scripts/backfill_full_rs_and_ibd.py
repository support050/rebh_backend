"""
backfill_full_rs_and_ibd.py
============================
Combines full historical recalculation for BOTH:
  1. RS Rating       → recalculates rs_rating for all stocks (TRUNCATES rs_daily_v2 first)
  2. IBD Metrics     → calculates acc_dis_rating, sector_rs_rating, industry_group_rs_rating,
                       industry_rs_rating, sub_industry_rs_rating (UPDATE only, does NOT
                       touch rs_rating column)

Run order is critical:
  RS first (TRUNCATE + COPY) → then IBD (COPY temp → UPDATE 5 columns)

Usage:
    python scripts/backfill_full_rs_and_ibd.py

WARNING:
    This will TRUNCATE the rs_daily_v2 table and rebuild it completely.
    Expect 30-90 minutes total runtime depending on data size.
"""

import sys
import time
import logging
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from scripts.calculate_rs_final_precise import RSCalculatorUltraFast
from scripts.calculate_ibd_metrics import _run_backfill
from sqlalchemy import text

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Full RS History
# ═══════════════════════════════════════════════════════════════════════════════

def run_rs_full_history(db_url: str) -> int:
    """
    Calculates RS Rating history for ALL stocks from scratch.
    TRUNCATEs rs_daily_v2 then bulk-inserts via COPY protocol.
    Returns the number of rows saved.
    """
    logger.info("=" * 65)
    logger.info("🧮  STEP 1/2 — Full Historical RS Recalculation")
    logger.info("=" * 65)

    calculator = RSCalculatorUltraFast(db_url)

    # Calculate in memory
    logger.info("⏳ Calculating full RS history in memory (may take 20-40 min)...")
    t0 = time.time()
    df_all = calculator.calculate_full_history_optimized()
    calc_time = time.time() - t0

    if df_all is None or df_all.empty:
        raise RuntimeError("❌ RS calculation returned no data! Aborting.")

    logger.info(f"✅ RS calculation done in {calc_time/60:.1f} min — {len(df_all):,} rows")

    # Truncate + save via COPY
    logger.info("🧹 Truncating rs_daily_v2 ...")
    with calculator.engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE rs_daily_v2"))

    logger.info("💾 Saving RS data via COPY protocol ...")
    t1 = time.time()
    count = calculator.save_with_copy_protocol(df_all)
    save_time = time.time() - t1

    logger.info(f"✅ RS saved: {count:,} rows in {save_time/60:.1f} min")
    return count


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — IBD Metrics Backfill
# ═══════════════════════════════════════════════════════════════════════════════

def run_ibd_backfill():
    """
    Runs the IBD metrics backfill.
    Updates acc_dis_rating, sector_rs_rating, industry_group_rs_rating,
    industry_rs_rating, sub_industry_rs_rating in rs_daily_v2.
    Does NOT touch rs_rating column.
    """
    logger.info("=" * 65)
    logger.info("📊  STEP 2/2 — IBD Metrics Full Historical Backfill")
    logger.info("=" * 65)
    _run_backfill()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print()
    print("=" * 65)
    print("⚠️   FULL RS + IBD HISTORICAL BACKFILL")
    print("=" * 65)
    print()
    print("This script will:")
    print("  1. TRUNCATE rs_daily_v2 and rebuild rs_rating for ALL stocks")
    print("  2. Calculate & update 5 IBD columns (acc_dis, sector_rs, etc.)")
    print()
    print("⚠️  WARNING: rs_daily_v2 will be WIPED and rebuilt from scratch.")
    print("⏳  Waiting 5 seconds before starting... (Ctrl+C to cancel)")
    print()

    try:
        for i in range(5, 0, -1):
            print(f"   Starting in {i}...", end="\r")
            time.sleep(1)
        print()
    except KeyboardInterrupt:
        print("\n🛑 Cancelled by user.")
        sys.exit(0)

    grand_start = time.time()

    # ── Step 1: RS ────────────────────────────────────────────────────────────
    t_rs_start = time.time()
    try:
        rs_rows = run_rs_full_history(str(settings.DATABASE_URL))
        rs_time  = time.time() - t_rs_start
    except Exception as e:
        logger.error(f"❌ RS step failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # ── Step 2: IBD ───────────────────────────────────────────────────────────
    t_ibd_start = time.time()
    try:
        run_ibd_backfill()
        ibd_time = time.time() - t_ibd_start
    except Exception as e:
        logger.error(f"❌ IBD step failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    total_time = time.time() - grand_start

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 65)
    print("🎉  BACKFILL COMPLETE!")
    print("=" * 65)
    print(f"   ✅ RS Rating rows saved : {rs_rows:,}")
    print(f"   ⏱️  RS time             : {rs_time/60:.1f} min")
    print(f"   ⏱️  IBD time            : {ibd_time/60:.1f} min")
    print(f"   🚀 Total time           : {total_time/60:.1f} min")
    print("=" * 65)
    print()
    print("💡 Tip: Run cache bust after this:")
    print("   .\\venv\\Scripts\\python.exe -X utf8 -m app.core.cache_bust --all")
    print()


if __name__ == "__main__":
    main()

