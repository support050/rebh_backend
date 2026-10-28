"""Regression tests for Batch 2 of the auth audit (H-1 … H-4).

Covered:
  1. H-3 — an EXPIRED lockout must be cleared AND COMMITTED. The reset used to
     live in memory only, inside a helper whose caller committed solely on the
     locked branch, so the row stayed ``is_locked = true`` forever while
     ``deps.get_current_user`` 401'd every request and ``/refresh-token`` kept
     minting tokens for the same locked row.
  2. H-4 — a lockout keyed only on the account is a denial-of-service weapon.
     Locking now needs five failures from ONE IP, or the cross-IP soft cap.
  3. H-1 — ``PUT /api/auth/profile`` refuses a password change without the
     current password.
  4. H-2 — the slowapi decorators that actually protect the auth surface, and
     the ``app.state.limiter`` wiring they depend on.

Endpoints are invoked through ``__wrapped__`` (slowapi decorates with
functools.wraps): that keeps the limiter's in-memory bucket out of the unit
tests, so a 5/minute login cap cannot make the suite order-dependent.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.deps import get_current_user
from app.api.routes import auth as auth_routes
from app.core.auth import get_password_hash, verify_password
from app.core.config import settings
from app.core.limiter import limiter
from app.core.redis import RedisUnavailableError
from app.models.user import User
from app.schemas.auth import (
    MAX_PASSWORD_LENGTH,
    ResetPasswordRequest,
    UserLogin,
    UserUpdate,
)
from app.services.account_lock import is_temporarily_locked, resolve_lockout

CURRENT = "ValidPass1!"
NEW = "ValidPass2!"


def _user(**kwargs) -> User:
    u = User(
        email=kwargs.pop("email", "user@rebh.ai"),
        hashed_password=get_password_hash(CURRENT),
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
    db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = user
    return db


def _request_with_access_payload(user_id: int = 7) -> MagicMock:
    request = MagicMock()
    request.state.token_payload = {"sub": str(user_id), "type": "access"}
    request.state.token_user_id = user_id
    request.client.host = "203.0.113.9"
    return request


# ─────────────────────────────────────────────────────────────────────────────
# H-3 — expired lockouts must be persisted, at every reader
# ─────────────────────────────────────────────────────────────────────────────

class TestExpiredLockoutIsPersisted:
    def test_expired_lock_clears_and_commits(self):
        user = _user(
            is_locked=True,
            locked_until=datetime.utcnow() - timedelta(minutes=1),
            failed_login_attempts=5,
        )
        db = MagicMock()

        assert resolve_lockout(db, user) is False
        assert user.is_locked is False
        assert user.locked_until is None
        assert user.failed_login_attempts == 0
        db.commit.assert_called_once_with()

    def test_active_lock_does_not_write(self):
        user = _user(is_locked=True, locked_until=datetime.utcnow() + timedelta(minutes=20))
        db = MagicMock()

        assert resolve_lockout(db, user) is True
        db.commit.assert_not_called()

    def test_healthy_account_does_not_write(self):
        user = _user()
        db = MagicMock()

        assert resolve_lockout(db, user) is False
        db.commit.assert_not_called()

    def test_administrative_lock_without_expiry_stays_locked(self):
        user = _user(is_locked=True, locked_until=None)
        db = MagicMock()

        assert resolve_lockout(db, user) is True
        assert user.is_locked is True
        db.commit.assert_not_called()

    def test_pure_predicate_never_touches_the_session(self):
        user = _user(
            is_locked=True,
            locked_until=datetime.utcnow() - timedelta(minutes=1),
            failed_login_attempts=4,
        )
        assert is_temporarily_locked(user) is False
        assert user.is_locked is False  # healed in memory, nothing committed

    def test_failed_heal_does_not_change_the_answer(self):
        user = _user(is_locked=True, locked_until=datetime.utcnow() - timedelta(minutes=1))
        db = MagicMock()
        db.commit.side_effect = RuntimeError("connection lost")

        assert resolve_lockout(db, user) is False
        db.rollback.assert_called_once_with()

    def test_get_current_user_self_heals_instead_of_bricking_the_account(self):
        """The exact production failure: locked row past expiry, valid JWT."""
        user = _user(
            is_locked=True,
            locked_until=datetime.utcnow() - timedelta(minutes=1),
            failed_login_attempts=5,
        )
        db = _db_for(user)

        assert get_current_user(_request_with_access_payload(), token="x", db=db) is user
        db.commit.assert_called_once_with()

    def test_get_current_user_still_rejects_an_active_lock(self):
        user = _user(is_locked=True, locked_until=datetime.utcnow() + timedelta(minutes=10))
        with pytest.raises(HTTPException) as exc:
            get_current_user(_request_with_access_payload(), token="x", db=_db_for(user))
        assert exc.value.status_code == 401


class TestRefreshTokenRespectsTheLock:
    def _call_refresh(self, user: User):
        request = MagicMock()
        request.cookies = {"refresh_token": "raw-refresh"}
        response = MagicMock()
        db = _db_for(user)

        async def scenario():
            with patch.object(auth_routes, "decode_token",
                              return_value={"type": "refresh", "sub": str(user.id)}), \
                 patch.object(auth_routes, "consume_refresh_token", new_callable=AsyncMock), \
                 patch.object(auth_routes, "create_and_store_tokens",
                              new_callable=AsyncMock, return_value=("acc", "ref")), \
                 patch.object(auth_routes, "set_auth_cookies"):
                return await auth_routes.refresh_token_endpoint.__wrapped__(
                    request, response, db
                )

        return asyncio.run(scenario()), db

    def test_active_lock_can_no_longer_refresh(self):
        user = _user(is_locked=True, locked_until=datetime.utcnow() + timedelta(minutes=10))
        with pytest.raises(HTTPException) as exc:
            self._call_refresh(user)
        assert exc.value.status_code == 401

    def test_administrative_lock_without_expiry_is_refused_too(self):
        """The old helper only looked at locked_until here, so a permanent lock
        kept issuing access tokens."""
        user = _user(is_locked=True, locked_until=None)
        with pytest.raises(HTTPException) as exc:
            self._call_refresh(user)
        assert exc.value.status_code == 401

    def test_expired_lock_is_healed_then_refresh_succeeds(self):
        user = _user(is_locked=True, locked_until=datetime.utcnow() - timedelta(minutes=1),
                     failed_login_attempts=5)
        payload, db = self._call_refresh(user)
        assert payload["user"]["id"] == user.id
        assert user.is_locked is False
        db.commit.assert_called_once_with()


# ─────────────────────────────────────────────────────────────────────────────
# H-4 — lockout must not be a denial-of-service weapon
# ─────────────────────────────────────────────────────────────────────────────

def _register(user: User, client_ip: str | None, per_ip):
    """Run _register_failed_login with the per-(IP, account) counter stubbed.

    `per_ip` is the counter value, or the string "down" to simulate Redis.
    """
    db = _db_for(user)
    incr = (
        AsyncMock(side_effect=RedisUnavailableError("down"))
        if per_ip == "down"
        else AsyncMock(return_value=per_ip)
    )

    async def scenario():
        with patch.object(auth_routes.redis_cache, "incr_with_expire", incr), \
             patch.object(auth_routes, "invalidate_all_sessions",
                          new_callable=AsyncMock) as inv:
            await auth_routes._register_failed_login(db, user, client_ip=client_ip)
            return inv

    return asyncio.run(scenario()), db


class TestLockoutThresholds:
    def test_five_failures_from_other_ips_does_not_lock_the_victim(self):
        """Before: five wrong passwords aimed at somebody's email locked them
        out — a free denial of service against any known address."""
        user = _user(failed_login_attempts=settings.MAX_FAILED_LOGIN_ATTEMPTS - 1)
        _register(user, client_ip="203.0.113.9", per_ip=2)
        assert user.is_locked is False
        assert user.failed_login_attempts == settings.MAX_FAILED_LOGIN_ATTEMPTS

    def test_five_failures_from_one_ip_locks_and_revokes_sessions(self):
        user = _user(failed_login_attempts=settings.MAX_FAILED_LOGIN_ATTEMPTS - 1)
        inv, db = _register(user, client_ip="203.0.113.9", per_ip=settings.MAX_FAILED_LOGIN_ATTEMPTS)
        assert user.is_locked is True
        assert user.locked_until > datetime.utcnow()
        db.commit.assert_called_once_with()
        inv.assert_awaited_once_with(user.id)

    def test_distributed_attempt_is_still_capped_by_the_soft_ceiling(self):
        user = _user(failed_login_attempts=settings.MAX_FAILED_LOGIN_ATTEMPTS_SOFT_CAP - 1)
        _register(user, client_ip="198.51.100.7", per_ip=1)
        assert user.is_locked is True

    def test_unknown_ip_falls_back_to_the_strict_account_counter(self):
        user = _user(failed_login_attempts=settings.MAX_FAILED_LOGIN_ATTEMPTS - 1)
        _register(user, client_ip=None, per_ip=None)
        assert user.is_locked is True

    def test_redis_outage_falls_back_instead_of_500ing_the_login_form(self):
        user = _user(failed_login_attempts=settings.MAX_FAILED_LOGIN_ATTEMPTS - 1)
        _register(user, client_ip="203.0.113.9", per_ip="down")
        assert user.is_locked is True  # strict path, and no exception surfaced

    def test_below_threshold_never_locks(self):
        user = _user(failed_login_attempts=1)
        _register(user, client_ip="203.0.113.9", per_ip=2)
        assert user.is_locked is False

    def test_configuration_keeps_the_two_thresholds_sane(self):
        assert settings.MAX_FAILED_LOGIN_ATTEMPTS_SOFT_CAP >= settings.MAX_FAILED_LOGIN_ATTEMPTS
        assert settings.LOCKOUT_DURATION_MINUTES >= 1

    def test_password_reset_unlocks_the_victim(self):
        """The self-service escape hatch that makes a lockout recoverable
        without an admin — asserted explicitly so it cannot regress."""
        user = _user(
            is_locked=True,
            locked_until=datetime.utcnow() + timedelta(minutes=20),
            failed_login_attempts=9,
            reset_token_hash="hashed",
            reset_token_expires_at=datetime.utcnow() + timedelta(minutes=5),
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = user

        payload = ResetPasswordRequest(token="raw-token", password=NEW)
        with patch.object(auth_routes, "hash_token", return_value="hashed"), \
             patch.object(auth_routes, "invalidate_all_sessions", new_callable=AsyncMock):
            result = asyncio.run(
                auth_routes.reset_password.__wrapped__(MagicMock(), payload, db)
            )

        assert result == {"message": "تم تغيير كلمة المرور بنجاح"}
        assert user.is_locked is False
        assert user.locked_until is None
        assert user.failed_login_attempts == 0
        assert verify_password(NEW, user.hashed_password)


# ─────────────────────────────────────────────────────────────────────────────
# H-1 — a password change must prove the old password
# ─────────────────────────────────────────────────────────────────────────────

class TestPasswordChangeRequiresCurrent:
    def _run(self, user: User, payload: UserUpdate):
        async def go():
            db = _db_for(user)
            original_hash = user.hashed_password
            with patch.object(auth_routes, "invalidate_all_sessions", new_callable=AsyncMock) as inv:
                result = await auth_routes.update_profile.__wrapped__(
                    MagicMock(), payload, current_user=user, db=db
                )
            return result, inv, original_hash, db

        return asyncio.run(go())

    def test_missing_current_password_is_refused(self):
        user = _user()
        with pytest.raises(HTTPException) as exc:
            self._run(user, UserUpdate(password=NEW))
        assert exc.value.status_code == 400
        assert "الحالية" in exc.value.detail

    def test_wrong_current_password_is_refused(self):
        user = _user()
        with pytest.raises(HTTPException) as exc:
            self._run(user, UserUpdate(password=NEW, current_password="WrongPass9!"))
        assert exc.value.status_code == 400

    def test_reusing_the_current_password_is_refused(self):
        user = _user()
        with pytest.raises(HTTPException) as exc:
            self._run(user, UserUpdate(password=CURRENT, current_password=CURRENT))
        assert exc.value.status_code == 400

    def test_weak_new_password_is_refused_even_with_the_current_one(self):
        user = _user()
        with pytest.raises(HTTPException) as exc:
            self._run(user, UserUpdate(password="weak", current_password=CURRENT))
        assert exc.value.status_code == 400

    def test_correct_current_password_changes_it_and_revokes_sessions(self):
        user = _user()
        result, inv, original_hash, db = self._run(
            user, UserUpdate(password=NEW, current_password=CURRENT)
        )
        assert result.hashed_password != original_hash
        assert verify_password(NEW, result.hashed_password)
        inv.assert_awaited_once_with(user.id)
        db.commit.assert_called_once_with()

    def test_name_only_update_needs_no_current_password(self):
        user = _user()
        result, inv, original_hash, db = self._run(user, UserUpdate(full_name="New Name"))
        assert result.full_name == "New Name"
        assert result.hashed_password == original_hash
        inv.assert_not_awaited()

    def test_privilege_fields_are_not_part_of_the_request_model(self):
        # is_admin / is_approved / is_locked cannot even be sent through
        # UserUpdate; the handler strips them again as defense in depth.
        assert {"is_admin", "is_approved", "is_locked"}.isdisjoint(set(UserUpdate.model_fields))


# ─────────────────────────────────────────────────────────────────────────────
# H-2 — the rate limits that protect the auth surface are really wired
# ─────────────────────────────────────────────────────────────────────────────

RATE_LIMITED_AUTH_ENDPOINTS = {
    "login", "register", "refresh_token_endpoint", "forget_password",
    "reset_password", "verify_email", "oauth_confirm_link", "google_callback",
    "facebook_callback", "update_profile", "delete_account", "activate_session",
    "logout_all", "pending_status_check", "pending_status_stream",
}


class TestRateLimitWiring:
    def _limited_keys(self) -> set[str]:
        return set(limiter._route_limits)

    def test_sensitive_auth_endpoints_are_rate_limited(self):
        missing = sorted(
            name for name in RATE_LIMITED_AUTH_ENDPOINTS
            if f"app.api.routes.auth.{name}" not in self._limited_keys()
        )
        assert not missing, f"auth endpoints without @limiter.limit: {missing}"

    def test_every_registered_limit_is_non_empty(self):
        empty = sorted(key for key, value in limiter._route_limits.items() if not value)
        assert not empty, f"limits registered but empty: {empty}"

    def test_app_state_exposes_the_limiter(self):
        """Without this the decorators silently do nothing."""
        from app.main import app

        assert app.state.limiter is limiter

    def test_limiter_keys_on_the_client_address(self):
        from slowapi.util import get_remote_address

        # slowapi stores the injected keyfunc on the private attribute.
        assert limiter._key_func is get_remote_address

    def test_no_blanket_default_limit_is_configured(self):
        """A global default would cap the data-heavy pages; enforcement is
        deliberately per-route (see app/core/limiter.py)."""
        assert not limiter._application_limits


class TestPasswordLengthCap:
    """Argon2 at 64 MiB / t=3 makes an unbounded password a CPU DoS."""

    @pytest.mark.parametrize(
        "schema,field",
        [(UserLogin, "password"), (UserUpdate, "password"), (UserUpdate, "current_password")],
    )
    def test_absurd_password_is_rejected_before_hashing(self, schema, field):
        payload = {"email": "user@rebh.ai", field: "x" * (MAX_PASSWORD_LENGTH + 1)}
        with pytest.raises(ValidationError):
            schema(**payload)

    def test_realistic_passphrase_still_passes(self):
        login = UserLogin(email="user@rebh.ai", password="correc-horse-battery-staple-9!")
        assert len(login.password) <= MAX_PASSWORD_LENGTH
