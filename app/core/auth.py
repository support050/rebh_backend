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
    """Issue an access token.

    Only `sub` is a payload claim of record. Anything the caller adds is read
    back from the database on every request (`deps.get_current_user`), so
    embedding `email` / `is_admin` here would only leak PII and privilege hints
    into a base64-readable HS256 payload — never rely on token claims for
    authorisation.
    """
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
    """Keep at most MAX_CONCURRENT_SESSIONS JTIs per user and kind; drop oldest.

    Runs as ONE Lua script. The previous `zadd -> expire -> zcard -> zrange ->
    zrem` round-trip let two concurrent logins both read "4 sessions" and both
    add, silently exceeding the cap; within a script the read-and-trim is
    atomic, and the same run also deletes the allowlist entries of the evicted
    JTIs so an overflow token dies immediately instead of lingering until its
    TTL.
    """
    index_key = f"session_index:{token_kind}:{user_id}"
    prefix = "access_token" if token_kind == "access" else "refresh_token"
    jti_prefix = "access_jti" if token_kind == "access" else "refresh_jti"
    client = await redis_cache._require_client()
    script = """
    local indexKey = KEYS[1]
    local newJti = ARGV[1]
    local ttl = tonumber(ARGV[2])
    local maxSessions = tonumber(ARGV[3])
    local allowPrefix = ARGV[4]
    local jtiPrefix = ARGV[5]
    local score = ARGV[6]
    local userId = ARGV[7]
    redis.call('ZADD', indexKey, score, newJti)
    redis.call('EXPIRE', indexKey, ttl)
    local count = tonumber(redis.call('ZCARD', indexKey))
    local overflow = count - maxSessions
    if overflow <= 0 then
      return 0
    end
    local victims = redis.call('ZRANGE', indexKey, 0, overflow - 1)
    for _, victim in ipairs(victims) do
      redis.call('DEL', allowPrefix .. ':' .. victim)
      redis.call('DEL', jtiPrefix .. ':' .. userId .. ':' .. victim)
      redis.call('ZREM', indexKey, victim)
    end
    return #victims
    """
    try:
        await client.eval(
            script,
            1,
            index_key,
            new_jti,
            str(ttl),
            str(settings.MAX_CONCURRENT_SESSIONS),
            prefix,
            jti_prefix,
            str(datetime.utcnow().timestamp()),
            str(user_id),
        )
    except RedisUnavailableError:
        raise
    except Exception as e:
        logger.error("Session limit enforcement failed: %s", type(e).__name__)
        raise RedisUnavailableError("session limit failed") from e


async def store_token_in_redis(
    user_id: int, token: str, session_kind: str = "access"
) -> None:
    """Allowlist an access token's JTI.

    `session_kind` selects the concurrency index the JTI is counted against.
    Pending approval tokens pass "pending": they are 15-minute artifacts whose
    only job is to prove the caller is waiting on this account, and letting them
    occupy the five-session budget meant a user who refreshed the pending page a
    few times could evict a real device's session before ever signing in.
    """
    try:
        payload = decode_token(token)
        jti = payload.get("jti")
        if not jti:
            raise ValueError("Missing jti in access token")
        ttl = ACCESS_TOKEN_EXPIRE_MINUTES * 60
        await redis_cache.auth_set(f"access_token:{jti}", str(user_id), expire=ttl)
        await redis_cache.auth_set(f"access_jti:{user_id}:{jti}", "1", expire=ttl)
        await _enforce_session_limit(user_id, session_kind, jti, ttl)
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
        # Pending approval tokens share the access allowlist but sit on their own
        # index, so an account-wide revocation has to clear both.
        await redis_cache.auth_delete(f"session_index:pending:{user_id}")
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

    # A pre-revocation token must not pass a check that its owner's next
    # rotation would reject.
    if int(payload.get("gen") or 0) != await get_refresh_generation(user_id):
        return False

    stored_user_id = await redis_cache.auth_get(f"refresh_token:{jti}")
    if stored_user_id is None:
        return False
    return str(stored_user_id) == str(user_id)


async def get_refresh_generation(user_id: int) -> int:
    """Current refresh-family epoch for the account (0 = never bumped).

    Bumping this number is how a reuse incident revokes the whole family: every
    refresh token carries the generation it was minted in, so one increment
    invalidates all of them at once — including the one this request is about to
    mint, which a blanket JTI sweep would wrongly destroy.
    """
    try:
        raw = await redis_cache.auth_get(f"refresh_gen:{user_id}")
    except RedisUnavailableError:
        raise
    except Exception as e:  # noqa: BLE001 — defensive: auth_get already wraps
        logger.error("Refresh generation read failed: %s", type(e).__name__)
        raise RedisUnavailableError("Redis GET failed") from e
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


async def bump_refresh_generation(user_id: int) -> int:
    """Invalidate every issued refresh token; returns the new generation."""
    try:
        return await redis_cache.incr_with_expire(
            f"refresh_gen:{user_id}", REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
        )
    except RedisUnavailableError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error("Refresh generation bump failed: %s", type(e).__name__)
        raise RedisUnavailableError("Redis INCR failed") from e


async def consume_refresh_token(user_id: int, token: str) -> str:
    """
    Atomically consume a refresh token JTI (rotation) and detect reuse.
    Returns the consumed jti on success.

    Rotation alone is not enough: if a stolen refresh token is burned by the
    legitimate user first, the thief's later attempt simply fails and the thief
    keeps trying with the copy they already hold, forever unnoticed. A
    single-use tombstone per consumed JTI turns that second attempt into an
    alarm — the family is revoked and the incident is logged (ASVS 3.3.3).
    """
    try:
        payload = decode_token(token)
        jti = payload.get("jti")
        token_type = payload.get("type")
        if not jti or token_type != "refresh":
            raise unauthorized("Invalid refresh token")

        # Epoch check before anything else: a token from a previous family is
        # dead even though its signature and expiry are still valid.
        current_gen = await get_refresh_generation(user_id)
        if int(payload.get("gen") or 0) != current_gen:
            raise unauthorized("Refresh token expired or revoked")

        stored_user_id = await redis_cache.auth_getdel(f"refresh_token:{jti}")
        if stored_user_id is None or str(stored_user_id) != str(user_id):
            try:
                await redis_cache.delete(f"refresh_jti:{user_id}:{jti}")
            except Exception:
                pass

            if stored_user_id is None:
                # Distinguish "already rotated" from "never existed".
                replay_marker = await redis_cache.auth_get(f"refresh_used:{user_id}:{jti}")
                if replay_marker:
                    logger.error(
                        "Refresh-token reuse detected — revoking the refresh family user_id=%s",
                        user_id,
                    )
                    await bump_refresh_generation(user_id)
                    # Family revocation: every refresh token issued before the
                    # bump is now dead (both parties must sign in again) and the
                    # live access sessions go with it, so the thief loses the
                    # session immediately rather than for the next 15 minutes.
                    await invalidate_all_sessions(user_id)
                    raise unauthorized("Refresh token expired or revoked")

            raise unauthorized("Refresh token expired or revoked")

        await redis_cache.delete(f"refresh_jti:{user_id}:{jti}")
        try:
            # Tombstone lives exactly as long as the stolen token would have.
            await redis_cache.auth_set(
                f"refresh_used:{user_id}:{jti}",
                "1",
                expire=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
            )
        except RedisUnavailableError:
            # Rotation already succeeded; losing the marker only disables reuse
            # detection for this one JTI, so it must not fail the user's login.
            logger.warning("Could not write refresh reuse marker for user_id=%s", user_id)
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
