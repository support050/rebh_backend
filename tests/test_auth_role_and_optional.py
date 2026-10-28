"""Regression tests for the auth follow-up hardening task.

Covered:
  1. get_current_user_optional — must enforce the same identity checks as
     get_current_user (approval, lock, JTI allowlist, token type/scope) while
     answering "guest" instead of 401, and must read the REAL session cookie.
  2. POST /api/admin/users/{id}/role — the only is_admin write path: admin-only,
     no self-service, last-admin guard, fail-closed session revocation,
     auto-approval on grant.
  3. Email normalisation at every user-supplied entry point.
  4. delete_user_related_data — must not orphan REBH per-user rows.
  5. PUT /api/auth/profile — an email change must revoke sessions first.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_current_admin, get_current_user
from app.api.deps import get_current_user_optional as _optional_dep  # noqa: F401 (documents the target)
from app.api.routes import admin as admin_routes
from app.api.routes import auth as auth_routes
from app.core.auth import (
    RedisUnavailableError,
    create_access_token,
    create_refresh_token,
    get_password_hash,
)
from app.core.database import get_db
from app.models.user import User
from app.schemas.auth import (
    ForgetPasswordRequest,
    UserLogin,
    UserRegister,
    UserUpdate,
    normalize_email,
)
from app.services.user_cleanup import delete_user_related_data


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
    u.approved_at = None
    u.approved_by = None
    return u


def _access(user_id: int, scope: str = "user") -> str:
    return create_access_token({"sub": str(user_id), "scope": scope})


def _limiter_app(*routers):
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    from app.core.limiter import limiter

    application = FastAPI()
    application.state.limiter = limiter
    application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    for router, prefix in routers:
        application.include_router(router, prefix=prefix)
    return application


# ══════════════════════════════════════════════════════════════════════════
# 1. get_current_user_optional
# ══════════════════════════════════════════════════════════════════════════

def _run_optional(token: str | None = None, *, cookie: str | None = None,
                  header: bool = False, jti_ok: bool = True,
                  user: User | None = None, jti_error: Exception | None = None):
    """Drive get_current_user_optional with a synthetic request + mocked deps."""
    from app.api import deps as deps_mod

    request = MagicMock()
    request.headers = {"Authorization": f"Bearer {token}"} if (header and token) else {}
    request.cookies = {"session_token": cookie} if cookie else {}

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = user

    kwargs = {"side_effect": jti_error} if jti_error else {"return_value": jti_ok}
    with patch.object(deps_mod, "verify_token_exists", new_callable=AsyncMock, **kwargs):
        return asyncio.run(deps_mod.get_current_user_optional(request, db))


class TestCurrentUserOptional:
    def test_no_token_is_guest(self):
        assert _run_optional() is None

    def test_garbage_token_is_guest_not_500(self):
        assert _run_optional("not.a.jwt", header=True) is None

    def test_valid_token_resolves_user(self):
        user = _user_row(id=7)
        assert _run_optional(_access(7), header=True, user=user) is user

    def test_session_cookie_is_read(self):
        """The access JWT lives in `session_token`; an `access_token` cookie never
        exists, so reading the wrong name silently disabled cookie auth here."""
        user = _user_row(id=7)
        assert _run_optional(cookie=_access(7), user=user) is user

    def test_refresh_token_rejected(self):
        refresh = create_refresh_token({"sub": "7"})
        assert _run_optional(refresh, header=True, user=_user_row(id=7)) is None

    def test_pending_approval_scope_rejected(self):
        assert _run_optional(_access(7, "check_approval_only"),
                             header=True, user=_user_row(id=7)) is None

    def test_unapproved_user_is_guest(self):
        """THE bug this task fixed: optional used to ignore is_approved, so a
        pending user kept their watchlist/notes identity on guest-fallback routes."""
        assert _run_optional(_access(7), header=True,
                             user=_user_row(id=7, is_approved=False)) is None

    def test_locked_user_is_guest(self):
        user = _user_row(id=7)
        user.is_locked = True
        assert _run_optional(_access(7), header=True, user=user) is None

    def test_revoked_jti_is_guest(self):
        assert _run_optional(_access(7), header=True, jti_ok=False,
                             user=_user_row(id=7)) is None

    def test_missing_row_is_guest(self):
        assert _run_optional(_access(7), header=True, user=None) is None

    def test_redis_outage_degrades_to_guest_not_500(self):
        assert _run_optional(_access(7), header=True, user=_user_row(id=7),
                             jti_error=RedisUnavailableError("down")) is None

    def test_sync_handlers_may_depend_on_an_async_dep(self):
        """The dep became async while /rebh-engine notes & journal handlers are
        sync (threadpool); FastAPI must still resolve it and answer 200."""
        application = _limiter_app()

        @application.get("/probe")
        def probe(user: User = Depends(_optional_dep)):
            return {"authed": user is not None}

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = _user_row(id=7)
        application.dependency_overrides[get_db] = lambda: db
        with TestClient(application) as c, \
                patch("app.api.deps.verify_token_exists", new_callable=AsyncMock,
                      return_value=True):
            assert c.get("/probe").json() == {"authed": False}
            # header auth
            assert c.get("/probe", headers={"Authorization": f"Bearer {_access(7)}"}).json() == {
                "authed": True
            }
            # cookie auth — the form a browser actually sends
            c.cookies.set("session_token", _access(7))
            assert c.get("/probe").json() == {"authed": True}


# ══════════════════════════════════════════════════════════════════════════
# 2. POST /api/admin/users/{id}/role
# ══════════════════════════════════════════════════════════════════════════

class _RoleHarness:
    """POSTs /role against a mocked DB whose .first()/.count() answer per query."""

    def __init__(self, application, target, *, admin, admin_count=None):
        self.db = MagicMock()
        q = MagicMock()
        q.filter.return_value = q
        q.first.return_value = target
        q.count.return_value = admin_count if admin_count is not None else 0
        self.db.query.return_value = q

        application.dependency_overrides[get_current_admin] = lambda: admin
        application.dependency_overrides[get_db] = lambda: self.db

    def post(self, client, user_id, is_admin):
        return client.post(f"/api/admin/users/{user_id}/role", json={"is_admin": is_admin})


@pytest.fixture
def admin_app():
    return _limiter_app((admin_routes.router, "/api"))


@pytest.fixture
def admin_client(admin_app):
    # CSRF middleware is intentionally not mounted here (mirrors the admin-suite style)
    with TestClient(admin_app) as c:
        yield c


class TestUserRoleEndpoint:
    def test_route_exists(self, admin_app):
        assert "/api/admin/users/{user_id}/role" in {r.path for r in admin_app.routes}

    def test_endpoint_requires_admin_dependency(self, admin_app):
        route = [r for r in admin_app.routes
                 if getattr(r, "path", None) == "/api/admin/users/{user_id}/role"][0]
        names, stack = set(), list(route.dependant.dependencies)
        while stack:
            d = stack.pop()
            names.add(getattr(d.call, "__name__", ""))
            stack.extend(d.dependencies)
        assert "get_current_admin" in names

    def test_unknown_user_404(self, admin_app, admin_client):
        h = _RoleHarness(admin_app, None, admin=_user_row(id=1, is_admin=True))
        assert h.post(admin_client, 99, True).status_code == 404
        h.db.commit.assert_not_called()

    def test_self_change_refused_both_directions(self, admin_app, admin_client):
        admin = _user_row(id=1, is_admin=True)
        h = _RoleHarness(admin_app, admin, admin=admin)
        assert h.post(admin_client, 1, False).status_code == 400
        assert h.post(admin_client, 1, True).status_code == 400
        assert admin.is_admin is True
        h.db.commit.assert_not_called()

    def test_demoting_last_admin_refused(self, admin_app, admin_client):
        target = _user_row(id=2, is_admin=True)
        h = _RoleHarness(admin_app, target, admin=_user_row(id=1, is_admin=True),
                         admin_count=1)
        assert h.post(admin_client, 2, False).status_code == 400
        assert target.is_admin is True
        h.db.commit.assert_not_called()

    def test_demote_revokes_sessions_before_commit(self, admin_app, admin_client):
        admin = _user_row(id=1, is_admin=True)
        target = _user_row(id=2, is_admin=True)
        h = _RoleHarness(admin_app, target, admin=admin, admin_count=2)
        order = []
        h.db.commit.side_effect = lambda: order.append("commit")
        with patch("app.api.routes.admin.invalidate_all_sessions", new_callable=AsyncMock) as inv:
            inv.side_effect = lambda *_a, **_k: order.append("invalidate")
            res = h.post(admin_client, 2, False)
        assert res.status_code == 200, res.text
        assert target.is_admin is False
        inv.assert_awaited_once_with(2)
        assert order == ["invalidate", "commit"], "revocation must precede the DB write"

    def test_demote_redis_down_is_503_and_not_committed(self, admin_app, admin_client):
        admin = _user_row(id=1, is_admin=True)
        target = _user_row(id=2, is_admin=True)
        h = _RoleHarness(admin_app, target, admin=admin, admin_count=2)
        with patch("app.api.routes.admin.invalidate_all_sessions", new_callable=AsyncMock,
                   side_effect=RedisUnavailableError("down")):
            res = h.post(admin_client, 2, False)
        assert res.status_code == 503
        h.db.commit.assert_not_called()
        assert target.is_admin is True, "fail-closed: no partial privilege change"

    def test_noop_does_not_churn_sessions(self, admin_app, admin_client):
        target = _user_row(id=2, is_admin=False)
        h = _RoleHarness(admin_app, target, admin=_user_row(id=1, is_admin=True))
        with patch("app.api.routes.admin.invalidate_all_sessions", new_callable=AsyncMock) as inv:
            assert h.post(admin_client, 2, False).status_code == 200
        inv.assert_not_awaited()
        h.db.commit.assert_not_called()

    def test_grant_auto_approves_pending_user(self, admin_app, admin_client):
        admin = _user_row(id=1, is_admin=True)
        target = _user_row(id=2, is_admin=False, is_approved=False)
        h = _RoleHarness(admin_app, target, admin=admin)
        with patch("app.api.routes.admin.redis_cache.publish", new_callable=AsyncMock), \
             patch("app.api.routes.admin.invalidate_all_sessions", new_callable=AsyncMock):
            res = h.post(admin_client, 2, True)
        assert res.status_code == 200, res.text
        assert target.is_admin is True
        assert target.is_approved is True, "an admin that cannot sign in is useless"
        assert target.approved_by == admin.id
        assert isinstance(target.approved_at, datetime)

    def test_grant_does_not_revoke_target_sessions(self, admin_app, admin_client):
        """Promotion grants access; it must not log the promoted user out."""
        target = _user_row(id=2, is_admin=False)
        h = _RoleHarness(admin_app, target, admin=_user_row(id=1, is_admin=True))
        with patch("app.api.routes.admin.invalidate_all_sessions", new_callable=AsyncMock) as inv:
            assert h.post(admin_client, 2, True).status_code == 200
        inv.assert_not_awaited()

    def test_response_shape_is_user_response(self, admin_app, admin_client):
        target = _user_row(id=2, is_admin=False, email="dev@rebh.ai")
        h = _RoleHarness(admin_app, target, admin=_user_row(id=1, is_admin=True))
        with patch("app.api.routes.admin.invalidate_all_sessions", new_callable=AsyncMock):
            res = h.post(admin_client, 2, True)
        body = res.json()
        assert body["email"] == "dev@rebh.ai" and body["is_admin"] is True
        assert set(body) == {"id", "email", "full_name", "is_verified",
                             "is_approved", "is_admin"}

    def test_payload_must_be_boolean(self, admin_app, admin_client):
        h = _RoleHarness(admin_app, _user_row(id=2), admin=_user_row(id=1, is_admin=True))
        assert h.post(admin_client, 2, "yes-ish").status_code == 422
        assert h.post(admin_client, 2, None).status_code == 422
        assert h.post(admin_client, 2, {}).status_code == 422


# ══════════════════════════════════════════════════════════════════════════
# 3. Email normalisation
# ══════════════════════════════════════════════════════════════════════════

class TestEmailNormalization:
    @pytest.mark.parametrize("model", [UserLogin, UserRegister, UserUpdate, ForgetPasswordRequest])
    def test_case_and_whitespace_stripped(self, model):
        data = {"email": "  AyMan@Rebh.AI "}
        if model in (UserLogin, UserRegister):
            data["password"] = "ValidPass1!"
        assert model(**data).email == "ayman@rebh.ai"

    def test_partial_update_leaves_email_untouched(self):
        assert UserUpdate(full_name="ayman").email is None

    @pytest.mark.parametrize("bad", ["not-an-email", "  ", "a@b"])
    def test_invalid_email_still_rejected(self, bad):
        with pytest.raises(Exception):  # pydantic ValidationError
            UserLogin(email=bad, password="ValidPass1!")

    def test_helper_is_idempotent_and_type_safe(self):
        assert normalize_email("A@B.com") == "a@b.com"
        assert normalize_email(None) is None
        assert normalize_email(123) == 123

    def test_oauth_email_is_stored_canonical(self):
        """No subject match + no email match ⇒ the provider address is persisted
        in its canonical form, so a later password signup cannot clone the identity."""
        db = MagicMock()
        q = MagicMock()
        q.filter.return_value = q
        q.first.side_effect = [None, None]  # google_sub lookup, then email lookup
        db.query.return_value = q

        created = {}

        def _refresh(obj):
            obj.id = 77

        db.refresh.side_effect = _refresh
        db.add.side_effect = lambda obj: created.setdefault("user", obj)

        user, link_token = asyncio.run(auth_routes._resolve_oauth_login(
            db, provider="google", provider_subject="sub-1",
            email="  AyMan@Rebh.AI ", name="Ayman",
        ))
        assert link_token is None
        assert user is created["user"]
        assert user.email == "ayman@rebh.ai"
        assert user.google_sub == "sub-1"
        assert user.is_approved is False, "OAuth signups still enter the approval queue"


# ══════════════════════════════════════════════════════════════════════════
# 4. delete_user_related_data coverage
# ══════════════════════════════════════════════════════════════════════════

def _cleanup_db():
    db = MagicMock()
    q = MagicMock()
    q.filter.return_value = q
    q.delete.return_value = 1
    q.update.return_value = 1
    db.query.return_value = q
    return db


class TestUserCleanupCoverage:
    def test_all_per_user_tables_are_cleaned(self):
        counts = delete_user_related_data(_cleanup_db(), 12)
        for expected in (
            "wallet_positions", "wallet_trades", "wallet_settings",
            "wallet_weekly_studies", "analyst_notes", "trade_journals",
            "user_watchlists", "council_company_checklists", "user_prefs",
            "user_dashboards",
        ):
            assert expected in counts, f"{expected} would be orphaned on delete"

    def test_each_model_queried(self):
        from app.models.breadth import UserDashboard
        from app.models.rebh_user_data import (
            AnalystNote,
            CouncilCompanyChecklist,
            TradeJournal,
            UserWatchlist,
        )
        from app.models.user_prefs import UserPreference
        from app.models.wallet import (
            WalletPosition,
            WalletSetting,
            WalletTrade,
            WalletWeeklyStudy,
        )

        db = _cleanup_db()
        delete_user_related_data(db, 12)
        queried = {c.args[0] for c in db.query.call_args_list if c.args}
        for model in (WalletPosition, WalletTrade, WalletSetting, WalletWeeklyStudy,
                      AnalystNote, TradeJournal, UserWatchlist, CouncilCompanyChecklist,
                      UserPreference, UserDashboard):
            assert model in queried, f"{model.__name__} not deleted"
        assert User in queried, "approved_by self-reference not cleared"

    def test_approved_by_self_reference_cleared(self):
        db = _cleanup_db()
        delete_user_related_data(db, 12)
        assert db.query.return_value.filter.return_value.update.called

    def test_counts_are_ints_so_logging_never_blows_up(self):
        assert delete_user_related_data(_cleanup_db(), 12)["user_dashboards"] == 1


# ══════════════════════════════════════════════════════════════════════════
# 5. Profile email change revokes sessions
# ══════════════════════════════════════════════════════════════════════════

class TestProfileEmailChangeSessions:
    @pytest.fixture
    def auth_client(self):
        from app.core.csrf import CSRFMiddleware

        application = _limiter_app((auth_routes.router, "/api"))
        application.add_middleware(CSRFMiddleware)

        current = _user_row(id=12, email="ayman@rebh.ai")
        db = _cleanup_db()
        db.query.return_value.filter.return_value.first.return_value = current
        application.dependency_overrides[get_current_user] = lambda: current
        application.dependency_overrides[get_db] = lambda: db

        with TestClient(application) as c:
            r = c.get("/api/auth/csrf")
            assert r.status_code == 200
            c.csrf = r.json()["csrf_token"]
            c.db, c.target = db, current
            yield c
        application.dependency_overrides.clear()

    def _put(self, client, payload):
        return client.put("/api/auth/profile", headers={
            "x-csrf-token": client.csrf,
            "Origin": "http://localhost:3000",
            "Content-Type": "application/json",
        }, json=payload)

    def test_identical_email_is_a_no_change(self, auth_client):
        with patch("app.api.routes.auth.invalidate_all_sessions", new_callable=AsyncMock) as inv:
            assert self._put(auth_client, {"full_name": "Ayman"}).status_code == 200
        inv.assert_not_awaited()

    def test_real_email_change_revokes_before_commit(self, auth_client):
        order = []
        auth_client.db.commit.side_effect = lambda: order.append("commit")
        # availability probe → nobody else owns the new address
        auth_client.db.query.return_value.filter.return_value.first.side_effect = None
        auth_client.db.query.return_value.filter.return_value.first.return_value = auth_client.target
        with patch("app.api.routes.auth.invalidate_all_sessions", new_callable=AsyncMock) as inv, \
             patch("app.api.routes.auth.verify_password", return_value=True):
            inv.side_effect = lambda *_a, **_k: order.append("invalidate")
            res = self._put(auth_client, {"email": "  AYMAN@NEW.IO ",
                                          "current_password": "ValidPass1!"})
        assert res.status_code == 200, res.text
        inv.assert_awaited_once_with(12)
        assert order == ["invalidate", "commit"]
        assert auth_client.target.email == "ayman@new.io", "stored canonical form"
        assert auth_client.target.is_verified is False

    def test_redis_down_blocks_email_change(self, auth_client):
        auth_client.db.query.return_value.filter.return_value.first.side_effect = None
        auth_client.db.query.return_value.filter.return_value.first.return_value = auth_client.target
        with patch("app.api.routes.auth.invalidate_all_sessions", new_callable=AsyncMock,
                   side_effect=RedisUnavailableError("down")), \
             patch("app.api.routes.auth.verify_password", return_value=True):
            res = self._put(auth_client, {"email": "other@new.io",
                                          "current_password": "ValidPass1!"})
        assert res.status_code == 503
        auth_client.db.commit.assert_not_called()
        assert auth_client.target.email == "ayman@rebh.ai"

    def test_wrong_current_password_refused(self, auth_client):
        auth_client.db.query.return_value.filter.return_value.first.side_effect = None
        auth_client.db.query.return_value.filter.return_value.first.return_value = auth_client.target
        with patch("app.api.routes.auth.verify_password", return_value=False):
            res = self._put(auth_client, {"email": "other@new.io",
                                          "current_password": "nope"})
        assert res.status_code == 400
        assert auth_client.target.email == "ayman@rebh.ai"

    def test_taken_email_refused(self, auth_client):
        db = auth_client.db
        db.query.return_value.filter.return_value.first.side_effect = [
            auth_client.target, _user_row(id=99, email="other@new.io")
        ]
        with patch("app.api.routes.auth.verify_password", return_value=True):
            res = self._put(auth_client, {"email": "other@new.io",
                                          "current_password": "ValidPass1!"})
        assert res.status_code == 400
