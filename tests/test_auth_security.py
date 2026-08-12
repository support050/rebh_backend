"""
Authentication security hardening tests.

These tests use mocks for Redis/DB where possible and cover fail-closed
behavior, lockout, token type confusion, refresh rotation, and admin auth.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from jose import jwt

# Ensure settings are loaded
from app.core.config import settings
from app.core.auth import (
    ALGORITHM,
    SECRET_KEY,
    consume_refresh_token,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_password_hash,
    hash_token,
    generate_token,
    verify_password,
    verify_token_exists,
    verify_refresh_token_exists,
)
from app.core.redis import RedisUnavailableError


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _make_user(**kwargs):
    user = MagicMock()
    user.id = kwargs.get("id", 1)
    user.email = kwargs.get("email", "user@example.com")
    user.full_name = kwargs.get("full_name", "Test User")
    user.hashed_password = kwargs.get("hashed_password", get_password_hash("ValidPass1!"))
    user.is_approved = kwargs.get("is_approved", True)
    user.is_admin = kwargs.get("is_admin", False)
    user.is_verified = kwargs.get("is_verified", True)
    user.is_locked = kwargs.get("is_locked", False)
    user.failed_login_attempts = kwargs.get("failed_login_attempts", 0)
    user.locked_until = kwargs.get("locked_until", None)
    user.reset_token_hash = kwargs.get("reset_token_hash", None)
    user.reset_token_expires_at = kwargs.get("reset_token_expires_at", None)
    return user


# ─────────────────────────────────────────────────────────────────────────
# JWT / token type tests
# ─────────────────────────────────────────────────────────────────────────

class TestJWT:
    def test_access_token_has_type_and_jti(self):
        token = create_access_token({"sub": "1", "email": "a@b.com"})
        payload = decode_token(token)
        assert payload["type"] == "access"
        assert payload["jti"]
        assert payload["sub"] == "1"

    def test_refresh_token_has_type(self):
        token = create_refresh_token({"sub": "1"})
        payload = decode_token(token)
        assert payload["type"] == "refresh"

    def test_malformed_jwt_returns_401(self):
        with pytest.raises(HTTPException) as exc:
            decode_token("not.a.jwt")
        assert exc.value.status_code == 401

    def test_invalid_signature_returns_401(self):
        token = jwt.encode(
            {"sub": "1", "exp": datetime.utcnow() + timedelta(minutes=5), "type": "access"},
            "wrong-secret-key-xxxxxxxxxxxxxxxxxxxxxxxx",
            algorithm=ALGORITHM,
        )
        with pytest.raises(HTTPException) as exc:
            decode_token(token)
        assert exc.value.status_code == 401

    def test_expired_token_returns_401(self):
        token = jwt.encode(
            {
                "sub": "1",
                "exp": datetime.utcnow() - timedelta(minutes=1),
                "type": "access",
                "jti": "x",
            },
            SECRET_KEY,
            algorithm=ALGORITHM,
        )
        with pytest.raises(HTTPException) as exc:
            decode_token(token)
        assert exc.value.status_code == 401

    def test_missing_sub_rejected_by_options(self):
        token = jwt.encode(
            {
                "exp": datetime.utcnow() + timedelta(minutes=5),
                "type": "access",
                "jti": "x",
            },
            SECRET_KEY,
            algorithm=ALGORITHM,
        )
        with pytest.raises(HTTPException) as exc:
            decode_token(token)
        assert exc.value.status_code == 401

    def test_access_and_refresh_not_confused(self):
        access = create_access_token({"sub": "1"})
        refresh = create_refresh_token({"sub": "1"})
        assert decode_token(access)["type"] == "access"
        assert decode_token(refresh)["type"] == "refresh"
        assert decode_token(access)["jti"] != decode_token(refresh)["jti"]


# ─────────────────────────────────────────────────────────────────────────
# Redis fail-closed
# ─────────────────────────────────────────────────────────────────────────

class TestRedisFailClosed:
    @pytest.mark.asyncio
    async def test_missing_jti_is_invalid(self):
        token = create_access_token({"sub": "42"})
        with patch("app.core.auth.redis_cache.auth_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            assert await verify_token_exists(42, token) is False

    @pytest.mark.asyncio
    async def test_redis_unavailable_raises(self):
        token = create_access_token({"sub": "42"})
        with patch(
            "app.core.auth.redis_cache.auth_get",
            new_callable=AsyncMock,
            side_effect=RedisUnavailableError("down"),
        ):
            with pytest.raises(RedisUnavailableError):
                await verify_token_exists(42, token)

    @pytest.mark.asyncio
    async def test_valid_jti_passes(self):
        token = create_access_token({"sub": "42"})
        with patch("app.core.auth.redis_cache.auth_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "42"
            assert await verify_token_exists(42, token) is True

    @pytest.mark.asyncio
    async def test_refresh_missing_jti_invalid(self):
        token = create_refresh_token({"sub": "7"})
        with patch("app.core.auth.redis_cache.auth_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            assert await verify_refresh_token_exists(7, token) is False

    @pytest.mark.asyncio
    async def test_refresh_redis_down_raises(self):
        token = create_refresh_token({"sub": "7"})
        with patch(
            "app.core.auth.redis_cache.auth_get",
            new_callable=AsyncMock,
            side_effect=RedisUnavailableError("down"),
        ):
            with pytest.raises(RedisUnavailableError):
                await verify_refresh_token_exists(7, token)

    @pytest.mark.asyncio
    async def test_consume_refresh_atomic_second_fails(self):
        token = create_refresh_token({"sub": "9"})
        calls = {"n": 0}

        async def getdel_once(key):
            calls["n"] += 1
            if calls["n"] == 1:
                return "9"
            return None

        with patch("app.core.auth.redis_cache.auth_getdel", side_effect=getdel_once):
            with patch("app.core.auth.redis_cache.delete", new_callable=AsyncMock):
                jti = await consume_refresh_token(9, token)
                assert jti
                with pytest.raises(HTTPException) as exc:
                    await consume_refresh_token(9, token)
                assert exc.value.status_code == 401


# ─────────────────────────────────────────────────────────────────────────
# Pending token rejection
# ─────────────────────────────────────────────────────────────────────────

class TestPendingTokenRejection:
    @pytest.mark.asyncio
    async def test_pending_scope_rejected_by_verify_token(self):
        from starlette.requests import Request
        from app.core.auth import verify_token

        token = create_access_token(
            {
                "sub": "1",
                "email": "a@b.com",
                "scope": "check_approval_only",
            }
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "GET",
            "path": "/api/auth/me",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 123),
            "server": ("test", 80),
            "scheme": "http",
        }
        request = Request(scope)
        request.state.token_payload = None

        with patch("app.core.auth.redis_cache.auth_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "1"
            # Inject cookie via empty credentials path — monkeypatch cookies
            with patch.object(type(request), "cookies", {"session_token": token}):
                with pytest.raises(HTTPException) as exc:
                    await verify_token(request, credentials=None)
                assert exc.value.status_code == 401
                assert "Pending" in str(exc.value.detail) or "pending" in str(exc.value.detail).lower() or "cannot" in str(exc.value.detail).lower()

    @pytest.mark.asyncio
    async def test_refresh_token_rejected_as_access(self):
        from starlette.requests import Request
        from app.core.auth import verify_token

        token = create_refresh_token({"sub": "1"})
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "GET",
            "path": "/api/x",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 123),
            "server": ("test", 80),
            "scheme": "http",
        }
        request = Request(scope)
        with patch.object(type(request), "cookies", {"session_token": token}):
            with pytest.raises(HTTPException) as exc:
                await verify_token(request, credentials=None)
            assert exc.value.status_code == 401


# ─────────────────────────────────────────────────────────────────────────
# Password hashing / tokens
# ─────────────────────────────────────────────────────────────────────────

class TestPasswordAndTokens:
    def test_password_hash_and_verify(self):
        h = get_password_hash("ValidPass1!")
        assert verify_password("ValidPass1!", h)
        assert not verify_password("WrongPass1!", h)

    def test_reset_token_hash_is_sha256_hex(self):
        raw = generate_token()
        digest = hash_token(raw)
        assert len(digest) == 64
        assert digest != raw
        assert hash_token(raw) == digest


# ─────────────────────────────────────────────────────────────────────────
# Email service — no secret leakage
# ─────────────────────────────────────────────────────────────────────────

class TestEmailNoLeakage:
    def test_unconfigured_smtp_does_not_log_body(self, caplog):
        from app.services.email_service import send_email
        import logging

        with patch("app.services.email_service.settings") as mock_settings:
            mock_settings.SMTP_USER = ""
            mock_settings.SMTP_PASSWORD = ""
            with caplog.at_level(logging.WARNING):
                ok = send_email(
                    "user@example.com",
                    "Reset",
                    "SECRET_TOKEN_VALUE_IN_BODY https://example.com/reset?token=abc123",
                    user_id=99,
                    purpose="password_reset",
                )
            assert ok is False
            joined = " ".join(r.message for r in caplog.records)
            assert "SECRET_TOKEN_VALUE_IN_BODY" not in joined
            assert "abc123" not in joined
            assert "user_id=99" in joined
            assert "password_reset" in joined


# ─────────────────────────────────────────────────────────────────────────
# Lockout helpers
# ─────────────────────────────────────────────────────────────────────────

class TestLockoutLogic:
    def test_temporary_lock_active(self):
        from app.api.routes.auth import _account_is_temporarily_locked

        user = _make_user(
            is_locked=True,
            locked_until=datetime.utcnow() + timedelta(minutes=10),
        )
        assert _account_is_temporarily_locked(user) is True

    def test_expired_lock_auto_unlocks(self):
        from app.api.routes.auth import _account_is_temporarily_locked

        user = _make_user(
            is_locked=True,
            locked_until=datetime.utcnow() - timedelta(minutes=1),
            failed_login_attempts=5,
        )
        assert _account_is_temporarily_locked(user) is False
        assert user.is_locked is False
        assert user.failed_login_attempts == 0


# ─────────────────────────────────────────────────────────────────────────
# Admin last-admin protection
# ─────────────────────────────────────────────────────────────────────────

class TestAdminGuards:
    def test_deps_rejects_non_admin(self):
        from app.api.deps import get_current_admin

        user = _make_user(is_admin=False)
        with pytest.raises(HTTPException) as exc:
            # sync function
            import asyncio

            # get_current_admin is sync
            get_current_admin(current_user=user)
        assert exc.value.status_code == 403

    def test_deps_allows_admin(self):
        from app.api.deps import get_current_admin

        user = _make_user(is_admin=True)
        assert get_current_admin(current_user=user) is user


# ─────────────────────────────────────────────────────────────────────────
# safeCallbackUrl (frontend mirror tested via logic duplication note)
# Backend open-redirect not applicable; frontend has safeCallbackUrl
# ─────────────────────────────────────────────────────────────────────────

class TestConfig:
    def test_required_settings_present(self):
        assert settings.SECRET_KEY
        assert len(settings.SECRET_KEY) >= 32
        assert settings.DATABASE_URL
        assert settings.REDIS_URL
        assert settings.MAX_FAILED_LOGIN_ATTEMPTS >= 1
        assert settings.LOCKOUT_DURATION_MINUTES >= 1
        assert settings.MAX_CONCURRENT_SESSIONS >= 1
