"""
Redis cache layer on top of pre-aggregated DB table `screener_daily_trend_counts`.
No heavy SQL on API requests — run scripts/backfill_screener_daily_trend.py once for history.
"""
import asyncio
import logging

from app.core.database import SessionLocal
from app.core.redis import redis_cache
from app.services.screener_daily_trend_service import build_payload, row_count

logger = logging.getLogger(__name__)

HISTORICAL_TREND_CACHE_KEY = "screener:historical:trend_v5"
HISTORICAL_TREND_CACHE_TTL = 86400


def _load_from_db(limit: int) -> dict:
    db = SessionLocal()
    try:
        if row_count(db) == 0:
            return {"title": "Minervini Trend", "series": [], "total_dates": 0}
        return build_payload(db, limit)
    finally:
        db.close()


async def get_historical_trend_cached(limit: int = 6000) -> dict | None:
    """Fast path: Redis → PostgreSQL aggregate table."""
    cached = await redis_cache.get(HISTORICAL_TREND_CACHE_KEY)
    if cached is not None:
        series = cached.get("series") or []
        if limit < len(series):
            cached = {**cached, "series": series[-limit:], "total_dates": limit}
        return cached

    data = await asyncio.to_thread(_load_from_db, limit)
    if not data.get("series"):
        return None

    full = await asyncio.to_thread(_load_from_db, 6000)
    await redis_cache.set(HISTORICAL_TREND_CACHE_KEY, full, expire=HISTORICAL_TREND_CACHE_TTL)
    if limit < len(data.get("series") or []):
        return data
    return full


async def invalidate_historical_trend_cache() -> None:
    await redis_cache.delete(HISTORICAL_TREND_CACHE_KEY)