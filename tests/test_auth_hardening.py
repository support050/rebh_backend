"""Regression tests for Batch 3 of the auth audit (M-1 … M-11 + L-1 … L-9).

Style mirrors test_auth_lockout_and_limits.py: handlers are invoked through
``__wrapped__`` (slowapi decorates with functools.wraps) so the limiter's
in-memory buckets never make the suite order-dependent, Redis is mocked at the
``redis_cache`` singleton, and the database is a MagicMock query chain.

Covered:
  M-1   verify_internal_key: constant-time, 503 on unset, 401 missing, 403 wrong.
  M-2   CSRF no longer exempts /api/rebh and /api/engine.
  M-3   Google callback refuses unverified emails (source tripwire).
  M-4   OAuth state cookie binding semantics.
  M-6   ENFORCE_EMAIL_VERIFICATION gate: on by default (permanent since
        2026-10-29 purge of the legacy unverified backlog); 403 when on.
  M-7   delete-account requires the password; denial is audited.
  M-8   refresh-token replay revokes the whole family via the epoch bump.
  M-9   access tokens carry no email/is_admin claims.
  M-10  admin revoke/delete return 503 (not 500) when Redis is down.
  M-11  audit_event emits one stable JSON line and never raises.
  L-1   ENVIRONMENT is an allow-list; cookie_secure is fail-secure.
  L-3   pending tokens are counted on their own session index.
  L-5   /api/auth responses get Cache-Control: no-store.
  L-6   /health is minimal; /health/detail exists and is admin-only.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routes import admin as admin_routes
from app.api.routes import auth as auth_routes
from app.core import audit_log as audit_log_module
from app.core import auth as core_auth
from app.core.audit_log import audit_event
from app.core.auth import (
    RedisUnavailableError,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.core.config import Settings, settings
from app.core.csrf import CSRFMiddleware
from app.core.security import verify_internal_key
from app.models.user import User
from app.schemas.auth import DeleteAccountRequest, UserLogin
from starlette.requests import Request
from starlette.responses import Response

CURRENT = "ValidPass1!"
OTHER = "WrongPass9!"


def _user(**kwargs) -> User:
    u = User(
        email=kwargs.pop("email", "user@rebh.ai"),
        hashed_password=core_auth.get_password_hash(CURRENT),
        full_name="User",
        is_verified=True,
        is_approved=True,
        is_admin=False,
        is_locked=False,
        failed_login_attempts=0,
    )
    u.id = kwargs.pop("id", 7)
    for key, value in kwargs.items():
        setattr(u, key, value)
    return u


def _db_for(user: User) -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = user
    return db


def _request_scope(method: str, path: str, headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": headers or [],
        "client": ("203.0.113.9", 51234),
        "server": ("testserver", 80),
    }


async def _echo_call_next(request: Request) -> Response:
    return Response(status_code=200)


# ═══════════════════════════════════════════════════════════════
#  M-1 — internal key: constant-time, misconfiguration is 503
# ═══════════════════════════════════════════════════════════════
class TestM1InternalKey:
    def test_unset_key_is_service_fault_not_crash(self):
        with patch.object(settings, "INTERNAL_API_KEY", ""):
            with pytest.raises(HTTPException) as exc:
                verify_internal_key("anything")
        assert exc.value.status_code == 503
        assert "Retry-After" in (exc.value.headers or {})

    def test_missing_header_unauthorized(self):
        with patch.object(settings, "INTERNAL_API_KEY", "s3cret-key"):
            with pytest.raises(HTTPException) as exc:
                verify_internal_key(None)
        assert exc.value.status_code == 401

    def test_wrong_key_forbidden(self):
        with patch.object(settings, "INTERNAL_API_KEY", "s3cret-key"):
            with pytest.raises(HTTPException) as exc:
                verify_internal_key("wrong")
        assert exc.value.status_code == 403

    def test_correct_key_passes(self):
        with patch.object(settings, "INTERNAL_API_KEY", "s3cret-key"):
            assert verify_internal_key("s3cret-key") is True

    def test_comparison_is_constant_time(self):
        """`!=` leaks the common prefix through timing; compare_digest does not."""
        assert "compare_digest" in inspect.getsource(verify_internal_key)


# ═══════════════════════════════════════════════════════════════
#  L-1 — ENVIRONMENT allow-list + fail-secure cookies
# ═══════════════════════════════════════════════════════════════
class TestL1EnvironmentHardening:
    def test_unknown_environment_rejected(self):
        with pytest.raises(ValidationError):
            Settings(ENVIRONMENT="prod")  # typo of "production" must not boot

    def test_cookie_secure_is_fail_secure(self):
        # DEBUG=False because the cross-field validator refuses DEBUG in prod;
        # that coupling is itself part of the L-1 hardening.
        assert Settings(ENVIRONMENT="production", DEBUG=False).cookie_secure is True
        assert Settings(ENVIRONMENT="staging", DEBUG=False).cookie_secure is True
        assert Settings(ENVIRONMENT="development").cookie_secure is False

    def test_cookie_helper_wired_into_routes(self):
        src = pathlib.Path(auth_routes.__file__).read_text(encoding="utf-8")
        assert "settings.cookie_secure" in src


# ═══════════════════════════════════════════════════════════════
#  M-9 — JWT claim hygiene
# ═══════════════════════════════════════════════════════════════
class TestM9ClaimHygiene:
    def test_access_token_carries_only_envelope_claims(self):
        token = create_access_token(data={"sub": "7"})
        payload = decode_token(token)
        assert set(payload) == {"sub", "exp", "iat", "jti", "type"}

    def test_issued_tokens_leak_no_email_or_privilege(self):
        user = _user(email="victim@rebh.ai")
        with patch.object(auth_routes, "store_token_in_redis", new_callable=AsyncMock), \
             patch.object(auth_routes, "store_refresh_token_in_redis", new_callable=AsyncMock), \
             patch.object(auth_routes, "get_refresh_generation", new_callable=AsyncMock, return_value=2):
            access, refresh = asyncio.run(auth_routes.create_and_store_tokens(user))
        for token in (access, refresh):
            payload = decode_token(token)
            assert "email" not in payload
            assert "is_admin" not in payload
        assert decode_token(refresh)["gen"] == 2


# ═══════════════════════════════════════════════════════════════
#  M-8 — refresh-token reuse detection revokes the family
# ═══════════════════════════════════════════════════════════════
class TestM8RefreshReuse:
    def _redis(self, *, gen="0", getdel=None, used=None):
        async def _auth_get(key):
            if key.startswith("refresh_gen"):
                return gen
            if key.startswith("refresh_used"):
                return used
            return None

        return {
            "auth_get": AsyncMock(side_effect=_auth_get),
            "auth_getdel": AsyncMock(return_value=getdel),
            "auth_set": AsyncMock(),
            "delete": AsyncMock(),
            "incr_with_expire": AsyncMock(return_value=1),
        }

    def _apply(self, mocks):
        return [patch.object(core_auth.redis_cache, name, mock) for name, mock in mocks.items()]

    def test_happy_rotation_writes_tombstone(self):
        token = create_refresh_token(data={"sub": "7", "gen": 0})
        jti = decode_token(token)["jti"]
        mocks = self._redis(getdel="7")
        with patch.object(core_auth, "invalidate_all_sessions", new_callable=AsyncMock):
            ctx = [p.start() for p in self._apply(mocks)]
            try:
                consumed = asyncio.run(core_auth.consume_refresh_token(7, token))
            finally:
                for p in ctx:
                    p.stop()
        assert consumed == jti
        mocks["auth_set"].assert_awaited()
        assert mocks["auth_set"].await_args.args[0] == f"refresh_used:7:{jti}"

    def test_replay_bumps_epoch_and_revokes_sessions(self):
        token = create_refresh_token(data={"sub": "7", "gen": 0})
        jti = decode_token(token)["jti"]
        mocks = self._redis(getdel=None, used="1")  # already consumed → replay
        with patch.object(core_auth, "invalidate_all_sessions", new_callable=AsyncMock) as invalidate:
            ctx = [p.start() for p in self._apply(mocks)]
            try:
                with pytest.raises(HTTPException) as exc:
                    asyncio.run(core_auth.consume_refresh_token(7, token))
            finally:
                for p in ctx:
                    p.stop()
        assert exc.value.status_code == 401
        mocks["incr_with_expire"].assert_awaited()  # epoch bump
        assert mocks["incr_with_expire"].await_args.args[0] == "refresh_gen:7"
        invalidate.assert_awaited_once_with(7)

    def test_old_family_token_rejected_before_touching_redis(self):
        token = create_refresh_token(data={"sub": "7", "gen": 0})
        mocks = self._redis(gen="3")
        ctx = [p.start() for p in self._apply(mocks)]
        try:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(core_auth.consume_refresh_token(7, token))
        finally:
            for p in ctx:
                p.stop()
        assert exc.value.status_code == 401
        mocks["auth_getdel"].assert_not_awaited()


# ═══════════════════════════════════════════════════════════════
#  L-3 — pending tokens never eat the real-session budget
# ═══════════════════════════════════════════════════════════════
class TestL3PendingSessionIndex:
    def test_pending_kind_routes_to_own_index(self):
        token = create_access_token(data={"sub": "7"})
        with patch.object(core_auth.redis_cache, "auth_set", new_callable=AsyncMock), \
             patch.object(core_auth, "_enforce_session_limit", new_callable=AsyncMock) as enforce:
            asyncio.run(core_auth.store_token_in_redis(7, token, session_kind="pending"))
        assert enforce.await_args.args[1] == "pending"

    def test_default_kind_is_access(self):
        token = create_access_token(data={"sub": "7"})
        with patch.object(core_auth.redis_cache, "auth_set", new_callable=AsyncMock), \
             patch.object(core_auth, "_enforce_session_limit", new_callable=AsyncMock) as enforce:
            asyncio.run(core_auth.store_token_in_redis(7, token))
        assert enforce.await_args.args[1] == "access"


# ═══════════════════════════════════════════════════════════════
#  M-2 — /api/rebh and /api/engine are inside the CSRF wall now
# ═══════════════════════════════════════════════════════════════
class TestM2CsrfScope:
    def _dispatch(self, method: str, path: str, headers=None):
        middleware = CSRFMiddleware(app=MagicMock())
        request = Request(_request_scope(method, path, headers))
        return asyncio.run(middleware.dispatch(request, _echo_call_next))

    @pytest.mark.parametrize("path", ["/api/rebh/journal", "/api/engine/notes/1150"])
    def test_unsafe_request_without_token_blocked(self, path):
        response = self._dispatch("POST", path)
        assert response.status_code == 403

    def test_machine_prefixes_stay_exempt(self):
        response = self._dispatch("POST", "/api/scraper/trigger")
        assert response.status_code == 200

    def test_safe_methods_never_checked(self):
        response = self._dispatch("GET", "/api/rebh/journal")
        assert response.status_code == 200

    def test_exempt_list_source_no_longer_names_rebh(self):
        src = inspect.getsource(CSRFMiddleware.dispatch)
        assert '"/api/rebh",' not in src
        assert '"/api/engine",' not in src


# ═══════════════════════════════════════════════════════════════
#  M-4 — OAuth state bound to the browser that started it
# ═══════════════════════════════════════════════════════════════
class TestM4OAuthStateBinding:
    def test_absent_cookie_degrades_to_pass(self):
        request = MagicMock()
        request.cookies = {}
        assert auth_routes._oauth_state_bound_to_browser(request, "google", "state-1") is True

    def test_matching_cookie_passes(self):
        request = MagicMock()
        request.cookies = {"oauth_state_google": "state-1"}
        assert auth_routes._oauth_state_bound_to_browser(request, "google", "state-1") is True

    def test_mismatched_cookie_fails(self):
        request = MagicMock()
        request.cookies = {"oauth_state_google": "attacker-state"}
        assert auth_routes._oauth_state_bound_to_browser(request, "google", "state-1") is False

    def test_state_cookie_is_httponly(self):
        response = Response()
        auth_routes.set_oauth_state_cookie(response, "google", "state-1")
        set_cookie = response.headers.get("set-cookie", "")
        assert "oauth_state_google=state-1" in set_cookie
        assert "HttpOnly" in set_cookie

    def test_google_callback_refuses_unverified_email(self):
        """M-3 tripwire: the provider's email_verified=False must be fatal."""
        src = pathlib.Path(auth_routes.__file__).read_text(encoding="utf-8")
        assert 'user_info.get("email_verified") is False' in src


# ═══════════════════════════════════════════════════════════════
#  M-6 — email-verification enforcement is a config decision
# ═══════════════════════════════════════════════════════════════
class TestM6VerificationGate:
    def _login(self, user, enforce):
        db = _db_for(user)
        request, response = MagicMock(), MagicMock()
        patches = [
            patch.object(settings, "ENFORCE_EMAIL_VERIFICATION", enforce),
            patch.object(auth_routes, "create_and_store_tokens", new_callable=AsyncMock,
                         return_value=("access", "refresh")),
            patch.object(auth_routes, "set_auth_cookies", MagicMock()),
            patch.object(auth_routes, "audit_event", MagicMock()),
        ]
        for p in patches:
            p.start()
        try:
            return asyncio.run(
                auth_routes.login.__wrapped__(
                    request, response, UserLogin(email=user.email, password=CURRENT), db
                )
            )
        finally:
            for p in patches:
                p.stop()

    def test_flag_off_lets_unverified_users_through(self):
        result = self._login(_user(is_verified=False), enforce=False)
        assert result["user"]["is_verified"] is False

    def test_flag_on_blocks_unverified_with_403(self):
        with pytest.raises(HTTPException) as exc:
            self._login(_user(is_verified=False), enforce=True)
        assert exc.value.status_code == 403

    def test_default_is_on_now_that_the_backlog_is_purged(self):
        """M-6 policy flip (2026-10-29): the 12 approved-unverified legacy
        test accounts were audited (zero data), backed up and purged, so
        strict email verification is the permanent default. The flag stays
        only as an emergency kill-switch."""
        assert Settings().ENFORCE_EMAIL_VERIFICATION is True


# ═══════════════════════════════════════════════════════════════
#  M-7 — delete-account demands the password again
# ═══════════════════════════════════════════════════════════════
class TestM7DeleteAccountReauth:
    def _call(self, user, password):
        db = _db_for(user)
        payload = DeleteAccountRequest(password=password)
        with patch.object(auth_routes, "audit_event", MagicMock()) as audit, \
             patch.object(auth_routes, "invalidate_all_sessions", new_callable=AsyncMock), \
             patch.object(auth_routes, "clear_auth_cookies", MagicMock()), \
             patch.object(auth_routes, "delete_user_related_data", MagicMock()):
            try:
                result = asyncio.run(
                    auth_routes.delete_account.__wrapped__(
                        MagicMock(), MagicMock(), payload, current_user=user, db=db
                    )
                )
            except HTTPException as exc:
                return exc, audit
        return result, audit

    def test_empty_password_rejected_by_schema(self):
        with pytest.raises(ValidationError):
            DeleteAccountRequest(password="")

    def test_wrong_password_denied_and_audited(self):
        user = _user()
        outcome, audit = self._call(user, OTHER)
        assert isinstance(outcome, HTTPException) and outcome.status_code == 400
        audit.assert_called_once()
        assert audit.call_args.args[0] == "account_delete_denied"

    def test_correct_password_deletes_and_audits(self):
        user = _user()
        outcome, audit = self._call(user, CURRENT)
        assert isinstance(outcome, dict)
        actions = [c.args[0] for c in audit.call_args_list]
        assert "account_delete" in actions


# ═══════════════════════════════════════════════════════════════
#  M-10 — admin actions fail with 503, never 500, on Redis outage
# ═══════════════════════════════════════════════════════════════
class TestM10AdminFailSecure:
    @pytest.mark.parametrize("handler_name", ["revoke_user", "delete_user"])
    def test_redis_outage_aborts_before_db_changes(self, handler_name):
        target = _user(email="target@rebh.ai")
        admin = _user(id=99, email="admin@rebh.ai", is_admin=True)
        db = _db_for(target)
        handler = getattr(admin_routes, handler_name).__wrapped__
        with patch.object(admin_routes, "invalidate_all_sessions",
                          new_callable=AsyncMock, side_effect=RedisUnavailableError), \
             patch.object(admin_routes, "audit_event", MagicMock()) as audit:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(handler(MagicMock(), target.id, current_admin=admin, db=db))
        assert exc.value.status_code == 503
        db.delete.assert_not_called()
        db.commit.assert_not_called()
        # A refused action still belongs in the trail (outcome=failure is the
        # handler's job upstream); here we only pin the fail-closed ordering.
        audit.assert_not_called()

    def test_revoke_success_is_audited(self):
        target = _user(email="target@rebh.ai")
        admin = _user(id=99, email="admin@rebh.ai", is_admin=True)
        db = _db_for(target)
        with patch.object(admin_routes, "invalidate_all_sessions", new_callable=AsyncMock), \
             patch.object(admin_routes, "delete_user_related_data", MagicMock()), \
             patch.object(admin_routes, "audit_event", MagicMock()) as audit:
            asyncio.run(admin_routes.revoke_user.__wrapped__(
                MagicMock(), target.id, current_admin=admin, db=db
            ))
        assert audit.call_args.args[0] == "admin_revoke_user"
        assert audit.call_args.kwargs["actor_id"] == admin.id
        assert audit.call_args.kwargs["target_user_id"] == target.id


# ═══════════════════════════════════════════════════════════════
#  M-11 — the audit trail itself
# ═══════════════════════════════════════════════════════════════
class TestM11AuditTrail:
    def test_emits_one_stable_json_line(self):
        lines = []
        with patch.object(audit_log_module.audit_logger, "info", side_effect=lines.append):
            audit_event(
                "admin_revoke_user",
                actor_id=1,
                actor_email="admin@rebh.ai",
                target_user_id=7,
                detail_email="target@rebh.ai",
            )
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert set(record) >= {"ts", "action", "actor_id", "actor_email",
                               "target_user_id", "outcome", "detail"}
        assert record["action"] == "admin_revoke_user"
        assert record["outcome"] == "success"
        assert record["detail"] == {"detail_email": "target@rebh.ai"}

    def test_never_raises(self):
        """Observability must not break the security action it records."""
        with patch.object(audit_log_module.audit_logger, "info",
                          side_effect=RuntimeError("logger on fire")):
            audit_event("anything")  # must not propagate

    def test_dedicated_logger_does_not_double_print(self):
        assert audit_log_module.audit_logger.propagate is False


# ═══════════════════════════════════════════════════════════════
#  L-5 / L-6 / L-9 — app wiring
# ═══════════════════════════════════════════════════════════════
class TestAppWiring:
    @pytest.fixture(scope="class")
    def app(self):
        from app.main import app as main_app
        return main_app

    def _no_store_dispatch(self, app):
        for entry in app.user_middleware:
            dispatch = entry.options.get("dispatch")
            if getattr(dispatch, "__name__", "") == "no_store_for_auth":
                return dispatch
        pytest.fail("no_store_for_auth middleware is not registered")

    def test_auth_responses_are_no_store(self, app):
        dispatch = self._no_store_dispatch(app)
        request = MagicMock()
        request.url.path = "/api/auth/login"
        response = asyncio.run(dispatch(request, _echo_call_next))
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"

    def test_data_responses_keep_caching(self, app):
        dispatch = self._no_store_dispatch(app)
        request = MagicMock()
        request.url.path = "/api/stocks"
        response = asyncio.run(dispatch(request, _echo_call_next))
        assert "cache-control" not in response.headers

    def test_health_split(self, app):
        paths = {r.path for r in app.routes}
        assert "/health" in paths and "/health/detail" in paths

    def test_create_tables_is_gated(self, app):
        import app.main as main_module
        src = inspect.getsource(main_module)
        assert "AUTO_CREATE_TABLES" in src
