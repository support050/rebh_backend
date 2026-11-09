"""
Backfill alhussain daily counts into screener_daily_trend_counts.

Safe to re-run — updates alhussain column for all trading days.

Usage:
  cd backend
  ..\\venv\\Scripts\\python.exe scripts\\backfill_alhussain_daily.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal, create_tables
from app.services.screener_daily_trend_service import backfill_alhussain_only, load_alhussain_series


def _ensure_columns() -> None:
    migration_file = os.path.join(os.path.dirname(__file__), "add_screener_trend_columns.py")
    if os.path.exists(migration_file):
        import runpy
        runpy.run_path(migration_file, run_name="__main__")


def main() -> None:
    create_tables()
    _ensure_columns()
    started = time.time()
    print("Backfill alhussain counts (chunked, resumable)...\n", flush=True)

    n = backfill_alhussain_only(6000, chunk_size=60, verbose=True)
    print(f"\nUpdated {n} days in {time.time() - started:.0f}s.", flush=True)

    db = SessionLocal()
    try:
        sample = load_alhussain_series(db, limit=3)
        print(f"Sample: {sample}", flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()
