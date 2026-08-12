"""Regression tests for critical/high auth security fixes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_current_admin, get_current_user
from app.api.routes import auth as auth_routes
from app.api.routes import cache as cache_routes
from app.core.auth import get_password_hash, invalidate_all_sessions, invalidate_token
from app.core.config import Settings
from app.core.csrf import CSRFMiddleware
from app.core.database import get_db
from app.core.redis import RedisUnavailableError
from app.models.user import User


@pytest.fixture
def app():
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from app.core.limiter import limiter

    application = FastAPI()
    application.state.limiter = limiter
    application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    application.add_middleware(CSRFMiddleware)
    application.include_router(auth_routes.router, prefix="/api")
    application.include_router(cache_routes.router, prefix="/api")
    return application


def _user_row(**kwargs):
    u = User(
        email=kwargs.get("email", "user@example.com"),
        hashed_password=kwargs.get("hashed_password", get_password_hash("ValidPass1!")),
        full_name=kwargs.get("full_name", "User"),
        is_verified=kwargs.get("is_verified", True),
        is_approved=kwargs.get("is_approved", True),
        is_admin=kwargs.get("is_admin", False),
        is_locked=False,
        failed_login_attempts=0,
    )
    u.id = kwargs.get("id", 1)
    u.google_sub = kwargs.get("google_sub", None)
    u.facebook_id = kwargs.get("facebook_id", None)
    u.locked_until = None
    return u


@pytest.fixture
def client(app):
    db = MagicMock()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        c.db = db
        # Seed real CSRF cookie + capture token
        r = c.get("/api/auth/csrf")
        assert r.status_code == 200
        c.csrf = r.json()["csrf_token"]
        yield c
    app.dependency_overrides.clear()


def _csrf(client, origin: str = "http://localhost:3000"):
    return {
        "x-csrf-token": client.csrf,
        "Origin": origin,
        "Content-Type": "application/json",
    }


# ── 1. Redis revocation fail-closed ─────────────────────────────────────

class TestRevocationFailClosed:
    @pytest.mark.asyncio
    async def test_invalidate_token_raises_on_redis_outage(self):
        with patch(
            "app.core.auth.redis_cache.auth_delete",
            new_callable=AsyncMock,
            side_effect=RedisUnavailableError("down"),
        ):
            with pytest.raises(RedisUnavailableError):
                await invalidate_token(1, "jti-abc")

    @pytest.mark.asyncio
    async def test_invalidate_all_sessions_raises_on_redis_outage(self):
        with patch(
            "app.core.auth.invalidate_token",
            new_callable=AsyncMock,
            side_effect=RedisUnavailableError("down"),
        ):
            with pytest.raises(RedisUnavailableError):
                await invalidate_all_sessions(1)

    def test_logout_returns_503_and_keeps_cookie_on_redis_outage(self, app, client):
        from app.core.auth import create_access_token

        user = _user_row(id=1)
        token = create_access_token({"sub": "1", "email": user.email})
        client.cookies.set("session_token", token)
        app.dependency_overrides[get_current_user] = lambda: user

        with patch(
            "app.core.auth.redis_cache.auth_get",
            new_callable=AsyncMock,
            return_value="1",
        ), patch(
            "app.api.routes.auth.invalidate_token",
            new_callable=AsyncMock,
            side_effect=RedisUnavailableError("down"),
        ):
            res = client.post("/api/auth/logout", headers=_csrf(client))

        assert res.status_code == 503, res.text
        # Cookie must remain — revocation did not succeed
        assert client.cookies.get("session_token") == token

    def test_logout_all_returns_503_on_redis_outage(self, app, client):
        user = _user_row(id=1)
        app.dependency_overrides[get_current_user] = lambda: user

        with patch(
            "app.api.routes.auth.invalidate_all_sessions",
            new_callable=AsyncMock,
            side_effect=RedisUnavailableError("down"),
        ):
            res = client.post("/api/auth/logout-all", headers=_csrf(client))

        assert res.status_code == 503, res.text

    def test_reset_password_503_when_revocation_fails(self, client):
        from app.core.auth import hash_token, generate_token
        from datetime import datetime, timedelta

        raw = generate_token()
        user = _user_row()
        user.reset_token_hash = hash_token(raw)
        user.reset_token_expires_at = datetime.utcnow() + timedelta(minutes=10)

        q = MagicMock()
        q.filter.return_value.with_for_update.return_value.first.return_value = user
        client.db.query.return_value = q

        with patch(
            "app.api.routes.auth.invalidate_all_sessions",
            new_callable=AsyncMock,
            side_effect=RedisUnavailableError("down"),
        ):
            res = client.post(
                "/api/auth/reset-password",
                json={"token": raw, "password": "NewValid1!"},
                headers=_csrf(client),
            )
        assert res.status_code == 503
        # Password must remain unchanged when revocation fails before commit
        assert verify_still_old(user)


def verify_still_old(user):
    from app.core.auth import verify_password

    return verify_password("ValidPass1!", user.hashed_password)


# ── 2. OAuth account linking ─────────────────────────────────────────────

class TestOAuthLinking:
    @pytest.mark.asyncio
    async def test_existing_verified_password_account_requires_link(self):
        db = MagicMock()
        existing = _user_row(email="victim@gmail.com", is_verified=True, google_sub=None)
        # first query by google_sub → None; second by email → existing
        db.query.return_value.filter.return_value.first.side_effect = [None, existing]

        with patch(
            "app.api.routes.auth._store_oauth_link_challenge",
            new_callable=AsyncMock,
            return_value="link-token-xyz",
        ):
            user, link_token = await auth_routes._resolve_oauth_login(
                db,
                provider="google",
                provider_subject="google-sub-1",
                email="victim@gmail.com",
                name="Victim",
            )
        assert user is None
        assert link_token == "link-token-xyz"

    @pytest.mark.asyncio
    async def test_first_time_google_creates_user(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [None, None]

        created = {}

        def add(u):
            created["user"] = u
            u.id = 99

        db.add.side_effect = add
        db.commit = MagicMock()
        db.refresh = MagicMock(side_effect=lambda u: setattr(u, "id", 99))

        user, link_token = await auth_routes._resolve_oauth_login(
            db,
            provider="google",
            provider_subject="sub-new",
            email="new@gmail.com",
            name="New",
        )
        assert link_token is None
        assert user is not None
        assert user.google_sub == "sub-new"
        assert user.is_verified is True

    @pytest.mark.asyncio
    async def test_unverified_account_requires_link(self):
        db = MagicMock()
        existing = _user_row(email="x@gmail.com", is_verified=False, google_sub=None)
        db.query.return_value.filter.return_value.first.side_effect = [None, existing]

        with patch(
            "app.api.routes.auth._store_oauth_link_challenge",
            new_callable=AsyncMock,
            return_value="link-unverified",
        ):
            user, link_token = await auth_routes._resolve_oauth_login(
                db,
                provider="google",
                provider_subject="sub-claim",
                email="x@gmail.com",
                name="X",
            )
        assert user is None
        assert link_token == "link-unverified"
        assert existing.google_sub is None
        assert existing.is_verified is False

    @pytest.mark.asyncio
    async def test_concurrent_oauth_to_same_verified_email_both_require_link(self):
        """Two OAuth subjects hitting the same verified password account must not auto-link."""
        import asyncio

        db = MagicMock()
        existing = _user_row(email="shared@gmail.com", is_verified=True, google_sub=None)

        def make_db_side_effect():
            # Each call: lookup by google_sub → None; by email → existing
            return [None, existing]

        results = []

        async def one(subject: str):
            local_db = MagicMock()
            local_db.query.return_value.filter.return_value.first.side_effect = make_db_side_effect()
            with patch(
                "app.api.routes.auth._store_oauth_link_challenge",
                new_callable=AsyncMock,
                return_value=f"link-{subject}",
            ):
                return await auth_routes._resolve_oauth_login(
                    local_db,
                    provider="google",
                    provider_subject=subject,
                    email="shared@gmail.com",
                    name="Shared",
                )

        results = await asyncio.gather(one("sub-a"), one("sub-b"))
        for user, link_token in results:
            assert user is None
            assert link_token is not None
            assert link_token.startswith("link-")


# ── 3. Cache admin-only / no FLUSHALL ────────────────────────────────────

class TestCacheAdminOnly:
    def test_non_admin_cannot_clear_stocks(self, app, client):
        user = _user_row(is_admin=False)

        def override_user():
            return user

        app.dependency_overrides[get_current_user] = override_user
        # get_current_admin depends on get_current_user — will 403
        res = client.post("/api/cache/clear/stocks", headers=_csrf(client))
        assert res.status_code == 403

    def test_clear_all_flushall_removed(self, client):
        res = client.post("/api/cache/clear/all", headers=_csrf(client))
        assert res.status_code == 404

    def test_admin_can_clear_stocks(self, app, client):
        admin = _user_row(is_admin=True)

        def override_admin():
            return admin

        app.dependency_overrides[get_current_admin] = override_admin
        with patch("app.api.routes.cache._delete_matching", new_callable=AsyncMock) as d:
            d.return_value = 3
            res = client.post("/api/cache/clear/stocks", headers=_csrf(client))
        assert res.status_code == 200
        assert res.json()["deleted_count"] == 3


# ── 4. Registration enumeration ─────────────────────────────────────────

class TestRegisterEnumeration:
    def test_existing_and_new_responses_identical_shape(self, client):
        existing = _user_row(email="dup@example.com")
        q = MagicMock()
        q.filter.return_value.first.return_value = existing
        client.db.query.return_value = q

        with patch("app.api.routes.auth.burn_password_hash_cpu"):
            res_existing = client.post(
                "/api/auth/register",
                json={"email": "dup@example.com", "password": "ValidPass1!", "full_name": "A"},
                headers=_csrf(client),
            )

        # New user path
        q2 = MagicMock()
        q2.filter.return_value.first.return_value = None
        client.db.query.return_value = q2
        created = _user_row(id=5, email="new@example.com")

        def refresh(obj):
            obj.id = 5

        client.db.refresh.side_effect = refresh
        with patch("app.api.routes.auth.store_verification_token", new_callable=AsyncMock):
            res_new = client.post(
                "/api/auth/register",
                json={"email": "new@example.com", "password": "ValidPass1!", "full_name": "B"},
                headers=_csrf(client),
            )

        assert res_existing.status_code == 201
        assert res_new.status_code == 201
        body_e = res_existing.json()
        body_n = res_new.json()
        assert body_e["user"]["id"] == 0
        assert body_n["user"]["id"] == 0
        assert body_e["message"] == body_n["message"]
        # Neither response should set pending_token
        assert "pending_token" not in res_existing.cookies
        assert "pending_token" not in res_new.cookies


# ── 5. CSRF ──────────────────────────────────────────────────────────────

class TestCSRFStrict:
    def test_sentinel_one_rejected(self, client):
        res = client.post(
            "/api/auth/login",
            json={"email": "a@b.com", "password": "ValidPass1!"},
            headers={
                "x-csrf-token": "1",
                "Origin": "http://localhost:3000",
                "Content-Type": "application/json",
            },
        )
        assert res.status_code == 403
        assert "mismatch" in res.json()["detail"].lower() or "CSRF" in res.json()["detail"]

    def test_sentinel_one_rejected_even_when_cookie_matches(self, app):
        from slowapi import _rate_limit_exceeded_handler
        from slowapi.errors import RateLimitExceeded
        from app.core.limiter import limiter

        application = FastAPI()
        application.state.limiter = limiter
        application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        application.add_middleware(CSRFMiddleware)
        application.include_router(auth_routes.router, prefix="/api")
        db = MagicMock()
        application.dependency_overrides[get_db] = lambda: (yield db)
        with TestClient(application) as c:
            c.cookies.set("csrf_token", "1")
            res = c.post(
                "/api/auth/login",
                json={"email": "a@b.com", "password": "ValidPass1!"},
                headers={
                    "x-csrf-token": "1",
                    "Origin": "http://localhost:3000",
                    "Content-Type": "application/json",
                },
            )
        assert res.status_code == 403

    def test_missing_cookie_rejected(self, app):
        # Fresh client without csrf cookie
        from slowapi import _rate_limit_exceeded_handler
        from slowapi.errors import RateLimitExceeded
        from app.core.limiter import limiter

        application = FastAPI()
        application.state.limiter = limiter
        application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        application.add_middleware(CSRFMiddleware)
        application.include_router(auth_routes.router, prefix="/api")
        db = MagicMock()
        application.dependency_overrides[get_db] = lambda: (yield db)
        with TestClient(application) as c:
            # Do NOT call /csrf
            res = c.post(
                "/api/auth/login",
                json={"email": "a@b.com", "password": "ValidPass1!"},
                headers={
                    "x-csrf-token": "some-token-without-cookie",
                    "Origin": "http://localhost:3000",
                    "Content-Type": "application/json",
                },
            )
        assert res.status_code == 403
        assert "cookie" in res.json()["detail"].lower()

    def test_wrong_header_rejected(self, client):
        res = client.post(
            "/api/auth/login",
            json={"email": "a@b.com", "password": "ValidPass1!"},
            headers={
                "x-csrf-token": "definitely-wrong-token",
                "Origin": "http://localhost:3000",
                "Content-Type": "application/json",
            },
        )
        assert res.status_code == 403


# ── 6. Production CORS / DEBUG ──────────────────────────────────────────

class TestProductionConfig:
    def test_production_debug_true_rejected(self):
        with pytest.raises(Exception):
            Settings(
                DATABASE_URL="postgresql://x",
                REDIS_URL="redis://x",
                SECRET_KEY="a" * 40,
                DEBUG=True,
                ENVIRONMENT="production",
                ALLOWED_ORIGINS="https://rebh.ai",
                INTERNAL_API_KEY="production-internal-key-ok",
            )

    def test_production_wildcard_origins_rejected(self):
        with pytest.raises(Exception):
            Settings(
                DATABASE_URL="postgresql://x",
                REDIS_URL="redis://x",
                SECRET_KEY="a" * 40,
                DEBUG=False,
                ENVIRONMENT="production",
                ALLOWED_ORIGINS="*",
                INTERNAL_API_KEY="production-internal-key-ok",
            )

    def test_production_missing_internal_api_key_rejected(self):
        with pytest.raises(Exception):
            Settings(
                DATABASE_URL="postgresql://x",
                REDIS_URL="redis://x",
                SECRET_KEY="a" * 40,
                DEBUG=False,
                ENVIRONMENT="production",
                ALLOWED_ORIGINS="https://rebh.ai",
                INTERNAL_API_KEY=None,
            )

    def test_production_weak_internal_api_key_rejected(self):
        with pytest.raises(Exception):
            Settings(
                DATABASE_URL="postgresql://x",
                REDIS_URL="redis://x",
                SECRET_KEY="a" * 40,
                DEBUG=False,
                ENVIRONMENT="production",
                ALLOWED_ORIGINS="https://rebh.ai",
                INTERNAL_API_KEY="test",
            )

    def test_development_allows_empty_internal_api_key(self):
        s = Settings(
            DATABASE_URL="postgresql://x",
            REDIS_URL="redis://x",
            SECRET_KEY="a" * 40,
            DEBUG=True,
            ENVIRONMENT="development",
            ALLOWED_ORIGINS="http://localhost:3000",
            INTERNAL_API_KEY="",
        )
        assert not (s.INTERNAL_API_KEY or "").strip()

    def test_debug_does_not_expand_cors_to_star(self):
        s = Settings(
            DATABASE_URL="postgresql://x",
            REDIS_URL="redis://x",
            SECRET_KEY="a" * 40,
            DEBUG=True,
            ENVIRONMENT="development",
            ALLOWED_ORIGINS="http://localhost:3000",
        )
        assert s.ALLOWED_ORIGINS == ["http://localhost:3000"]
        assert "*" not in s.ALLOWED_ORIGINS


# ── Pending-status JTI ───────────────────────────────────────────────────

class TestPendingStatusCheckJti:
    def test_missing_jti_returns_401(self, client):
        from app.core.auth import create_access_token

        token = create_access_token(
            {
                "sub": "1",
                "email": "u@example.com",
                "scope": "check_approval_only",
            }
        )
        client.cookies.set("pending_token", token)
        with patch(
            "app.api.routes.auth.verify_token_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            res = client.get("/api/auth/pending-status/check")
        assert res.status_code == 401

    def test_redis_down_returns_503(self, client):
        from app.core.auth import create_access_token

        token = create_access_token(
            {
                "sub": "1",
                "email": "u@example.com",
                "scope": "check_approval_only",
            }
        )
        client.cookies.set("pending_token", token)
        with patch(
            "app.api.routes.auth.verify_token_exists",
            new_callable=AsyncMock,
            side_effect=RedisUnavailableError("down"),
        ):
            res = client.get("/api/auth/pending-status/check")
        assert res.status_code == 503


# ── Email verification hashed tokens ────────────────────────────────────

class TestVerificationTokenHashed:
    @pytest.mark.asyncio
    async def test_stores_hash_not_raw(self):
        from app.core.auth import generate_token, hash_token
        from app.core.redis import store_verification_token

        raw = generate_token()
        with patch("app.core.redis.redis_cache.set", new_callable=AsyncMock) as mock_set:
            mock_set.return_value = True
            await store_verification_token(7, raw, expire_minutes=60)
        key = mock_set.await_args.args[0]
        assert key == f"verify_token:{hash_token(raw)}"
        assert raw not in key

    @pytest.mark.asyncio
    async def test_valid_verification_lookup(self):
        from app.core.auth import generate_token, hash_token
        from app.core.redis import get_verification_token

        raw = generate_token()
        with patch("app.core.redis.redis_cache.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "7"
            uid = await get_verification_token(raw)
        assert uid == 7
        assert mock_get.await_args.args[0] == f"verify_token:{hash_token(raw)}"

    @pytest.mark.asyncio
    async def test_replay_after_delete(self):
        from app.core.auth import generate_token
        from app.core.redis import delete_verification_token, get_verification_token

        raw = generate_token()
        with patch("app.core.redis.redis_cache.delete", new_callable=AsyncMock) as mock_del:
            mock_del.return_value = True
            await delete_verification_token(raw)
        with patch("app.core.redis.redis_cache.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            assert await get_verification_token(raw) is None

    @pytest.mark.asyncio
    async def test_expired_token_missing(self):
        from app.core.auth import generate_token
        from app.core.redis import get_verification_token

        raw = generate_token()
        with patch("app.core.redis.redis_cache.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            assert await get_verification_token(raw) is None


# ── Account lock invalidates sessions ────────────────────────────────────

class TestAccountLockInvalidation:
    @pytest.mark.asyncio
    async def test_lock_revokes_all_sessions(self):
        from app.core.config import settings

        user = _user_row(id=42)
        user.failed_login_attempts = settings.MAX_FAILED_LOGIN_ATTEMPTS - 1
        db = MagicMock()
        db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = user

        with patch(
            "app.api.routes.auth.invalidate_all_sessions",
            new_callable=AsyncMock,
        ) as inv:
            await auth_routes._register_failed_login(db, user)

        assert user.is_locked is True
        inv.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_lock_redis_failure_not_swallowed(self):
        from app.core.config import settings
        from fastapi import HTTPException

        user = _user_row(id=42)
        user.failed_login_attempts = settings.MAX_FAILED_LOGIN_ATTEMPTS - 1
        db = MagicMock()
        db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = user

        with patch(
            "app.api.routes.auth.invalidate_all_sessions",
            new_callable=AsyncMock,
            side_effect=RedisUnavailableError("down"),
        ):
            with pytest.raises(HTTPException) as exc:
                await auth_routes._register_failed_login(db, user)
        assert exc.value.status_code == 503


# ── flush_all preserves auth keys ────────────────────────────────────────

class TestFlushAllAuthIsolation:
    @pytest.mark.asyncio
    async def test_flush_all_skips_auth_prefixes(self):
        from app.core.redis import RedisCache

        cache = RedisCache()
        deleted = []

        async def fake_scan_iter(match="*"):
            for k in (
                "access_token:abc",
                "refresh_token:xyz",
                "tadawul_stocks:foo",
                "verify_token:hhh",
                "rs:v2:bar",
            ):
                yield k

        class FakeClient:
            async def scan_iter(self, match="*"):
                async for k in fake_scan_iter(match):
                    yield k

            async def delete(self, key):
                deleted.append(key)
                return 1

        cache.redis_client = FakeClient()
        with patch.object(cache, "ensure_connection", new_callable=AsyncMock, return_value=True):
            ok = await cache.flush_all()
        assert ok is True
        assert "tadawul_stocks:foo" in deleted
        assert "rs:v2:bar" in deleted
        assert "access_token:abc" not in deleted
        assert "refresh_token:xyz" not in deleted
        assert "verify_token:hhh" not in deleted


# ── Post-revocation token rejection ──────────────────────────────────────

class TestPostRevocationTokenRejection:
    """Prove JTIs are unusable after logout / reset / lock invalidation."""

    @pytest.mark.asyncio
    async def test_logout_invalidation_rejects_access_and_refresh(self):
        from app.core.auth import (
            create_access_token,
            create_refresh_token,
            decode_token,
            invalidate_refresh_token,
            invalidate_token,
            verify_refresh_token_exists,
            verify_token_exists,
        )

        access = create_access_token({"sub": "11", "email": "a@b.com"})
        refresh = create_refresh_token({"sub": "11", "email": "a@b.com"})
        a_jti = decode_token(access)["jti"]
        r_jti = decode_token(refresh)["jti"]
        store = {
            f"access_token:{a_jti}": "11",
            f"access_jti:11:{a_jti}": "1",
            f"refresh_token:{r_jti}": "11",
            f"refresh_jti:11:{r_jti}": "1",
        }

        async def auth_get(key):
            return store.get(key)

        async def auth_delete(key):
            store.pop(key, None)

        with patch("app.core.auth.redis_cache.auth_get", side_effect=auth_get), patch(
            "app.core.auth.redis_cache.auth_delete", side_effect=auth_delete
        ):
            assert await verify_token_exists(11, access) is True
            assert await verify_refresh_token_exists(11, refresh) is True
            # Same operations successful logout performs
            await invalidate_token(11, a_jti)
            await invalidate_refresh_token(11, r_jti)
            assert await verify_token_exists(11, access) is False
            assert await verify_refresh_token_exists(11, refresh) is False

    @pytest.mark.asyncio
    async def test_password_reset_invalidation_rejects_tokens(self):
        from app.core.auth import (
            create_access_token,
            create_refresh_token,
            decode_token,
            invalidate_all_sessions,
            verify_refresh_token_exists,
            verify_token_exists,
        )

        access = create_access_token({"sub": "22"})
        refresh = create_refresh_token({"sub": "22"})
        a_jti = decode_token(access)["jti"]
        r_jti = decode_token(refresh)["jti"]
        store = {
            f"access_token:{a_jti}": "22",
            f"access_jti:22:{a_jti}": "1",
            f"refresh_token:{r_jti}": "22",
            f"refresh_jti:22:{r_jti}": "1",
            "session_index:access:22": "1",
            "session_index:refresh:22": "1",
        }

        async def auth_get(key):
            return store.get(key)

        async def auth_delete(key):
            store.pop(key, None)

        async def auth_scan_iter(pattern):
            prefix = pattern.rstrip("*")
            return [k for k in list(store.keys()) if k.startswith(prefix)]

        with patch("app.core.auth.redis_cache.auth_get", side_effect=auth_get), patch(
            "app.core.auth.redis_cache.auth_delete", side_effect=auth_delete
        ), patch("app.core.auth.redis_cache.auth_scan_iter", side_effect=auth_scan_iter):
            await invalidate_all_sessions(22)  # reset-password path
            assert await verify_token_exists(22, access) is False
            assert await verify_refresh_token_exists(22, refresh) is False

    @pytest.mark.asyncio
    async def test_account_lock_invalidation_rejects_tokens(self):
        from app.core.auth import (
            create_access_token,
            create_refresh_token,
            decode_token,
            verify_refresh_token_exists,
            verify_token_exists,
        )
        from app.core.config import settings

        access = create_access_token({"sub": "33"})
        refresh = create_refresh_token({"sub": "33"})
        a_jti = decode_token(access)["jti"]
        r_jti = decode_token(refresh)["jti"]
        store = {
            f"access_token:{a_jti}": "33",
            f"access_jti:33:{a_jti}": "1",
            f"refresh_token:{r_jti}": "33",
            f"refresh_jti:33:{r_jti}": "1",
        }

        async def auth_get(key):
            return store.get(key)

        async def auth_delete(key):
            store.pop(key, None)

        async def auth_scan_iter(pattern):
            prefix = pattern.rstrip("*")
            return [k for k in list(store.keys()) if k.startswith(prefix)]

        user = _user_row(id=33)
        user.failed_login_attempts = settings.MAX_FAILED_LOGIN_ATTEMPTS - 1
        db = MagicMock()
        db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = user

        with patch("app.core.auth.redis_cache.auth_get", side_effect=auth_get), patch(
            "app.core.auth.redis_cache.auth_delete", side_effect=auth_delete
        ), patch("app.core.auth.redis_cache.auth_scan_iter", side_effect=auth_scan_iter):
            assert await verify_token_exists(33, access) is True
            await auth_routes._register_failed_login(db, user)
            assert user.is_locked is True
            assert await verify_token_exists(33, access) is False
            assert await verify_refresh_token_exists(33, refresh) is False
