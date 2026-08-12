"""Redis cache + auth-specific fail-closed helpers."""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

import redis.asyncio as redis
from fastapi.encoders import jsonable_encoder

from app.core.config import settings

logger = logging.getLogger(__name__)


class RedisUnavailableError(Exception):
    """Raised when Redis cannot be reached for a security-critical operation."""


class RedisCache:
    def __init__(self):
        self.redis_client = None
        self.is_connected = False

    async def init_redis(self) -> bool:
        try:
            self.redis_client = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                socket_keepalive=True,
                retry_on_timeout=True,
                max_connections=50,
            )
            await self.redis_client.ping()
            self.is_connected = True
            logger.info("Redis connected successfully")
            return True
        except Exception as e:
            logger.error("Redis connection failed: %s", type(e).__name__)
            self.redis_client = None
            self.is_connected = False
            return False

    async def ensure_connection(self) -> bool:
        if not self.is_connected or not self.redis_client:
            return await self.init_redis()
        try:
            await self.redis_client.ping()
            return True
        except Exception:
            self.is_connected = False
            return await self.init_redis()

    async def _require_client(self):
        if not await self.ensure_connection() or not self.redis_client:
            raise RedisUnavailableError("Redis is unavailable")
        return self.redis_client

    # ── Generic cache (fail-open for non-auth data is OK) ───────────────

    async def set(self, key: str, value: Any, expire: int = 86400) -> bool:
        if not await self.ensure_connection():
            return False
        try:
            serializable = jsonable_encoder(value)
            serialized_value = json.dumps(serializable, ensure_ascii=False)
            result = await self.redis_client.set(key, serialized_value, ex=expire)
            return bool(result)
        except Exception as e:
            logger.error("Redis set failed for key pattern: %s (%s)", key.split(":")[0], type(e).__name__)
            return False

    async def get(self, key: str) -> Optional[Any]:
        if not await self.ensure_connection():
            return None
        try:
            value = await self.redis_client.get(key)
            if value is None:
                return None
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        except Exception as e:
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

    async def exists(self, key: str) -> bool:
        if not await self.ensure_connection():
            return False
        try:
            return await self.redis_client.exists(key) == 1
        except Exception as e:
            logger.error("Redis exists failed: %s", type(e).__name__)
            return False

    # Auth / session keys must survive application cache clears
    _AUTH_KEY_PREFIXES = (
        "access_token:",
        "access_jti:",
        "refresh_token:",
        "refresh_jti:",
        "session_index:",
        "oauth_state:",
        "oauth_link:",
        "verify_token:",
        "reset_token:",
    )

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

    async def keys(self, pattern: str) -> List[str]:
        if not await self.ensure_connection():
            return []
        try:
            keys = await self.redis_client.keys(pattern)
            return keys or []
        except Exception as e:
            logger.error("Redis keys failed: %s", type(e).__name__)
            return []

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
        client = await self._require_client()
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
        client = await self._require_client()
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
        client = await self._require_client()
        try:
            return (await client.delete(key)) > 0
        except Exception as e:
            logger.error("Auth Redis DELETE failed: %s", type(e).__name__)
            raise RedisUnavailableError("Redis DELETE failed") from e

    async def auth_getdel(self, key: str) -> Optional[str]:
        """
        Atomic GET + DELETE for refresh-token rotation.
        Only one concurrent caller can successfully consume a key.
        """
        client = await self._require_client()
        try:
            # Prefer native GETDEL (Redis >= 6.2)
            if hasattr(client, "getdel"):
                raw = await client.getdel(key)
            else:
                # Lua fallback for older Redis
                lua = """
                local v = redis.call('GET', KEYS[1])
                if v then
                  redis.call('DEL', KEYS[1])
                end
                return v
                """
                raw = await client.eval(lua, 1, key)

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
        """SET if Not eXists — used for short-lived locks. True if lock acquired."""
        client = await self._require_client()
        try:
            return bool(await client.set(key, value, nx=True, ex=expire))
        except Exception as e:
            logger.error("Auth Redis SET NX failed: %s", type(e).__name__)
            raise RedisUnavailableError("Redis SET NX failed") from e

    async def auth_scan_iter(self, pattern: str) -> List[str]:
        """Scan keys fail-closed (for session revocation)."""
        client = await self._require_client()
        try:
            keys: List[str] = []
            async for key in client.scan_iter(match=pattern):
                keys.append(key)
            return keys
        except Exception as e:
            logger.error("Auth Redis SCAN failed: %s", type(e).__name__)
            raise RedisUnavailableError("Redis SCAN failed") from e

    async def incr_with_expire(self, key: str, expire: int) -> int:
        """Atomic INCR; set TTL on first increment. Fail-closed."""
        client = await self._require_client()
        try:
            pipe = client.pipeline()
            pipe.incr(key)
            pipe.expire(key, expire, nx=True)
            results = await pipe.execute()
            return int(results[0])
        except Exception as e:
            logger.error("Auth Redis INCR failed: %s", type(e).__name__)
            raise RedisUnavailableError("Redis INCR failed") from e


redis_cache = RedisCache()


async def store_reset_token(user_id: int, token: str, expire_minutes: int = 15):
    await redis_cache.set(f"reset_token:{token}", str(user_id), expire=expire_minutes * 60)


async def get_reset_token(token: str) -> Optional[int]:
    result = await redis_cache.get(f"reset_token:{token}")
    return int(result) if result else None


async def delete_reset_token(token: str):
    await redis_cache.delete(f"reset_token:{token}")


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
