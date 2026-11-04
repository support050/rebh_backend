"""
Cache Utilities & Helpers
Reusable functions for caching patterns, key generation, and invalidation
"""

import asyncio
import inspect
import json
import logging
import random
import re
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from app.core.redis import namespace_of, redis_cache
from app.core.cache_config import *  # noqa: F401,F403 — PREFIX_*/CACHE_TTL_* are the API

logger = logging.getLogger(__name__)

# Sentinel returned by the poll helper when no peer ever produced a value.
_NO_VALUE = object()

# Background revalidations must keep a strong reference or the event loop is
# free to garbage-collect the task mid-flight (asyncio docs, "create_task").
_BG_TASKS: set = set()

_KEY_ALLOWED_RE = re.compile(CACHE_KEY_ALLOWED_PATTERN)  # noqa: F401 — from cache_config

# ============================================================================
# Key Normalization & Generation
# ============================================================================

def cache_key_problem(key: Any) -> Optional[str]:
    """Why this key must not be written, or None when it is clean.

    Guards the two ways a user-controlled parameter hurts the cache:
      * length — an unbounded parameter becomes an unbounded key (memory leak
        that no invalidation pattern can name);
      * glob/control characters — ``*``, ``?``, ``[``, ``]`` inside a stem
        poison every ``namespace:*`` sweep built on top of it, and a newline
        desynchronises the RESP protocol on some proxies.
    Refusing to CACHE such a key never fails the request: the caller simply
    serves from the database (fail-open, exactly like a dead cache).
    """
    k = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
    if not k:
        return "empty"
    if len(k) > CACHE_KEY_MAX_LENGTH:
        return "too_long"
    if any(ch in k for ch in CACHE_KEY_GLOB_CHARS):
        return "glob_character"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in k):
        return "control_character"
    if not _KEY_ALLOWED_RE.fullmatch(k):
        return "unsupported_character"
    if k.startswith(tuple(AUTH_KEY_PREFIXES)):
        # An auth stem is not a cache; refusing it here is the same rule the
        # invalidation helpers enforce, applied at write time.
        return "auth_keyspace"
    return None


def apply_ttl_jitter(ttl: int) -> int:
    """Spread mass expiry so a namespace cannot die in one second.

    Only TTLs of at least TTL_JITTER_THRESHOLD_SECONDS move, and only UPWARD
    by at most TTL_JITTER_RATIO (10 %). No configured TTL is ever shortened —
    the strict "do not change existing TTLs" rule holds; the floor stays.
    """
    base = int(ttl)
    if base < TTL_JITTER_THRESHOLD_SECONDS:
        return base
    spread = int(base * TTL_JITTER_RATIO)
    if spread <= 0:
        return base
    return base + random.randint(0, spread)


def lock_key_for(key: str) -> str:
    """The single-flight lock that guards ``key`` (namespaced so a sweep of the
    data namespace never has to look at locks)."""
    return f"{LOCK_KEY_PREFIX}{key}"


def _pattern_is_scoped(pattern: Any) -> bool:
    """True when a glob can only ever match inside one named namespace.

    A sweep helper is defined by what it refuses: ``refresh_namespace`` deletes,
    so a pattern of ``"*"`` (or one aimed at an auth stem) must be rejected even
    though ``invalidate_patterns`` would filter key by key — refusing the pattern
    is what keeps the refusal independent of an auth list drifting later.
    """
    p = str(pattern or "").strip()
    if not p or p == "*":
        return False
    stem = re.split(r"[*?\[\]{}]", p, 1)[0].strip(":")
    return len(stem) >= 2 and not _is_auth_key(stem) and not _is_auth_key(p)


def _is_empty_result(value: Any) -> bool:
    """None / [] / {} — the three shapes that mean "nothing to show"."""
    return value is None or (isinstance(value, (list, tuple, dict)) and len(value) == 0)


def _wrap_empty(value: Any) -> Dict[str, Any]:
    """Mark a legitimate empty answer so a HIT can tell it from a MISS.

    Redis stores a string, and ``RedisCache.get`` reports "miss" for anything it
    reads back as ``None`` — so a cached ``None`` was indistinguishable from "key
    absent" and got re-fetched on every single request. That is the penetration
    hole this closes. ``[]`` and ``{}`` did used to be cached and served (on the
    full TTL); the sentinel additionally shortens their expiry to
    NEGATIVE_CACHE_TTL, so a symbol whose data arrives later stops being blank
    for days. The original shape is preserved ([] stays []), so no caller notices.
    """
    return {EMPTY_SENTINEL_KEY: value if value is not None else None}


def _unwrap_empty(value: Any) -> Any:
    if isinstance(value, dict) and len(value) == 1 and EMPTY_SENTINEL_KEY in value:
        return value[EMPTY_SENTINEL_KEY]
    return value


def _is_empty_sentinel(value: Any) -> bool:
    return isinstance(value, dict) and len(value) == 1 and EMPTY_SENTINEL_KEY in value


def _is_auth_key(key: Any) -> bool:
    """True for keys that are NOT a cache (sessions, JTIs, OAuth state).

    Same list the rest of the system uses (cache_config.AUTH_KEY_PREFIXES):
    an invalidation helper that deletes these logs every user out or resets a
    login lockout, so every sweep here refuses them first.
    """
    k = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
    return any(k.startswith(prefix) for prefix in AUTH_KEY_PREFIXES)


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


def make_prices_seasonality_key(symbol: str, years: int) -> str:
    """Key: prices:seasonality:symbol:{symbol}:years:{years}"""
    symbol_norm = normalize_string(symbol)
    return f"{PREFIX_PRICES}:seasonality:symbol:{symbol_norm}:years:{years}"


def make_prices_tasi_history_key(days: int) -> str:
    """Key: prices:tasi:history:days:{days} — TASI index series from market_pulse."""
    return f"{PREFIX_PRICES}:tasi:history:days:{days}"


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

# ============================================================================
# Read-Through Cache Helper (single-flight, jittered TTL, negative caching)
# ============================================================================

async def _invoke(fetch_func: Callable, args: tuple, kwargs: dict) -> Any:
    """Run a fetch_func that may be sync, async or return an awaitable."""
    if inspect.iscoroutinefunction(fetch_func):
        return await fetch_func(*args, **kwargs)
    result = fetch_func(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _peer_is_cooking(key: str) -> bool:
    """Is somebody actually holding this key's lock?

    The distinction matters more than it looks. ``set_nx`` answers "no" for two
    completely different reasons:

      * a peer holds the lock → wait for its value (the stampede fix); and
      * Redis is unreachable → there is nothing to wait FOR.

    Treating the second as the first would make every request on a dead cache
    burn LOCK_POLL_MAX_SECONDS before doing its own work — the exact per-request
    5 s stall that Part 1's circuit breaker exists to prevent. Fail-soft like
    every other data-path probe: "unknown" means "go and compute".
    """
    try:
        return bool(await redis_cache.exists(lock_key_for(key)))
    except Exception:                                 # noqa: BLE001 - stay fail-open
        return False


async def _await_peer_value(key: str) -> Any:
    """Short-poll for the lock winner's value. _NO_VALUE when nobody produced one.

    A waiter must never fail the request: after LOCK_POLL_MAX_SECONDS it gives up
    and cooks the value itself. That is a duplicate query in the worst case —
    which is precisely the old behaviour — and still one fewer than the stampede
    it replaced.
    """
    deadline = time.monotonic() + LOCK_POLL_MAX_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(LOCK_POLL_INTERVAL_SECONDS)
        try:
            value = await redis_cache.get(key)
        except Exception:                            # noqa: BLE001 - stay fail-open
            return _NO_VALUE
        if value is not None:
            return value
    return _NO_VALUE


def _spawn_background_revalidate(key: str, ttl: int, stale_ttl: Optional[int],
                                 fetch_func: Callable, args: tuple, kwargs: dict,
                                 cache_empty: bool = False) -> None:
    """Serve the stale payload now, refresh it in flight (never blocks the reply)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:                              # pragma: no cover - defensive
        return
    task = loop.create_task(
        _revalidate_into_cache(key, ttl, stale_ttl, fetch_func, args, kwargs, cache_empty)
    )
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def _revalidate_into_cache(key: str, ttl: int, stale_ttl: Optional[int],
                                 fetch_func: Callable, args: tuple, kwargs: dict,
                                 cache_empty: bool = False) -> bool:
    """Rebuild one stale key for a reader that already got an answer.

    Takes the same single-flight lock, so the fifty readers that all saw the
    stale value during the same window still produce one query. Failures are
    logged and swallowed: the reader already has a (stale but real) payload.
    """
    token = uuid.uuid4().hex
    if not await redis_cache.set_nx(lock_key_for(key), token, LOCK_TTL_SECONDS):
        return False
    try:
        result = await _invoke(fetch_func, args, kwargs)
        if cache_empty and _is_empty_result(result):
            await redis_cache.set(key, _wrap_empty(result), expire=NEGATIVE_CACHE_TTL)
        else:
            await redis_cache.set(key, result, expire=_store_ttl(ttl, stale_ttl))
        return True
    except Exception as e:                            # noqa: BLE001 - stale is served
        logger.warning(
            "cache_stale_revalidate_failed",
            extra={"namespace": namespace_of(key), "error": str(e)[:200]}
        )
        return False
    finally:
        await redis_cache.release_lock(lock_key_for(key), token)


def _store_ttl(ttl: int, stale_ttl: Optional[int]) -> int:
    """Jittered freshness, plus the stale-serving window when requested.

    The fresh boundary is ``hard_ttl - stale_ttl`` and jitter only ever makes
    ``hard_ttl`` bigger, so a value inside the fresh region can never be judged
    stale by the remaining-TTL test in cache_read_through.
    """
    return apply_ttl_jitter(ttl + int(stale_ttl or 0))


async def cache_read_through(
    key: str,
    ttl: int,
    fetch_func: Callable,
    *args,
    stale_ttl: Optional[int] = None,
    cache_empty: bool = False,
    **kwargs
) -> Any:
    """
    Read-through cache pattern:
    1. Try Redis GET
    2. If miss: take the single-flight lock and execute fetch_func
    3. If hit: deserialize and return
    4. Set result in cache, release the lock

    Args:
        key: Cache key
        ttl: Time to live in seconds (a floor — see apply_ttl_jitter)
        fetch_func: Async function to call on cache miss
        *args, **kwargs: Arguments for fetch_func
        stale_ttl: Serve a value this far past ``ttl`` while refreshing it in
            the background. None (default) = the pre-Part-2 behaviour.
        cache_empty: Give an empty answer (None/[]/{}) a NEGATIVE_CACHE_TTL
            sentinel under the same key, so "there is nothing here" becomes a
            cacheable answer instead of one full query every time. None was
            previously never cacheable at all; [] and {} were cached for the
            full ttl. False (default) keeps both of those behaviours untouched.

    Returns:
        Cached or fetched data

    TTL discipline (Phase 7): a key stored with no expiry never leaves Redis —
    it survives every bust, every deploy, every rename. A missing/zero ttl
    therefore falls back to the named DEFAULT_CACHE_TTL and says so; no
    configured TTL is ever changed by this.

    Error discipline (2026-10-12, after the Render PG-restart 500s):
    ONLY Redis operations are wrapped in try/except. A fetch_func failure
    propagates as-is. The previous shape wrapped the DB fetch in the same
    catch-all as the Redis read and then RE-INVOKED fetch_func on the same
    session — after a mid-query connection drop that second execute always
    raised PendingRollbackError (invalid transaction), turning a transient
    PG restart into a 500 instead of one clean, retryable error.

    Stampede discipline (Part 2 / Phase 3): one expired key used to unlock N
    identical heavy queries. Now exactly one caller computes; the rest wait for
    its value for up to LOCK_POLL_MAX_SECONDS and only then compute for
    themselves. A dead Redis skips the lock entirely (fail-open, unchanged).
    """
    # 0. Key hygiene: an oversized / glob-bearing / control-character key is
    #    never written (it would be uncacheable junk), but the request still
    #    runs. Fail-open, like every other cache problem here.
    problem = cache_key_problem(key)
    if problem:
        logger.warning(
            "cache_key_rejected",
            extra={"namespace": namespace_of(key), "reason": problem,
                   "length": len(str(key))}
        )
        return await _invoke(fetch_func, args, kwargs)

    # 1. Try to get from cache — a broken cache must not mask or redo a DB read.
    ttl = redis_cache.resolve_ttl(key, ttl)
    cache_available = True
    try:
        cached_value = await redis_cache.get(key)
    except Exception as cache_error:
        cache_available = False
        cached_value = None
        logger.warning(
            "cache_error",
            extra={
                "key": key,
                "operation": "read",
                "error": str(cache_error)
            }
        )

    if cached_value is not None:
        if _is_empty_sentinel(cached_value):
            # A negatively-cached empty answer is still an answer: no DB read.
            logger.info("cache_hit_negative", extra={"namespace": namespace_of(key)})
            return _unwrap_empty(cached_value)
        if stale_ttl is not None:
            # Staleness is derived from the remaining TTL (no envelope, so the
            # stored payload format is untouched): once what is left falls
            # inside the stale window, the value is past its freshness. The
            # jitter applied at write time only widens the fresh region.
            remaining = await redis_cache.ttl(key)
            if remaining is not None and 0 < remaining <= int(stale_ttl):
                logger.info("cache_stale_served", extra={"namespace": namespace_of(key)})
                _spawn_background_revalidate(key, ttl, stale_ttl, fetch_func, args,
                                             kwargs, bool(cache_empty))
        logger.info(
            "cache_hit",
            extra={
                "key": key,
                "ttl": ttl
            }
        )
        return cached_value

    # 2. Cache miss — single-flight, then fetch from DB / service. NO try/except
    #    here on purpose: the caller's get_db dependency rolls the session back
    #    when the exception propagates, so the next request starts clean.
    logger.info(
        "cache_miss",
        extra={
            "key": key,
            "ttl": ttl
        }
    )
    lock_token: Optional[str] = None
    if cache_available:
        lock_token = uuid.uuid4().hex
        if not await redis_cache.set_nx(lock_key_for(key), lock_token, LOCK_TTL_SECONDS):
            # Somebody else may be cooking this exact key — but only if the lock
            # is really there (see _peer_is_cooking: a dead Redis also says no).
            lock_token = None
            if await _peer_is_cooking(key):
                peer_value = await _await_peer_value(key)
                if peer_value is not _NO_VALUE:
                    logger.info(
                        "cache_stampede_avoided",
                        extra={"namespace": namespace_of(key)}
                    )
                    return _unwrap_empty(peer_value)
                # Timed out (or the winner died): do the work rather than fail.
                logger.warning(
                    "cache_lock_wait_timeout",
                    extra={"namespace": namespace_of(key),
                           "waited_seconds": LOCK_POLL_MAX_SECONDS}
                )

    try:
        result = await _invoke(fetch_func, args, kwargs)

        # 3. Set in cache — a set failure never invalidates a good fetch.
        if cache_available:
            try:
                if cache_empty and _is_empty_result(result):
                    store_ttl: Optional[int] = NEGATIVE_CACHE_TTL
                    payload: Any = _wrap_empty(result)
                    logger.info(
                        "cache_set_negative",
                        extra={"namespace": namespace_of(key), "ttl": store_ttl}
                    )
                else:
                    store_ttl = _store_ttl(ttl, stale_ttl)
                    payload = result
                await redis_cache.set(key, payload, expire=store_ttl)
                logger.info(
                    "cache_set",
                    extra={
                        "key": key,
                        "ttl": store_ttl
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
    finally:
        # Released only after the value is written, so a waiter can never see
        # "lock free, value absent" and stampede alongside the next arrival.
        if lock_token:
            await redis_cache.release_lock(lock_key_for(key), lock_token)

    return result


# ============================================================================
# Cache Invalidation Helpers
# ============================================================================

def _chunks(items: List[Any], size: int) -> Iterable[List[Any]]:
    """Batches of at most ``size`` — never one command per key, never one
    command for a hundred thousand keys."""
    step = max(1, int(size))
    for start in range(0, len(items), step):
        yield items[start:start + step]


async def _batched_unlink(keys: List[str], *, batch_size: int = INVALIDATE_BATCH_SIZE,
                          pause: float = INVALIDATE_BATCH_PAUSE_SECONDS,
                          client: Optional[Any] = None) -> int:
    """UNLINK ``keys`` in batches, yielding to the loop between them.

    Why batching matters even though UNLINK is already non-blocking server-side:
    the client builds one command per call, and 100k names in a single UNLINK is
    a multi-megabyte request on a shared instance that also holds auth keys.
    The sleep(0)-shaped pause lets queued requests interleave with the sweep
    instead of queueing behind it.

    ``client`` exists for the worker-thread warm-up path: a redis-py pool is
    bound to the event loop that created it, so refresh_namespace() must sweep
    with the instance it built, never with the app singleton.
    """
    target = client if client is not None else redis_cache
    deleted = 0
    for batch in _chunks(list(keys), batch_size):
        deleted += await target.unlink(*batch)
        if pause:
            await asyncio.sleep(pause)
    return deleted


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

    Part 2 / Phase 4: the sweep is batched (INVALIDATE_BATCH_SIZE keys per
    UNLINK, short await between batches) so a whole-namespace clear cannot
    occupy the server for seconds at a time.
    """
    for pattern in patterns:
        try:
            keys_to_delete = await redis_cache.scan_iter(pattern)
            # Auth/session keys are not a cache. A pattern aimed at a namespace
            # should never be able to revoke sessions on its way past "*".
            if keys_to_delete:
                doomed = [k for k in keys_to_delete if not _is_auth_key(k)]
                skipped = len(keys_to_delete) - len(doomed)
                if skipped:
                    logger.warning(
                        "cache_invalidate_skipped_auth_keys",
                        extra={"pattern": pattern, "auth_keys_skipped": skipped}
                    )
                if doomed:
                    # Batched UNLINK (non-blocking reclamation) instead of one
                    # DELETE per key — bounded request size, zero event-loop stalls.
                    deleted = await _batched_unlink(doomed)
                    logger.info(
                        "cache_invalidate",
                        extra={
                            "pattern": pattern,
                            "keys_deleted": deleted or len(doomed)
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


# ============================================================================
# Zero-Gap Namespace Refresh (Part 2 / Phase 4)
# ============================================================================

async def refresh_namespace(
    prefix: str,
    warm_fn: Callable[[], Any],
    *,
    cache: Optional[Any] = None,
    patterns: Optional[List[str]] = None,
    ttl: Optional[int] = None,
    top: int = WARM_UP_TOP_SYMBOLS,
    budget_seconds: float = WARM_UP_BUDGET_SECONDS,
    per_key_timeout: float = WARM_UP_PER_KEY_TIMEOUT_SECONDS,
    verify_sweep: bool = True,
    close_cache_when_done: bool = True,
) -> Dict[str, Any]:
    """Rewrite the hot keys of ``prefix`` BEFORE the rest of it is evicted.

    Why this exists: every daily job ends by clearing a whole namespace, and the
    first reader after that pays for every page at once (a self-inflicted
    stampede). Warming the keys the next reader will actually ask for — and
    overwriting the old value rather than deleting it first — means there is
    never a moment where a hot page has no cached payload.

    Args:
        prefix: namespace stem (``rebh``, ``terminal``, ``prices``...); refuses
            auth prefixes outright, because this helper deletes.
        warm_fn: callable returning the hot payloads, either as an awaitable or
            directly. It may yield/return ``(key, value)`` or
            ``(key, value, ttl)``; awaited items are produced lazily so the
            wall-clock budget can stop a slow pass mid-stream.
        cache: a RedisCache to use. Pass one when calling from a worker thread
            with its own event loop (job_runner does) — a redis-py pool belongs
            to the loop that created it, so the app singleton must not be
            reused across loops. None = build a throwaway instance and close it.
        patterns: globs to evict after warming (default ``<prefix>:*``).
        ttl: override TTL for warmed keys that do not carry their own.
        top: passed to ``warm_fn`` as a keyword when it accepts one (a service
            reads it as "how many hot symbols"; helpers that ignore it are fine).
        budget_seconds / per_key_timeout: hard caps. A warm-up that overruns is
            abandoned, never allowed to delay the eviction behind it.
        verify_sweep: re-scan after the eviction and count what is left, so the
            caller may only skip the regular bust for a namespace this helper can
            PROVE it swept. Without proof, "the sweep silently did nothing" looks
            exactly like "the namespace was already empty" — and RedisCache
            .scan_iter() returns [] on error, so that confusion is reachable.

    Returns a report dict — never raises. The eviction step runs even when the
    warm-up fails, because stale-but-present is only safe until the job's data
    is due; correctness still requires the cold keys to go.
    """
    stem = (prefix or "").strip().rstrip(":")
    started = time.monotonic()
    report: Dict[str, Any] = {
        "prefix": stem,
        "ok": False,
        "warmed": 0,
        "warm_failed": 0,
        "kept_warm": 0,
        "skipped_auth_keys": 0,
        "scan_failed": 0,
        "residual": 0,
        "evicted": 0,
        "patterns": list(patterns or [f"{stem}:*"]),
        "error": None,
        "note": None,
    }
    if not stem:
        report["error"] = "ValueError: prefix is required"
        return report
    if _is_auth_key(stem) or any(stem == p.rstrip(":") for p in AUTH_KEY_PREFIXES):
        # The single most important refusal in this file, and it is checked before
        # the pattern check so the report names the real reason: warming a
        # namespace is allowed to delete keys, and auth keys must never be
        # deletable by a cache routine.
        report["error"] = f"ValueError: {stem} is an auth keyspace, not a cache"
        logger.error("refresh_namespace REFUSED auth keyspace stem=%s", stem)
        return report
    unsafe = [p for p in report["patterns"] if not _pattern_is_scoped(p)]
    if unsafe:
        # Refused before anything is written or deleted: an unscoped glob here
        # would sweep the whole keyspace, which is the one thing this helper must
        # never do while it is allowed to delete.
        report["error"] = f"ValueError: unscoped pattern(s) {unsafe}"
        logger.error("refresh_namespace REFUSED unscoped patterns=%s prefix=%s",
                     unsafe, stem)
        return report

    own_cache = cache is None
    if own_cache:
        from app.core.redis import RedisCache
        cache = RedisCache()
        if not await cache.init_redis():
            report["error"] = "RedisUnavailable: could not connect"
            return report
    try:
        # ── 1. Warm: overwrite the hot keys, so a reader never sees a gap ──
        warmed_keys: List[str] = []
        try:
            items = _invoke_warm_fn(warm_fn, top)
            if inspect.isawaitable(items):
                items = await asyncio.wait_for(items, timeout=budget_seconds)
            async for payload in _as_async_iterable(items):
                if (time.monotonic() - started) > budget_seconds:
                    report["note"] = "budget exceeded — remaining hot keys left cold"
                    break
                entry = await _materialise(payload, per_key_timeout)
                if entry is None:
                    report["warm_failed"] += 1
                    continue
                key, value, key_ttl = entry
                problem = cache_key_problem(key)
                if problem:
                    report["warm_failed"] += 1
                    logger.warning("warm_up_key_rejected reason=%s namespace=%s",
                                   problem, namespace_of(key))
                    continue
                # Never immortal: an expiry-less warm key survives every future
                # bust by name, which is the exact failure Phase 4 exists to fix.
                expire = key_ttl if key_ttl is not None else (
                    ttl if ttl is not None else DEFAULT_CACHE_TTL)
                if await cache.set(key, value, expire=apply_ttl_jitter(expire)):
                    warmed_keys.append(key)
                    report["warmed"] += 1
                else:
                    report["warm_failed"] += 1
        except asyncio.TimeoutError:
            report["note"] = "warm_fn timed out"
            logger.warning("refresh_namespace warm_fn timeout prefix=%s", stem)
        except Exception as e:                        # noqa: BLE001 - eviction still runs
            report["note"] = f"warm_fn failed: {type(e).__name__}"
            logger.warning("refresh_namespace warm_fn failed prefix=%s: %s", stem, e)

        # ── 2. Evict the rest, in batches, refusing auth keys ───────────
        # The just-warmed keys are skipped on purpose: deleting them here would
        # put the gap straight back where the warm-up just removed it.
        if not await cache.ensure_connection():
            # A dead cache makes every scan below return [] — indistinguishable
            # from "nothing to delete", which is precisely the lie that must not
            # be handed to the bust as a skip_prefixes licence.
            report["error"] = "RedisUnavailable: sweep probe failed"
            return report
        warmed = set(warmed_keys)
        for pattern in report["patterns"]:
            try:
                found = await cache.scan_iter(pattern)
            except Exception as e:                    # noqa: BLE001
                # Counted, not just logged: the caller (job_runner) only lets the
                # regular bust skip a namespace whose sweep verifiably completed.
                report["scan_failed"] += 1
                logger.warning("refresh_namespace scan failed pattern=%s: %s", pattern, e)
                continue
            auth_hits = {k for k in found if _is_auth_key(k)}
            report["skipped_auth_keys"] += len(auth_hits)
            kept_warm = [k for k in found if k in warmed and k not in auth_hits]
            report["kept_warm"] += len(kept_warm)
            doomed = [k for k in found if k not in warmed and k not in auth_hits]
            if doomed:
                report["evicted"] += await _batched_unlink(doomed, client=cache)

        # ── 3. Prove the sweep happened before anyone is told to skip it ──
        # Anything left that is neither warmed nor auth is residual staleness:
        # a key the eviction should have taken. Readers that repopulated the
        # namespace mid-sweep land here too, and the honest answer for that case
        # is "let the regular bust run" — an optimisation never outranks correctness.
        if verify_sweep:
            for pattern in report["patterns"]:
                try:
                    still_there = await cache.scan_iter(pattern)
                except Exception as e:                    # noqa: BLE001
                    report["scan_failed"] += 1
                    logger.warning("refresh_namespace verify failed pattern=%s: %s",
                                   pattern, e)
                    continue
                report["residual"] += sum(
                    1 for k in still_there
                    if k not in warmed and not _is_auth_key(k)
                )
        report["ok"] = True
    finally:
        if own_cache and close_cache_when_done:
            try:
                await cache.aclose()
            except Exception:                         # noqa: BLE001 - cleanup only
                pass
        report["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    if report["skipped_auth_keys"]:
        logger.warning("refresh_namespace protected auth keys prefix=%s kept=%s",
                       stem, report["skipped_auth_keys"])
    logger.info("refresh_namespace %s", json.dumps(report, sort_keys=True, default=str))
    return report


async def _as_async_iterable(items: Any):
    """Walk a sync list, a sync generator, an async iterator or an awaitable list."""
    if items is None:
        return
    if hasattr(items, "__aiter__"):
        async for item in items:
            yield item
        return
    for item in items:
        yield item


#: One hot item that could not be produced (timed out, raised, or came back
#: empty). Distinct from None so "the supplier failed" is never confused with
#: "the supplier legitimately returned nothing".
_SKIP_ITEM = object()


def _invoke_warm_fn(warm_fn: Callable, top: int) -> Any:
    """Call the supplier, handing ``top`` only to the ones that declare it.

    refresh_namespace plugs into jobs that already exist, so a warm source may
    be a zero-arg closure over its own symbol set just as easily as a function
    that wants to be told how many hot symbols to build.
    """
    try:
        params = inspect.signature(warm_fn).parameters
    except (TypeError, ValueError):        # builtins without an introspectable signature
        params = {}
    if "top" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        try:
            return warm_fn(top=top)
        except TypeError:                # declared **kwargs but rejects top: bare call
            pass
    return warm_fn()


async def _call_with_timeout(fn: Callable, timeout: float) -> Any:
    """Resolve one lazy hot value under a per-key deadline.

    The value may be a callable (or return an awaitable) precisely so a supplier
    can defer the expensive engine run until the budget for it is actually
    available. A key that misses its deadline is abandoned individually — it is
    one symbol, not the whole pass, and certainly not the eviction behind it.

    Synchronous callables run on a worker thread: the engine and terminal
    services are blocking DB code, and a deadline cannot interrupt a call that
    never yields control. Off-loop execution is what makes ``timeout`` mean
    something (the thread is left to finish; its result is simply dropped).
    """
    try:
        if inspect.iscoroutinefunction(fn):
            return await asyncio.wait_for(fn(), timeout=timeout)
        return await asyncio.wait_for(asyncio.to_thread(fn), timeout=timeout)
    except asyncio.TimeoutError:
        return _SKIP_ITEM
    except Exception as e:                            # noqa: BLE001 - one key, not all
        logger.warning("warm_fn item failed: %s", type(e).__name__)
        return _SKIP_ITEM


async def _materialise(payload: Any,
                       timeout: float) -> Optional[Tuple[str, Any, Optional[int]]]:
    """Normalise one warm item to ``(key, value, ttl)``; None when unusable.

    Accepted shapes: ``(key, value)`` and ``(key, value, ttl)``, where ``value``
    may itself be a callable / awaitable produced lazily. A value that failed to
    materialise, or that materialised to None, is refused — writing Redis `null`
    would look like a cache MISS anyway, so it would buy a fake "warmed" number
    and nothing else.
    """
    if not isinstance(payload, (list, tuple)) or len(payload) not in (2, 3):
        return None
    key, value = payload[0], payload[1]
    key_ttl = payload[2] if len(payload) == 3 else None
    if key is None or not str(key):
        return None
    if callable(value):
        value = await _call_with_timeout(value, timeout)
        if value is _SKIP_ITEM:
            return None
    elif inspect.isawaitable(value):
        try:
            value = await asyncio.wait_for(value, timeout=timeout)
        except Exception:                             # noqa: BLE001 - one key, not all
            return None
        if value is _SKIP_ITEM or value is None:
            return None
    if value is None:
        return None
    return str(key), value, key_ttl


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


async def invalidate_industry_groups_data() -> None:
    """Invalidate all industry-groups cache (latest + per-group stock lists).

    Called by the prices ingest route and the scraper ingestion route after
    financial data changes; the two import sites pre-dated this definition.
    """
    await invalidate_patterns([f"{PREFIX_INDUSTRY_GROUPS}:*"])


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


# ============================================================================
# REBH ENGINE Cache Key Generators & Invalidation
# ============================================================================

def make_rebh_engine_key(symbol: str, price: Optional[float] = None, market_cap: Optional[float] = None) -> str:
    """Key: rebh:engine:{symbol}:{price}:{market_cap} — per-company universal contract."""
    sym_norm = normalize_string(symbol)
    price_norm = normalize_float(price)
    mc_norm = normalize_float(market_cap)
    return f"{PREFIX_REBH}:engine:{sym_norm}:{price_norm}:{mc_norm}"


async def invalidate_rebh_engine_cache(symbol: Optional[str] = None) -> None:
    """Invalidate REBH engine caches for a specific symbol or the whole universe"""
    if symbol:
        sym_norm = normalize_string(symbol)
        await invalidate_patterns([f"{PREFIX_REBH}:engine:{sym_norm}:*"])
    else:
        await invalidate_patterns([f"{PREFIX_REBH}:engine:*"])


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
        f"{PREFIX_TERMINAL}:*",
        f"{PREFIX_REBH}:*"
    ])

