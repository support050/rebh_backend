"""
Cache Utilities & Helpers
Reusable functions for caching patterns, key generation, and invalidation
"""

import json
import logging
import inspect
from typing import Any, Optional, List, Callable, Dict
from functools import wraps
from app.core.redis import redis_cache
from app.core.cache_config import *

logger = logging.getLogger(__name__)

# ============================================================================
# Key Normalization & Generation
# ============================================================================

def normalize_string(val: Optional[str]) -> str:
    """Normalize strings for cache keys: lowercase, strip, handle None"""
    if val is None:
        return "none"
    return str(val).lower().strip()


def normalize_number(val: Optional[int]) -> str:
    """Normalize numbers for cache keys: handle None"""
    if val is None:
        return "none"
    return str(int(val))


def normalize_float(val: Optional[float]) -> str:
    """Normalize floats for cache keys: handle None"""
    if val is None:
        return "none"
    return str(float(val))


def normalize_bool(val: Optional[bool]) -> str:
    """Normalize booleans for cache keys"""
    if val is None:
        return "none"
    return "true" if val else "false"


# ============================================================================
# RS.PY Cache Key Generators
# ============================================================================

def make_rs_latest_key(min_rs: Optional[int], limit: int) -> str:
    """
    Key: rs:latest:min_rs:{min_rs}:limit:{limit}
    """
    min_rs_norm = normalize_number(min_rs)
    return f"{PREFIX_RS}:latest:min_rs:{min_rs_norm}:limit:{limit}"


def make_rs_history_key(symbol: str, from_date: Optional[str], to_date: Optional[str]) -> str:
    """
    Key: rs:history:symbol:{symbol}:from:{from}:to:{to}
    """
    symbol_norm = normalize_string(symbol)
    from_norm = normalize_string(from_date)
    to_norm = normalize_string(to_date)
    return f"{PREFIX_RS}:history:symbol:{symbol_norm}:from:{from_norm}:to:{to_norm}"


def make_rs_advanced_key(
    min_rs: int,
    min_rank_3m: Optional[int],
    min_rank_6m: Optional[int],
    sort_by: str,
    limit: int
) -> str:
    """
    Key: rs:advanced:min_rs:{min_rs}:rank3:{rank3}:rank6:{rank6}:sort:{sort}:limit:{limit}
    """
    rank3_norm = normalize_number(min_rank_3m)
    rank6_norm = normalize_number(min_rank_6m)
    sort_norm = normalize_string(sort_by)
    return f"{PREFIX_RS}:advanced:min_rs:{min_rs}:rank3:{rank3_norm}:rank6:{rank6_norm}:sort:{sort_norm}:limit:{limit}"


# ============================================================================
# RS_V2.PY Cache Key Generators
# ============================================================================

def make_rsv2_latest_key(
    min_rs: Optional[int],
    max_rs: Optional[int],
    industry: Optional[str],
    limit: int,
    offset: int
) -> str:
    """
    Key: rsv2:latest:min:{min}:max:{max}:industry:{industry}:limit:{limit}:offset:{offset}
    """
    min_norm = normalize_number(min_rs)
    max_norm = normalize_number(max_rs)
    ind_norm = normalize_string(industry)
    return f"{PREFIX_RS_V2}:latest:min:{min_norm}:max:{max_norm}:industry:{ind_norm}:limit:{limit}:offset:{offset}"


def make_rsv2_history_key(symbol: str, start_date: Optional[str], end_date: Optional[str], limit: int) -> str:
    """
    Key: rsv2:history:symbol:{symbol}:start:{start}:end:{end}:limit:{limit}
    """
    symbol_norm = normalize_string(symbol)
    start_norm = normalize_string(start_date)
    end_norm = normalize_string(end_date)
    return f"{PREFIX_RS_V2}:history:symbol:{symbol_norm}:start:{start_norm}:end:{end_norm}:limit:{limit}"


def make_rsv2_stats_key() -> str:
    """Key: rsv2:stats (no params)"""
    return f"{PREFIX_RS_V2}:stats"


def make_rsv2_industries_key() -> str:
    """Key: rsv2:industries (no params)"""
    return f"{PREFIX_RS_V2}:industries"


def make_rsv2_topmovers_key(days: int, limit: int) -> str:
    """Key: rsv2:topmovers:days:{days}:limit:{limit}"""
    return f"{PREFIX_RS_V2}:topmovers:days:{days}:limit:{limit}"


# ============================================================================
# SCREENERS.PY Cache Key Generators
# ============================================================================

def make_screener_key(name: str, target_date: Optional[str], limit: int, offset: int) -> str:
    """
    Key: screener:{name}:date:{date}:limit:{limit}:offset:{offset}
    Screener names: trend-1-month, trend-2-months, trend-4-months, trend-5-months, 
                    trend-5-months-wide, power-play
    """
    name_norm = normalize_string(name)
    date_norm = normalize_string(target_date) if target_date else "latest"
    return f"{PREFIX_SCREENER}:{name_norm}:date:{date_norm}:limit:{limit}:offset:{offset}"


def make_screener_historical_key(limit: int) -> str:
    """Key: screener:historical:limit:{limit}"""
    return f"{PREFIX_SCREENER}:historical:limit:{limit}"


# ============================================================================
# TECHNICAL_SCREENER.PY Cache Key Generators
# ============================================================================

def make_technical_screener_key(
    target_date: Optional[str],
    latest_only: bool,
    symbol: Optional[str],
    min_score: Optional[int],
    passing_only: bool,
    limit: int,
    offset: int
) -> str:
    """
    Key: technical:screener:date:{date}:latest_only:{latest}:symbol:{symbol}:min_score:{min}:passing:{passing}:limit:{limit}:offset:{offset}
    """
    date_norm = normalize_string(target_date) if target_date else ("latest" if latest_only else "none")
    symbol_norm = normalize_string(symbol)
    min_norm = normalize_number(min_score)
    passing_norm = normalize_bool(passing_only)
    return f"{PREFIX_TECHNICAL_SCREENER}:screener:date:{date_norm}:latest_only:{normalize_bool(latest_only)}:symbol:{symbol_norm}:min_score:{min_norm}:passing:{passing_norm}:limit:{limit}:offset:{offset}"


# ============================================================================
# PRICES.PY Cache Key Generators
# ============================================================================

def make_prices_latest_key(limit: int) -> str:
    """Key: prices:latest:limit:{limit}"""
    return f"{PREFIX_PRICES}:latest:limit:{limit}"


def make_prices_history_key(symbol: str, limit: int) -> str:
    """Key: prices:history:symbol:{symbol}:limit:{limit}"""
    symbol_norm = normalize_string(symbol)
    return f"{PREFIX_PRICES}:history:symbol:{symbol_norm}:limit:{limit}"


# ============================================================================
# INDUSTRY_GROUPS.PY Cache Key Generators
# ============================================================================

def make_industry_groups_latest_key() -> str:
    """Key: industry:groups:latest (no params)"""
    return f"{PREFIX_INDUSTRY_GROUPS}:latest"


def make_industry_groups_stocks_key(industry_group: str) -> str:
    """Key: industry:groups:stocks:group:{group}"""
    group_norm = normalize_string(industry_group)
    return f"{PREFIX_INDUSTRY_GROUPS}:stocks:group:{group_norm}"


# ============================================================================
# Read-Through Cache Helper
# ============================================================================

async def cache_read_through(
    key: str,
    ttl: int,
    fetch_func: Callable,
    *args,
    **kwargs
) -> Any:
    """
    Read-through cache pattern:
    1. Try Redis GET
    2. If miss: execute fetch_func
    3. If hit: deserialize and return
    4. Set result in cache
    
    Args:
        key: Cache key
        ttl: Time to live in seconds
        fetch_func: Async function to call on cache miss
        *args, **kwargs: Arguments for fetch_func
    
    Returns:
        Cached or fetched data
    """
    try:
        # 1. Try to get from cache
        cached_value = await redis_cache.get(key)
        if cached_value is not None:
            logger.info(
                "cache_hit",
                extra={
                    "key": key,
                    "ttl": ttl
                }
            )
            return cached_value
        
        # 2. Cache miss - fetch from DB / service
        logger.info(
            "cache_miss",
            extra={
                "key": key,
                "ttl": ttl
            }
        )
        if inspect.iscoroutinefunction(fetch_func):
            result = await fetch_func(*args, **kwargs)
        else:
            result = fetch_func(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        
        # 3. Set in cache
        try:
            await redis_cache.set(key, result, expire=ttl)
            logger.info(
                "cache_set",
                extra={
                    "key": key,
                    "ttl": ttl
                }
            )
        except Exception as cache_set_error:
            logger.warning(
                "cache_error",
                extra={
                    "key": key,
                    "operation": "set",
                    "error": str(cache_set_error)
                }
            )
        
        return result
        
    except Exception as cache_error:
        # Redis error - fall back to DB without error
        logger.warning(
            "cache_error",
            extra={
                "key": key,
                "operation": "read",
                "error": str(cache_error)
            }
        )
        # Execute fetch_func without caching
        if inspect.iscoroutinefunction(fetch_func):
            return await fetch_func(*args, **kwargs)
        res = fetch_func(*args, **kwargs)
        if inspect.isawaitable(res):
            return await res
        return res


# ============================================================================
# Cache Invalidation Helpers
# ============================================================================

async def invalidate_patterns(patterns: List[str]) -> None:
    """
    Invalidate all keys matching given patterns using SCAN iteration.
    Safe for production (uses SCAN instead of KEYS *).
    
    Args:
        patterns: List of key patterns to invalidate (supports wildcards)
    
    Example:
        await invalidate_patterns([
            "rs:*",
            "prices:latest:*"
        ])
    """
    for pattern in patterns:
        try:
            keys_to_delete = await redis_cache.scan_iter(pattern)
            if keys_to_delete:
                for key in keys_to_delete:
                    await redis_cache.delete(key)
                logger.info(
                    "cache_invalidate",
                    extra={
                        "pattern": pattern,
                        "keys_deleted": len(keys_to_delete) if isinstance(keys_to_delete, list) else "unknown"
                    }
                )
        except Exception as e:
            logger.warning(
                "cache_error",
                extra={
                    "pattern": pattern,
                    "operation": "invalidate",
                    "error": str(e)
                }
            )


async def invalidate_rs_data() -> None:
    """Invalidate all RS-related cache"""
    await invalidate_patterns([f"{PREFIX_RS}:*"])


async def invalidate_rs_v2_data() -> None:
    """Invalidate all RS V2-related cache"""
    await invalidate_patterns([f"{PREFIX_RS_V2}:*"])


async def invalidate_screener_data() -> None:
    """Invalidate all screener-related cache"""
    await invalidate_patterns([f"{PREFIX_SCREENER}:*"])


async def invalidate_technical_screener_data() -> None:
    """Invalidate all technical screener cache"""
    await invalidate_patterns([f"{PREFIX_TECHNICAL_SCREENER}:*"])


async def invalidate_prices_data() -> None:
    """Invalidate all prices cache"""
    await invalidate_patterns([f"{PREFIX_PRICES}:*"])


async def invalidate_prices_latest() -> None:
    """Invalidate only prices latest cache"""
    await invalidate_patterns([f"{PREFIX_PRICES}:latest:*"])


async def invalidate_prices_history() -> None:
    """Invalidate only prices history cache"""
    await invalidate_patterns([f"{PREFIX_PRICES}:history:*"])


# ============================================================================
# TERMINAL Cache Key Generators & Invalidation
# ============================================================================

def make_terminal_market_machine_key() -> str:
    """Key: terminal:market_machine"""
    return f"{PREFIX_TERMINAL}:market_machine"


def make_terminal_quant_lab_key() -> str:
    """Key: terminal:quant_lab"""
    return f"{PREFIX_TERMINAL}:quant_lab"


def make_terminal_all_ratios_key() -> str:
    """Key: terminal:all_ratios"""
    return f"{PREFIX_TERMINAL}:all_ratios"


def make_terminal_audit_summary_key() -> str:
    """Key: terminal:audit_summary"""
    return f"{PREFIX_TERMINAL}:audit_summary"


def make_terminal_company_fundamental_key(symbol: str) -> str:
    """Key: terminal:company:{symbol}"""
    sym_norm = normalize_string(symbol)
    return f"{PREFIX_TERMINAL}:company:{sym_norm}"


def make_terminal_sector_templates_key() -> str:
    """Key: terminal:sector_templates"""
    return f"{PREFIX_TERMINAL}:sector_templates"


async def invalidate_terminal_daily_cache() -> None:
    """Invalidate daily market-driven terminal caches (Market Machine, Quant Lab, All Ratios)"""
    await invalidate_patterns([
        f"{PREFIX_TERMINAL}:market_machine",
        f"{PREFIX_TERMINAL}:quant_lab",
        f"{PREFIX_TERMINAL}:all_ratios",
    ])


async def invalidate_terminal_company_cache(symbol: Optional[str] = None) -> None:
    """Invalidate company fundamental cache for a specific symbol or all companies"""
    if symbol:
        sym_norm = normalize_string(symbol)
        await invalidate_patterns([f"{PREFIX_TERMINAL}:company:{sym_norm}"])
    else:
        await invalidate_patterns([f"{PREFIX_TERMINAL}:company:*"])


async def invalidate_terminal_all_cache() -> None:
    """Invalidate all terminal caches (called when new XBRL filing is parsed/ingested)"""
    await invalidate_patterns([f"{PREFIX_TERMINAL}:*"])


async def invalidate_all_caches() -> None:
    """
    Invalidate all application caches (use with caution).
    Called after major data updates.
    """
    await invalidate_patterns([
        f"{PREFIX_RS}:*",
        f"{PREFIX_RS_V2}:*",
        f"{PREFIX_SCREENER}:*",
        f"{PREFIX_TECHNICAL_SCREENER}:*",
        f"{PREFIX_PRICES}:*",
        f"{PREFIX_INDUSTRY_GROUPS}:*",
        f"{PREFIX_TERMINAL}:*"
    ])

