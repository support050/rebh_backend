"""JWT creation/validation and Redis JTI allowlist (fail-closed)."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.core.redis import RedisUnavailableError, redis_cache

logger = logging.getLogger(__name__)

SECRET_KEY = settings.SECRET_KEY
ALGORITHM = settings.JWT_ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES
REFRESH_TOKEN_EXPIRE_DAYS = settings.REFRESH_TOKEN_EXPIRE_DAYS

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
security = HTTPBearer(auto_error=False)

# Precomputed dummy hash path helper for anti-enumeration timing.
_DUMMY_PASSWORD_HASH = pwd_context.hash("!TimingEqualizationDummyPassword9!")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def burn_password_hash_cpu(password: str) -> None:
    """Run Argon2 work comparable to registration hashing (anti-enumeration)."""
    try:
        pwd_context.hash(password)
    except Exception:
        pwd_context.verify(password, _DUMMY_PASSWORD_HASH)


def generate_token() -> str:
    """Cryptographically secure random token (32 bytes = 64 hex chars)."""
    return secrets.token_hex(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def service_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication service temporarily unavailable",
        headers={"Retry-After": "30"},
    )


def unauthorized(detail: str = "توكن غير صالح") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    now = datetime.utcnow()
    expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update(
        {
            "exp": expire,
            "iat": now,
            "jti": str(uuid4()),
            "type": "access",
        }
    )
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    now = datetime.utcnow()
    expire = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update(
        {
            "exp": expire,
            "iat": now,
            "jti": str(uuid4()),
            "type": "refresh",
        }
    )
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate JWT. Always returns 401 on failure (never 500)."""
    if not token or not isinstance(token, str):
        raise unauthorized("Authentication token missing")
    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"require_exp": True, "require_sub": True},
        )
        return payload
    except JWTError:
        raise unauthorized("توكن غير صالح")
    except HTTPException:
        raise
    except Exception:
        logger.warning("Unexpected token decode error")
        raise unauthorized("توكن غير صالح")


def parse_user_id(payload: dict) -> int:
    sub = payload.get("sub")
    if sub is None:
        raise unauthorized("Invalid token subject")
    try:
        return int(sub)
    except (TypeError, ValueError):
        raise unauthorized("Invalid token subject")


async def _enforce_session_limit(user_id: int, token_kind: str, new_jti: str, ttl: int) -> None:
    """Keep at most MAX_CONCURRENT_SESSIONS JTIs per user; drop oldest."""
    index_key = f"session_index:{token_kind}:{user_id}"
    client = await redis_cache._require_client()
    try:
        now_score = datetime.utcnow().timestamp()
        await client.zadd(index_key, {new_jti: now_score})
        await client.expire(index_key, ttl)
        max_sessions = settings.MAX_CONCURRENT_SESSIONS
        cardinality = await client.zcard(index_key)
        if cardinality <= max_sessions:
            return
        overflow = cardinality - max_sessions
        oldest = await client.zrange(index_key, 0, overflow - 1)
        for old_jti in oldest:
            if token_kind == "access":
                await redis_cache.auth_delete(f"access_token:{old_jti}")
                await redis_cache.auth_delete(f"access_jti:{user_id}:{old_jti}")
            else:
                await redis_cache.auth_delete(f"refresh_token:{old_jti}")
                await redis_cache.auth_delete(f"refresh_jti:{user_id}:{old_jti}")
            await client.zrem(index_key, old_jti)
    except RedisUnavailableError:
        raise
    except Exception as e:
        logger.error("Session limit enforcement failed: %s", type(e).__name__)
        raise RedisUnavailableError("session limit failed") from e


async def store_token_in_redis(user_id: int, token: str) -> None:
    try:
        payload = decode_token(token)
        jti = payload.get("jti")
        if not jti:
            raise ValueError("Missing jti in access token")
        ttl = ACCESS_TOKEN_EXPIRE_MINUTES * 60
        await redis_cache.auth_set(f"access_token:{jti}", str(user_id), expire=ttl)
        await redis_cache.auth_set(f"access_jti:{user_id}:{jti}", "1", expire=ttl)
        await _enforce_session_limit(user_id, "access", jti, ttl)
    except RedisUnavailableError:
        logger.error("Failed to store access token JTI (Redis unavailable)")
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to store access token JTI: %s", type(e).__name__)
        raise RedisUnavailableError("store access token failed") from e


async def store_refresh_token_in_redis(user_id: int, token: str) -> None:
    try:
        payload = decode_token(token)
        jti = payload.get("jti")
        if not jti:
            raise ValueError("Missing jti in refresh token")
        ttl = REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
        await redis_cache.auth_set(f"refresh_token:{jti}", str(user_id), expire=ttl)
        await redis_cache.auth_set(f"refresh_jti:{user_id}:{jti}", "1", expire=ttl)
        await _enforce_session_limit(user_id, "refresh", jti, ttl)
    except RedisUnavailableError:
        logger.error("Failed to store refresh token JTI (Redis unavailable)")
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to store refresh token JTI: %s", type(e).__name__)
        raise RedisUnavailableError("store refresh token failed") from e


async def invalidate_token(user_id: int, token_jti: str | None = None) -> None:
    """
    Revoke access JTI(s). Fail-closed: raises RedisUnavailableError if Redis cannot confirm deletion.
    Missing keys are treated as already revoked (success).
    """
    try:
        if token_jti:
            await redis_cache.auth_delete(f"access_token:{token_jti}")
            await redis_cache.auth_delete(f"access_jti:{user_id}:{token_jti}")
            return

        keys = await redis_cache.auth_scan_iter(f"access_jti:{user_id}:*")
        for key in keys:
            jti = key.split(":")[-1]
            await redis_cache.auth_delete(f"access_token:{jti}")
            await redis_cache.auth_delete(key)
        await redis_cache.auth_delete(f"session_index:access:{user_id}")
    except RedisUnavailableError:
        logger.error("Access token invalidation failed: Redis unavailable user_id=%s", user_id)
        raise


async def invalidate_refresh_token(user_id: int, token_jti: str | None = None) -> None:
    """Revoke refresh JTI(s). Fail-closed on Redis errors."""
    try:
        if token_jti:
            await redis_cache.auth_delete(f"refresh_token:{token_jti}")
            await redis_cache.auth_delete(f"refresh_jti:{user_id}:{token_jti}")
            return

        keys = await redis_cache.auth_scan_iter(f"refresh_jti:{user_id}:*")
        for key in keys:
            jti = key.split(":")[-1]
            await redis_cache.auth_delete(f"refresh_token:{jti}")
            await redis_cache.auth_delete(key)
        await redis_cache.auth_delete(f"session_index:refresh:{user_id}")
    except RedisUnavailableError:
        logger.error("Refresh token invalidation failed: Redis unavailable user_id=%s", user_id)
        raise


async def invalidate_all_sessions(user_id: int) -> None:
    """Invalidate every access + refresh JTI for a user. Fail-closed."""
    await invalidate_token(user_id)
    await invalidate_refresh_token(user_id)


async def verify_token_exists(user_id: int, token: str) -> bool:
    """
    Fail-closed JTI allowlist check for access tokens.
    Raises RedisUnavailableError if Redis is down.
    Returns False if JTI is missing/mismatched.
    """
    payload = decode_token(token)
    jti = payload.get("jti")
    token_type = payload.get("type")
    if not jti or token_type != "access":
        return False

    stored_user_id = await redis_cache.auth_get(f"access_token:{jti}")
    if stored_user_id is None:
        return False
    return str(stored_user_id) == str(user_id)


async def verify_refresh_token_exists(user_id: int, token: str) -> bool:
    """Fail-closed JTI allowlist check for refresh tokens (non-consuming)."""
    payload = decode_token(token)
    jti = payload.get("jti")
    token_type = payload.get("type")
    if not jti or token_type != "refresh":
        return False

    stored_user_id = await redis_cache.auth_get(f"refresh_token:{jti}")
    if stored_user_id is None:
        return False
    return str(stored_user_id) == str(user_id)


async def consume_refresh_token(user_id: int, token: str) -> str:
    """
    Atomically consume a refresh token JTI (rotation).
    Returns the consumed jti on success.
    """
    try:
        payload = decode_token(token)
        jti = payload.get("jti")
        token_type = payload.get("type")
        if not jti or token_type != "refresh":
            raise unauthorized("Invalid refresh token")

        stored_user_id = await redis_cache.auth_getdel(f"refresh_token:{jti}")
        if stored_user_id is None or str(stored_user_id) != str(user_id):
            try:
                await redis_cache.delete(f"refresh_jti:{user_id}:{jti}")
            except Exception:
                pass
            raise unauthorized("Refresh token expired or revoked")

        await redis_cache.delete(f"refresh_jti:{user_id}:{jti}")
        return jti
    except HTTPException:
        raise
    except RedisUnavailableError:
        logger.error("Refresh token consume failed: Redis unavailable")
        raise service_unavailable()


async def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """
    Extract + validate access token from Authorization header or session_token cookie.
    Rejects refresh tokens and pending/check_approval_only tokens.
    Attaches decoded payload to request.state to avoid re-decoding downstream.
    """
    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    else:
        token = request.cookies.get("session_token")

    if not token:
        raise unauthorized("Authentication token missing")

    payload = decode_token(token)

    if payload.get("type") != "access":
        raise unauthorized("Invalid token type")

    if payload.get("scope") == "check_approval_only":
        raise unauthorized("Pending token cannot access this resource")

    user_id = parse_user_id(payload)

    try:
        if not await verify_token_exists(user_id, token):
            raise unauthorized("توكن غير صالح أو منتهي")
    except RedisUnavailableError:
        logger.error("Access token validation failed: Redis unavailable")
        raise service_unavailable()

    request.state.token_payload = payload
    request.state.token_user_id = user_id
    return token
