"""Integration-style auth route tests with mocked Redis + DB session."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import create_access_token, create_refresh_token, get_password_hash
from app.core.csrf import CSRFMiddleware
from app.core.database import get_db
from app.api.routes import auth as auth_routes
from app.api.routes import admin as admin_routes
from app.api.deps import get_current_admin, get_current_user
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
    application.include_router(admin_routes.router, prefix="/api")
    return application


def _user_row(**kwargs):
    u = User(
        email=kwargs.get("email", "user@example.com"),
        hashed_password=kwargs.get("hashed_password", get_password_hash("ValidPass1!")),
        full_name=kwargs.get("full_name", "User"),
        is_verified=True,
        is_approved=kwargs.get("is_approved", True),
        is_admin=kwargs.get("is_admin", False),
        is_locked=kwargs.get("is_locked", False),
        failed_login_attempts=kwargs.get("failed_login_attempts", 0),
    )
    u.id = kwargs.get("id", 1)
    u.locked_until = kwargs.get("locked_until", None)
    return u


@pytest.fixture
def client(app):
    db = MagicMock()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db

    with TestClient(app) as c:
        c.db = db
        r = c.get("/api/auth/csrf")
        assert r.status_code == 200
        c.csrf = r.json()["csrf_token"]
        yield c

    app.dependency_overrides.clear()


def _csrf_headers(client=None, origin: str = "http://localhost:3000", token: str | None = None):
    """Use seeded client.csrf from GET /api/auth/csrf — never the sentinel '1'."""
    csrf = getattr(client, "csrf", None) if client is not None else None
    if not csrf:
        raise RuntimeError("CSRF token missing; client fixture must seed via GET /api/auth/csrf")
    return {
        "x-csrf-token": csrf,
        "Origin": origin,
        "Content-Type": "application/json",
    }


class TestLoginLockout:
    def test_wrong_password_increments(self, client):
        user = _user_row()
        q = MagicMock()
        q.filter.return_value.first.return_value = user
        q.filter.return_value.with_for_update.return_value.first.return_value = user
        client.db.query.return_value = q

        with patch("app.api.routes.auth._register_failed_login", new_callable=AsyncMock) as reg:
            res = client.post(
                "/api/auth/login",
                json={"email": "user@example.com", "password": "WrongPass1!"},
                headers=_csrf_headers(client),
            )
            assert res.status_code == 401
            assert res.json()["detail"] == "بيانات الدخول غير صحيحة"
            reg.assert_awaited_once()

    def test_locked_account_same_error(self, client):
        user = _user_row(
            is_locked=True,
            locked_until=datetime.utcnow() + timedelta(minutes=20),
        )
        q = MagicMock()
        q.filter.return_value.first.return_value = user
        client.db.query.return_value = q

        with patch("app.api.routes.auth.burn_password_hash_cpu"):
            res = client.post(
                "/api/auth/login",
                json={"email": "user@example.com", "password": "ValidPass1!"},
                headers=_csrf_headers(client),
            )
        assert res.status_code == 401
        assert res.json()["detail"] == "بيانات الدخول غير صحيحة"

    def test_unknown_email_same_error(self, client):
        q = MagicMock()
        q.filter.return_value.first.return_value = None
        client.db.query.return_value = q
        with patch("app.api.routes.auth.burn_password_hash_cpu"):
            res = client.post(
                "/api/auth/login",
                json={"email": "missing@example.com", "password": "ValidPass1!"},
                headers=_csrf_headers(client),
            )
        assert res.status_code == 401
        assert res.json()["detail"] == "بيانات الدخول غير صحيحة"


class TestRegisterEnumeration:
    def test_duplicate_email_returns_generic_success(self, client):
        existing = _user_row()
        q = MagicMock()
        q.filter.return_value.first.return_value = existing
        client.db.query.return_value = q
        with patch("app.api.routes.auth.burn_password_hash_cpu"):
            res = client.post(
                "/api/auth/register",
                json={
                    "email": "user@example.com",
                    "password": "ValidPass1!",
                    "full_name": "X",
                },
                headers=_csrf_headers(client),
            )
        assert res.status_code == 201
        body = res.json()
        assert "تم استلام الطلب" in body["message"]
        assert body["user"]["id"] == 0


class TestRefreshRotation:
    def test_refresh_consumes_token(self, client):
        user = _user_row(id=5)
        q = MagicMock()
        q.filter.return_value.first.return_value = user
        client.db.query.return_value = q

        refresh = create_refresh_token({"sub": "5", "email": user.email})

        with patch(
            "app.api.routes.auth.consume_refresh_token", new_callable=AsyncMock
        ) as consume:
            consume.return_value = "jti-1"
            with patch(
                "app.api.routes.auth.create_and_store_tokens", new_callable=AsyncMock
            ) as create:
                create.return_value = ("access-new", "refresh-new")
                client.cookies.set("refresh_token", refresh)
                res = client.post(
                    "/api/auth/refresh-token",
                    headers=_csrf_headers(client),
                )
        assert res.status_code == 200
        consume.assert_awaited()


class TestAdminAuth:
    def test_non_admin_blocked(self, app, client):
        user = _user_row(is_admin=False, is_approved=True)

        async def override_user():
            return user

        app.dependency_overrides[get_current_user] = override_user
        # get_current_admin depends on get_current_user — will reject
        res = client.get("/api/admin/users", headers=_csrf_headers(client))
        # GET may not need CSRF; admin dependency should 403
        assert res.status_code == 403

    def test_admin_ok(self, app, client):
        admin = _user_row(is_admin=True, is_approved=True)

        def override_admin():
            return admin

        app.dependency_overrides[get_current_admin] = override_admin
        q = MagicMock()
        q.offset.return_value.limit.return_value.all.return_value = []
        # list_users: db.query(User) then maybe filter
        client.db.query.return_value = q
        q.filter.return_value = q
        q.offset.return_value = q
        q.limit.return_value = q
        q.all.return_value = []

        with patch("app.api.routes.admin.redis_cache.keys", new_callable=AsyncMock) as keys:
            keys.return_value = []
            res = client.get("/api/admin/stats", headers=_csrf_headers(client))
        assert res.status_code == 200

    def test_last_admin_self_delete_blocked(self, app, client):
        admin = _user_row(id=1, is_admin=True, email="admin@example.com")

        def override_admin():
            return admin

        app.dependency_overrides[get_current_admin] = override_admin

        q = MagicMock()
        q.filter.return_value.first.return_value = admin
        q.filter.return_value.count.return_value = 1
        client.db.query.return_value = q

        res = client.delete("/api/admin/users/1", headers=_csrf_headers(client))
        assert res.status_code == 400
        assert "حسابك" in res.json()["detail"] or "مدير" in res.json()["detail"]


class TestCSRF:
    def test_missing_csrf_header_blocked(self, client):
        res = client.post(
            "/api/auth/login",
            json={"email": "a@b.com", "password": "ValidPass1!"},
            headers={"Origin": "http://localhost:3000", "Content-Type": "application/json"},
        )
        assert res.status_code == 403
        assert "CSRF" in res.json()["detail"]
