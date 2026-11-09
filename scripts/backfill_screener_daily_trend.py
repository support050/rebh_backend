"""
One-time (or rare) backfill into screener_daily_trend_counts.

Uses small SQL chunks (resumable, no PostgreSQL timeout). Safe to re-run: skips
days already stored.

Usage:
  cd backend
  ..\\venv\\Scripts\\python.exe scripts\\backfill_screener_daily_trend.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal, create_tables
from app.core.redis import redis_cache
from app.services.screener_daily_trend_service import backfill_history, row_count, build_payload
from app.services.minervini_cache import HISTORICAL_TREND_CACHE_KEY, HISTORICAL_TREND_CACHE_TTL


async def _refresh_redis_cache(payload: dict) -> None:
    """Single event loop — avoids 'Event loop is closed' from multiple asyncio.run()."""
    redis_cache.redis_client = None
    redis_cache.is_connected = False
    await redis_cache.init_redis()
    await redis_cache.delete(HISTORICAL_TREND_CACHE_KEY)
    await redis_cache.set(HISTORICAL_TREND_CACHE_KEY, payload, expire=HISTORICAL_TREND_CACHE_TTL)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Backfill screener daily trend counts")
    parser.add_argument("--force", action="store_true", help="Force recalculation of all dates even if they exist")
    args = parser.parse_args()

    create_tables()
    started = time.time()
    print("Backfill (chunked, safe upsert)...\n", flush=True)

    n = backfill_history(6000, chunk_size=60, verbose=True, force=args.force)
    print(f"\nWrote/updated {n} rows in {time.time() - started:.0f}s.", flush=True)

    db = SessionLocal()
    try:
        total = row_count(db)
        print(f"Table total: {total} days", flush=True)
        if total == 0:
            print("No rows — check stock_indicators data.")
            return
        sample = build_payload(db, limit=3)
        print(f"Sample: {sample['series']}", flush=True)
        full = build_payload(db, 6000)
    finally:
        db.close()

    print("Refreshing Redis cache…", flush=True)
    try:
        asyncio.run(_refresh_redis_cache(full))
        print("Redis cache updated.", flush=True)
    except Exception as redis_err:
        print(
            f"Redis refresh skipped ({redis_err}). "
            "Chart still works — first API hit will load from PostgreSQL.",
            flush=True,
        )
    print(f"Done in {time.time() - started:.0f}s. Chart API is ready.", flush=True)


if __name__ == "__main__":
    main()
