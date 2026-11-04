"""Redis cache + auth-specific fail-closed helpers.

Two failure modes live here on purpose, and they are opposites:
  * DATA path (set/get/delete/unlink/...) falls OPEN — a broken cache costs a
    DB read, never a 500.
  * AUTH path (auth_*) raises RedisUnavailableError — a token allowlist that
    cannot be read must deny, not assume.

Version floor: nothing in the deployment pins the Redis server version
(render.yaml only builds the image; the instance is configured in the
dashboard), and the local one reports 5.0.14.1. So every atomic primitive in
this module is a Lua script — supported since 2.6 — rather than GETDEL (6.2)
or ``EXPIRE ... NX`` (7.0), both of which raised on the older server and turned
a login-failure counter into a 503.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import warnings
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import redis.asyncio as redis
from fastapi.encoders import jsonable_encoder

from app.core import cache_codec
from app.core.cache_config import (
    APP_CACHE_PREFIXES,
    AUTH_KEY_PREFIXES,
    CACHE_STATS_LOG_INTERVAL_SECONDS,
    DEFAULT_CACHE_TTL,
    LOCK_TTL_SECONDS,
    REDIS_BREAKER_FAILURE_THRESHOLD,
    REDIS_BREAKER_RESET_SECONDS,
)
from app.core.config import settings

logger = logging.getLogger(__name__)


class RedisUnavailableError(Exception):
    """Raised when Redis cannot be reached for a security-critical operation."""


# ── Atomic Lua primitives (version-independent) ─────────────────────────

#: INCR + "set TTL only when this INCR created the key (or it has none)".
#: Replaces ``EXPIRE key secs NX`` (Redis 7.0+) and keeps the counter from ever
#: becoming immortal, which is what a lost EXPIRE would have caused.
LUA_INCR_WITH_EXPIRE = """
local v = redis.call('INCR', KEYS[1])
if v == 1 or redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return v
"""

#: GET + DEL in one atomic step — the refresh-token rotation consume. Replaces
#: GETDEL (Redis 6.2+); identical on every server, so behaviour does not depend
#: on which Redis a deploy happens to run.
LUA_GETDEL = """
local v = redis.call('GET', KEYS[1])
if v then
  redis.call('DEL', KEYS[1])
end
return v
"""

#: Compare-and-delete: release a single-flight lock only if we still own it.
#: A plain DEL would free a lock whose TTL expired and that was re-acquired by
#: another request mid-cook — the second holder would have its lock stolen, and
#: the stampede the lock exists to prevent would come straight back.
LUA_RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisCircuitBreaker:
    """DATA-path only: stop paying a connect-timeout per request on a dead Redis.

    After ``failure_threshold`` consecutive failures the breaker opens and every
    caller is told "no cache" instantly for ``reset_timeout`` seconds; after that
    window one attempt is let through (half-open) to decide recovery. Success
    closes it again.

    The AUTH path never asks this object — security reads keep trying and keep
    failing closed (see :meth:`RedisCache._require_auth_client`).
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self,
                 failure_threshold: int = REDIS_BREAKER_FAILURE_THRESHOLD,
                 reset_timeout: float = REDIS_BREAKER_RESET_SECONDS) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._lock = threading.Lock()
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at = 0.0

    def allow(self) -> bool:
        """True when a real connection attempt may be made right now."""
        with self._lock:
            if self._state != self.OPEN:
                return True
            if time.monotonic() - self._opened_at >= self.reset_timeout:
                self._state = self.HALF_OPEN
                return True
            return False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == self.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = self.CLOSED
            self._opened_at = 0.0

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"state": self._state, "consecutive_failures": self._failures}


# Longest prefix first: "industry:groups" must win over a bare "prices"-style
# head match, and auth prefixes must be recognised as auth, not as a namespace.
_SORTED_CACHE_PREFIXES: List[str] = sorted(
    {p.rstrip(":") for p in tuple(APP_CACHE_PREFIXES) + tuple(AUTH_KEY_PREFIXES)},
    key=len,
    reverse=True,
)
_AUTH_STEMS = frozenset(p.rstrip(":") for p in AUTH_KEY_PREFIXES)


def namespace_of(key: Any) -> str:
    """Label bucket for a key (used by stats and log lines) — never the key.

    Only ever returns a namespace stem, so a token value can never end up in a
    log line by accident.
    """
    k = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
    for prefix in _SORTED_CACHE_PREFIXES:
        if k == prefix or k.startswith(prefix + ":"):
            return "auth" if prefix in _AUTH_STEMS else prefix
    return k.split(":", 1)[0] or "other"


def mask_target(url: Optional[str]) -> str:
    """``redis://:secret@host:7424/0`` -> ``redis://host:7424/0`` — printable."""
    if not url:
        return "redis://(not configured)"
    try:
        parsed = urlparse(str(url))
    except ValueError:
        return "redis://(unparseable)"
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    scheme = parsed.scheme or "redis"
    path = parsed.path or ""
    return f"{scheme}://{host}:{port}{path}"


class RedisCache:
    def __init__(self):
        self.redis_client = None
        # Separate instance for auth/session keys, only when REDIS_AUTH_URL is
        # configured. None means "auth shares redis_client", i.e. today.
        self.auth_client = None
        self.is_connected = False
        self._breaker = RedisCircuitBreaker()
        self._scripts: Dict[str, Any] = {}
        self._stats: Dict[str, Dict[str, int]] = {}
        self._stats_lock = threading.Lock()
        self._stats_last_log = time.monotonic()
        self._server_version: Optional[str] = None
        # Namespaces already warned about (one warning each, not one per write).
        self._default_ttl_seen: set = set()

    # ── Connection management ───────────────────────────────────────────

    @staticmethod
    def _connect(url: str):
        """One place that knows how to build a client (timeouts, pool size)."""
        return redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            socket_keepalive=True,
            retry_on_timeout=True,
            max_connections=50,
        )

    def describe_target(self) -> str:
        """Printable, credential-free target — for logs and /health."""
        return mask_target(settings.REDIS_URL)

    async def init_redis(self) -> bool:
        # Script handles are bound to the pool they were loaded on.
        self._scripts.clear()
        self._server_version = None
        try:
            self.redis_client = self._connect(settings.REDIS_URL)
            await self.redis_client.ping()
            self.is_connected = True
            self._breaker.record_success()
            logger.info("Redis connected successfully target=%s", self.describe_target())
            return True
        except Exception as e:
            logger.error(
                "Redis connection failed target=%s (%s) — cache falls open, auth fails closed",
                self.describe_target(), type(e).__name__,
            )
            self.redis_client = None
            self.is_connected = False
            self._breaker.record_failure()
            return False

    async def _ensure_auth_connection(self) -> bool:
        """Connect (or re-check) the auth instance. Never breaker-gated."""
        auth_url = getattr(settings, "REDIS_AUTH_URL", None)
        if not auth_url:
            return False
        if self.auth_client is None:
            try:
                self.auth_client = self._connect(auth_url)
            except Exception as e:
                logger.error("Auth Redis connect failed target=%s (%s)",
                             mask_target(auth_url), type(e).__name__)
                return False
        try:
            await self.auth_client.ping()
            return True
        except Exception as e:
            logger.error("Auth Redis unreachable target=%s (%s) — denying (fail-closed)",
                         mask_target(auth_url), type(e).__name__)
            return False

    async def aclose(self) -> None:
        """Close this instance's pools.

        Only for instances a caller created itself (the warm-up path in
        job_runner runs in a worker thread inside its own event loop, and a
        redis-py pool is bound to the loop that created it — see
        cache_helpers.refresh_namespace). NEVER call this on the module
        singleton: the app keeps one shared pool.
        """
        for client in (self.redis_client, self.auth_client):
            if client is None:
                continue
            try:
                await client.aclose()
            except Exception as e:                     # noqa: BLE001 - cleanup only
                logger.debug("Redis client close failed: %s", type(e).__name__)
        self.redis_client = None
        self.auth_client = None
        self.is_connected = False
        self._scripts.clear()

    async def ensure_connection(self, bypass_breaker: bool = False) -> bool:
        """Data-path readiness check.

        ``bypass_breaker`` is used by the auth path: a breaker that tells the
        data path "serve from DB" must not become a reason to skip a
        security-critical read.
        """
        if not bypass_breaker and not self._breaker.allow():
            return False  # instant cache-miss instead of a 5s connect timeout
        if not self.is_connected or not self.redis_client:
            return await self.init_redis()
        try:
            await self.redis_client.ping()
            # An auth-path ping that succeeds also re-closes the breaker: with a
            # single shared instance it just proved the data path is alive too.
            self._breaker.record_success()
            return True
        except Exception:
            self.is_connected = False
            return await self.init_redis()

    async def _require_client(self):
        if not await self.ensure_connection(bypass_breaker=True) or not self.redis_client:
            raise RedisUnavailableError("Redis is unavailable")
        return self.redis_client

    async def _require_auth_client(self):
        """Client for security-critical keys.

        With REDIS_AUTH_URL set, auth keys live on their own instance (so a
        maxmemory eviction on the cache box cannot log people out). Otherwise
        this is exactly the single shared instance as before. Both paths raise
        RedisUnavailableError when the box cannot be reached — fail-closed.
        """
        if getattr(settings, "REDIS_AUTH_URL", None):
            if self.auth_client is None or not await self._ensure_auth_connection():
                raise RedisUnavailableError("Redis auth instance is unavailable")
            return self.auth_client
        return await self._require_client()

    # ── Atomic Lua execution ────────────────────────────────────────────

    async def _eval_script(self, client, name: str, body: str,
                           keys: List[str], args: List[Any]):
        """Run a Lua script, registered once per client (EVALSHA) when possible.

        ``register_script`` needs no server round trip (it hashes lazily and
        falls back to EVAL on NOSCRIPT), but test doubles that only expose
        ``eval`` are supported too.
        """
        handle = self._scripts.get(name)
        if handle is None and hasattr(client, "register_script"):
            try:
                handle = client.register_script(body)
                self._scripts[name] = handle
            except Exception:  # pragma: no cover - defensive, eval path covers it
                handle = None
        if handle is not None:
            try:
                return await handle(keys=keys, args=args)
            except Exception:
                # Server restart / SCRIPT FLUSH drops the cache: retry via EVAL.
                self._scripts.pop(name, None)
        return await client.eval(body, len(keys), *keys, *args)

    # ── In-process cache counters (no new dependency) ───────────────────

    def record_stat(self, kind: str, key: Any) -> None:
        """Count one hit/miss/set/error against the key's namespace."""
        namespace = namespace_of(key)
        due = False
        with self._stats_lock:
            bucket = self._stats.setdefault(namespace, {"hit": 0, "miss": 0, "set": 0, "error": 0})
            if kind in bucket:
                bucket[kind] += 1
            if (time.monotonic() - self._stats_last_log) >= CACHE_STATS_LOG_INTERVAL_SECONDS:
                self._stats_last_log = time.monotonic()
                due = True
        if due:
            # self, never the module-level singleton: a second RedisCache (tests,
            # a worker with its own pool) must log ITS counters.
            logger.info("cache_stats %s", json.dumps(self.stats_snapshot(), sort_keys=True))

    def breaker_state(self) -> Dict[str, Any]:
        return self._breaker.snapshot()

    def stats_snapshot(self) -> Dict[str, Any]:
        """Namespace counters + a total, for /health/detail and periodic logs."""
        with self._stats_lock:
            namespaces = {ns: dict(counts) for ns, counts in self._stats.items()}
        totals = {kind: sum(bucket[kind] for bucket in namespaces.values())
                  for kind in ("hit", "miss", "set", "error")}
        looked = totals["hit"] + totals["miss"]
        return {
            "namespaces": namespaces,
            "totals": totals,
            "hit_ratio": round(totals["hit"] / looked, 4) if looked else 0.0,
            "breaker": self._breaker.snapshot(),
        }

    def reset_stats(self) -> None:
        with self._stats_lock:
            self._stats.clear()

    # ── Health probe (never raises, never 500s) ──────────────────────

    async def health(self, timeout: float = 2.0) -> Dict[str, Any]:
        """Ping + version + latency for /health.

        A cache outage is a degradation, not an outage of the API: this returns
        a dict and lets the caller report "degraded" while still answering 200,
        so Render does not restart a container that is serving traffic fine.
        """
        info: Dict[str, Any] = {
            "status": "unavailable",
            "target": self.describe_target(),
            "version": self._server_version,
            "ping_ms": None,
            "breaker": self._breaker.snapshot()["state"],
        }
        if not self._breaker.allow():
            info["note"] = "breaker open — probe skipped"
            return info
        own_client = self.redis_client is None
        client = self.redis_client or self._connect(settings.REDIS_URL)
        try:
            started = time.perf_counter()
            await asyncio.wait_for(client.ping(), timeout=timeout)
            info["ping_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
            info["status"] = "ok"
            self._breaker.record_success()
            if not info["version"]:
                server = await asyncio.wait_for(client.info("server"), timeout=timeout)
                info["version"] = server.get("redis_version")
                self._server_version = info["version"]
        except Exception as exc:
            info["status"] = "unavailable"
            info["error"] = type(exc).__name__
            self._breaker.record_failure()
        finally:
            if own_client:
                # Do not leak a pool built just to answer one probe.
                try:
                    await client.aclose()
                except Exception:  # pragma: no cover - best effort cleanup
                    pass
        return info

    # ── Generic cache (fail-open for non-auth data is OK) ───────────────

    def resolve_ttl(self, key: Any, expire: Optional[int]) -> int:
        """Every cache key must expire — an immortal key survives every bust.

        ``set()`` has always defaulted to 24h; that fallback stays (no TTL
        changes), but it is now loud, once per namespace, so a caller that
        forgot to pass a CACHE_TTL_* is visible instead of silently writing a
        key that never ages out.
        """
        if expire is None or int(expire) <= 0:
            namespace = namespace_of(key)
            if namespace not in self._default_ttl_seen:
                self._default_ttl_seen.add(namespace)
                logger.warning(
                    "cache SET without an explicit TTL namespace=%s fallback=%ss — pass a "
                    "CACHE_TTL_* constant from app/core/cache_config.py",
                    namespace, DEFAULT_CACHE_TTL,
                )
            return DEFAULT_CACHE_TTL
        return int(expire)

    async def set(self, key: str, value: Any, expire: Optional[int] = None) -> bool:
        if not await self.ensure_connection():
            self.record_stat("error", key)
            return False
        ttl = self.resolve_ttl(key, expire)
        try:
            serializable = jsonable_encoder(value)
            serialized_value = json.dumps(serializable, ensure_ascii=False)
            # Part 3 / Phase 6: payloads over COMPRESS_MIN_BYTES are stored
            # compressed (Base64, because this client is decode_responses=True).
            # Small values come back byte-identical, and a value written before
            # the codec existed still reads back through the same path — so there
            # is no migration and no flush required to ship this.
            payload, meta = cache_codec.encode(serialized_value)
            result = await self.redis_client.set(key, payload, ex=ttl)
            if result:
                self.record_stat("set", key)
                if meta:
                    logger.info(
                        "cache_payload_compressed namespace=%s algo=%s raw_bytes=%s "
                        "stored_bytes=%s saved_pct=%s ttl=%ss",
                        namespace_of(key), meta["algo"], meta["raw_bytes"],
                        meta["stored_bytes"],
                        round(100.0 * meta["saved_bytes"] / meta["raw_bytes"], 1),
                        ttl,
                    )
            return bool(result)
        except Exception as e:
            self.record_stat("error", key)
            logger.error("Redis set failed for namespace: %s (%s)", namespace_of(key), type(e).__name__)
            return False

    async def get(self, key: str) -> Optional[Any]:
        if not await self.ensure_connection():
            self.record_stat("error", key)
            return None
        try:
            value = await self.redis_client.get(key)
            if value is None:
                self.record_stat("miss", key)
                return None
            # Decompress when the value carries our marker; anything else (every
            # pre-codec value included) is handed on untouched. A marked value
            # that will not decompress comes back as None and is reported as a
            # MISS on purpose: the alternative — returning the Base64 blob — would
            # serve a wall of garbage to the caller as if it were an API payload.
            value, codec_meta = cache_codec.decode(value)
            if value is None:
                self.record_stat("miss", key)
                logger.warning("cache_payload_unreadable namespace=%s — treated as a "
                               "miss and recomputed", namespace_of(key))
                return None
            self.record_stat("hit", key)
            if codec_meta:
                logger.debug(
                    "cache_payload_decompressed namespace=%s algo=%s stored_bytes=%s "
                    "raw_bytes=%s", namespace_of(key), codec_meta["algo"],
                    codec_meta["stored_bytes"], codec_meta["raw_bytes"])
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        except Exception as e:
            self.record_stat("error", key)
            logger.error("Redis get failed: %s", type(e).__name__)
            return None

    async def delete(self, key: str) -> bool:
        if not await self.ensure_connection():
            return False
        try:
            result = await self.redis_client.delete(key)
            return result > 0
        except Exception as e:
            logger.error("Redis delete failed: %s", type(e).__name__)
            return False

    async def unlink(self, *keys: str) -> int:
        """Non-blocking DELETE of many keys (Redis 4.0+): memory is reclaimed
        in background threads, so shared-instance sweeps never stall live
        requests. Fail-soft like delete(): 0 on unavailable/error — the
        caller's keys then age out via their TTL safety net."""
        if not keys:
            return 0
        if not await self.ensure_connection():
            return 0
        try:
            return int(await self.redis_client.unlink(*keys))
        except Exception as e:
            logger.error("Redis unlink failed (%s): %s", type(e).__name__, keys[:3])
            return 0

    async def exists(self, key: str) -> bool:
        if not await self.ensure_connection():
            return False
        try:
            return await self.redis_client.exists(key) == 1
        except Exception as e:
            logger.error("Redis exists failed: %s", type(e).__name__)
            return False

    # ── Single-flight primitives (Part 2 / Phase 3, DATA path = fail-open) ──

    async def set_nx(self, key: str, value: Any, expire: int = LOCK_TTL_SECONDS) -> bool:
        """SET NX EX — True only when this caller created the key.

        Deliberately NOT the auth ``auth_set_nx``: a lock is a cache optimisation,
        so an unreachable Redis answers "you do not hold the lock" and the caller
        simply computes the value itself (fail-open). ``SET key val NX EX n`` is
        atomic since Redis 2.6.12, so no Lua is needed here.
        """
        if not await self.ensure_connection():
            return False
        try:
            return bool(await self.redis_client.set(key, value, nx=True, ex=expire))
        except Exception as e:
            logger.error("Redis SET NX failed: %s", type(e).__name__)
            return False

    async def release_lock(self, lock_key: str, token: Any) -> bool:
        """Delete ``lock_key`` only if it still carries ``token`` (Lua, atomic)."""
        if not await self.ensure_connection():
            return False
        try:
            result = await self._eval_script(
                self.redis_client, "release_lock", LUA_RELEASE_LOCK,
                [lock_key], [str(token)],
            )
            return bool(result)
        except Exception as e:
            # A lock we cannot release ages out on its own within LOCK_TTL_SECONDS.
            logger.error("Redis lock release failed: %s", type(e).__name__)
            return False

    async def ttl(self, key: str) -> Optional[int]:
        """Remaining seconds: -1 no expiry, -2 missing, None Redis unavailable.

        The stale-while-revalidate window is derived from this (see
        cache_helpers.cache_read_through) — no envelope, so the stored payload
        format stays exactly what it has always been.
        """
        if not await self.ensure_connection():
            return None
        try:
            return int(await self.redis_client.ttl(key))
        except Exception as e:
            logger.error("Redis TTL failed: %s", type(e).__name__)
            return None

    # Auth / session keys must survive application cache clears. The list lives
    # in cache_config so redis.py, cache_bust.py and the /api/cache routes
    # cannot drift apart — a missing prefix there is a cache clear that logs
    # everybody out or resets a login lockout.
    _AUTH_KEY_PREFIXES = AUTH_KEY_PREFIXES

    def _is_auth_key(self, key: str) -> bool:
        k = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
        return any(k.startswith(p) for p in self._AUTH_KEY_PREFIXES)

    async def flush_all(self) -> bool:
        """
        Clear application cache keys only.
        Never uses FLUSHALL — auth/session JTIs and OAuth/verify keys are preserved.
        """
        if not await self.ensure_connection():
            return False
        try:
            deleted = 0
            async for key in self.redis_client.scan_iter(match="*"):
                if self._is_auth_key(key):
                    continue
                await self.redis_client.delete(key)
                deleted += 1
            logger.info("Redis application cache clear completed deleted=%s", deleted)
            return True
        except Exception as e:
            logger.error("Redis application cache clear failed: %s", type(e).__name__)
            return False

    async def keys_blocking_deprecated(self, pattern: str) -> List[str]:
        """KEYS — O(N) over the WHOLE keyspace, blocking the server while it runs.

        Renamed from ``keys()`` so that the cost is in the call site's face. On
        a shared instance (auth keys, rate-limit buckets, all caches) this is the
        command that stalls a live API. Nothing in the application should use
        it: ``scan_iter()`` is the cursor-based equivalent.
        """
        logger.warning(
            "Blocking KEYS call namespace=%s — use scan_iter() instead",
            namespace_of(pattern),
        )
        if not await self.ensure_connection():
            return []
        try:
            keys = await self.redis_client.keys(pattern)
            return keys or []
        except Exception as e:
            logger.error("Redis keys failed: %s", type(e).__name__)
            return []

    async def keys(self, pattern: str) -> List[str]:
        """Deprecated alias kept for backward compatibility — see
        :meth:`keys_blocking_deprecated` for why it is a last resort."""
        warnings.warn(
            "RedisCache.keys() is a blocking KEYS scan; use scan_iter() "
            "(cursor-based) — see keys_blocking_deprecated()",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self.keys_blocking_deprecated(pattern)

    async def scan_iter(self, pattern: str):
        if not await self.ensure_connection():
            return []
        try:
            keys = []
            async for key in self.redis_client.scan_iter(match=pattern):
                keys.append(key)
            return keys
        except Exception as e:
            logger.error("Redis scan failed: %s", type(e).__name__)
            return []

    async def publish(self, channel: str, message: str) -> int:
        if not await self.ensure_connection():
            return 0
        try:
            return await self.redis_client.publish(channel, message)
        except Exception as e:
            logger.error("Redis publish failed: %s", type(e).__name__)
            return 0

    async def pubsub(self):
        if not await self.ensure_connection():
            return None
        return self.redis_client.pubsub()

    # ── Auth fail-closed primitives ─────────────────────────────────────

    async def auth_set(self, key: str, value: str, expire: int) -> None:
        """Store an auth allowlist entry. Raises RedisUnavailableError on failure."""
        client = await self._require_auth_client()
        try:
            # Store as JSON string for compatibility with existing keys
            payload = json.dumps(value, ensure_ascii=False)
            ok = await client.set(key, payload, ex=expire)
            if not ok:
                raise RedisUnavailableError("Redis SET returned false")
        except RedisUnavailableError:
            raise
        except Exception as e:
            logger.error("Auth Redis SET failed: %s", type(e).__name__)
            raise RedisUnavailableError("Redis SET failed") from e

    async def auth_get(self, key: str) -> Optional[str]:
        """
        Get an auth allowlist entry.
        Returns None if key is missing.
        Raises RedisUnavailableError if Redis is down.
        """
        client = await self._require_auth_client()
        try:
            raw = await client.get(key)
            if raw is None:
                return None
            try:
                parsed = json.loads(raw)
                return str(parsed)
            except (json.JSONDecodeError, TypeError):
                return str(raw)
        except RedisUnavailableError:
            raise
        except Exception as e:
            logger.error("Auth Redis GET failed: %s", type(e).__name__)
            raise RedisUnavailableError("Redis GET failed") from e

    async def auth_delete(self, key: str) -> bool:
        client = await self._require_auth_client()
        try:
            return (await client.delete(key)) > 0
        except Exception as e:
            logger.error("Auth Redis DELETE failed: %s", type(e).__name__)
            raise RedisUnavailableError("Redis DELETE failed") from e

    async def auth_getdel(self, key: str) -> Optional[str]:
        """
        Atomic GET + DELETE for refresh-token rotation.
        Only one concurrent caller can successfully consume a key.

        Lua on purpose: the native GETDEL needs Redis 6.2 and nothing pins the
        deployed version (the local instance reports 5.0.14.1). Running the
        script on every version keeps rotation identical everywhere — a
        replayed refresh token can never be read twice, on any server.
        """
        client = await self._require_auth_client()
        try:
            raw = await self._eval_script(
                client, "auth_getdel", LUA_GETDEL, [key], []
            )
            if raw is None:
                return None
            try:
                return str(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                return str(raw)
        except RedisUnavailableError:
            raise
        except Exception as e:
            logger.error("Auth Redis GETDEL failed: %s", type(e).__name__)
            raise RedisUnavailableError("Redis GETDEL failed") from e

    async def auth_set_nx(self, key: str, value: str, expire: int) -> bool:
        """SET if Not eXists — used for short-lived locks. True if lock acquired.

        ``SET key val NX EX n`` has been atomic since Redis 2.6.12, so no
        compatibility work is needed here (unlike GETDEL / EXPIRE NX).
        """
        client = await self._require_auth_client()
        try:
            return bool(await client.set(key, value, nx=True, ex=expire))
        except Exception as e:
            logger.error("Auth Redis SET NX failed: %s", type(e).__name__)
            raise RedisUnavailableError("Redis SET NX failed") from e

    async def auth_scan_iter(self, pattern: str) -> List[str]:
        """Scan keys fail-closed (for session revocation)."""
        client = await self._require_auth_client()
        try:
            keys: List[str] = []
            async for key in client.scan_iter(match=pattern):
                keys.append(key)
            return keys
        except Exception as e:
            logger.error("Auth Redis SCAN failed: %s", type(e).__name__)
            raise RedisUnavailableError("Redis SCAN failed") from e

    async def incr_with_expire(self, key: str, expire: int) -> int:
        """Atomic INCR; set TTL on first increment. Fail-closed.

        One Lua script instead of an ``INCR`` + ``EXPIRE ... NX`` pipeline: the
        NX flag needs Redis 7.0, and on anything older the pipeline raised
        ("wrong number of arguments"), this method failed closed and login
        lockouts / refresh-generation bumps silently returned 503. The script
        also re-arms the TTL whenever a counter survives without one, so a
        counter can never become immortal.
        """
        client = await self._require_auth_client()
        try:
            result = await self._eval_script(
                client, "incr_with_expire", LUA_INCR_WITH_EXPIRE, [key], [int(expire)]
            )
            return int(result)
        except RedisUnavailableError:
            raise
        except Exception as e:
            logger.error("Auth Redis INCR failed: %s", type(e).__name__)
            raise RedisUnavailableError("Redis INCR failed") from e


redis_cache = RedisCache()


def cache_stats() -> Dict[str, Any]:
    """Module-level convenience: the in-process counters (see stats_snapshot)."""
    return redis_cache.stats_snapshot()


# NOTE: reset tokens used to live in Redis (store/get/delete_reset_token). They
# were removed on 2026-10-28 — the reset flow stores a SHA-256 hash on the user
# row instead, and the leftover helpers were an unused second source of truth
# that a future caller could pick by mistake.


async def store_verification_token(user_id: int, token: str, expire_minutes: int = 60):
    from app.core.auth import hash_token

    token_hash = hash_token(token)
    await redis_cache.set(f"verify_token:{token_hash}", str(user_id), expire=expire_minutes * 60)


async def get_verification_token(token: str) -> Optional[int]:
    from app.core.auth import hash_token

    token_hash = hash_token(token)
    result = await redis_cache.get(f"verify_token:{token_hash}")
    return int(result) if result else None


async def delete_verification_token(token: str):
    from app.core.auth import hash_token

    token_hash = hash_token(token)
    await redis_cache.delete(f"verify_token:{token_hash}")
