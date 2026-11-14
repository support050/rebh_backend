"""
Clear Prices History Cache
Removes cached chart data so fresh indicator data shows up
"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.redis import redis_cache
from app.core.cache_helpers import invalidate_prices_history

async def main():
    print("Clearing prices history cache...")
    connected = await redis_cache.init_redis()
    if not connected:
        print("❌ Could not connect to Redis")
        return
    print("✅ تم الاتصال بـ Redis بنجاح")
    await invalidate_prices_history()
    print("Cache cleared successfully!")

if __name__ == "__main__":
    asyncio.run(main())
