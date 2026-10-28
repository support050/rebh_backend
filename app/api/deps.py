"""FastAPI auth dependencies — clean DI, no double-decode, strict token types."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.auth import (
    RedisUnavailableError,
    decode_token,
    parse_user_id,
    verify_token,
    verify_token_exists,
)
from app.core.database import get_db
from app.models.user import User
from app.services.account_lock import resolve_lockout


def get_token_payload(request: Request, token: str = Depends(verify_token)) -> dict:
    """Return already-decoded payload from verify_token (attached to request.state)."""
    payload = getattr(request.state, "token_payload", None)
    if not isinstance(payload, dict):
        # Should not happen if verify_token ran; fail closed.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="توكن غير صالح",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Ensure token dependency runs (and is not optimized away)
    _ = token
    return payload


def get_current_user(
    request: Request,
    token: str = Depends(verify_token),
    db: Session = Depends(get_db),
) -> User:
    payload = getattr(request.state, "token_payload", None)
    if isinstance(payload, dict):
        user_id = getattr(request.state, "token_user_id", None) or parse_user_id(payload)
    else:
        # Fallback should be unreachable; keep type-safe failure
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="توكن غير صالح",
            headers={"WWW-Authenticate": "Bearer"},
        )

    _ = token  # dependency already validated

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # A lock blocks the API even with a still-valid JWT — but an EXPIRED lockout
    # is cleared and committed here first, otherwise this hot path would 401 a
    # user forever while login/refresh believed them unlocked.
    if resolve_lockout(db, user):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="بيانات الدخول غير صحيحة",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account pending admin approval",
        )

    return user


def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The user doesn't have enough privileges",
        )
    return current_user


async def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db)
) -> Optional[User]:
    """
    Returns the authenticated User if a usable token is present, otherwise None.
    Does not raise 401, permitting guest mode with fallback.

    "Optional" loosens the RESPONSE (401 -> None), never the IDENTITY CHECKS: every
    guarantee of get_current_user still applies here — access-type tokens only, the
    pending `check_approval_only` token rejected, the Redis JTI allowlist enforced
    (so logged-out / revoked sessions stop resolving), and both lock and approval
    state honoured. Anything unverifiable yields None, i.e. a guest.

    Deliberately fail-closed on Redis outage (-> guest) rather than 503: these
    endpoints serve optional personalisation on top of public data, so degrading
    to anonymous keeps the page working. Write paths still answer 401.
    """
    auth_header = request.headers.get("Authorization")
    # Cookie name must match what verify_token reads and set_auth_cookies writes:
    # the access JWT lives in `session_token` (there has never been an `access_token`
    # cookie, so reading one silently disabled cookie-auth for every guest-fallback route).
    cookie_token = request.cookies.get("session_token")
    raw_token = None
    if auth_header and auth_header.startswith("Bearer "):
        raw_token = auth_header.split(" ", 1)[1].strip()
    elif cookie_token:
        raw_token = cookie_token.strip()

    if not raw_token:
        return None

    try:
        payload = decode_token(raw_token)
        if payload.get("type") != "access":
            return None
        if payload.get("scope") == "check_approval_only":
            # Pending-approval token: identity, but no account access yet.
            return None
        user_id = parse_user_id(payload)
        if not await verify_token_exists(user_id, raw_token):
            return None
        user = db.query(User).filter(User.id == user_id).first()
    except Exception:  # noqa: BLE001 — covers unauthorized() (401), the
                       # RedisUnavailableError raised by the JTI allowlist, and the
                       # 401 HTTPException from decode/parse. Guest fallback must
                       # never surface as a 500.
        return None

    if not user or resolve_lockout(db, user) or not user.is_approved:
        return None
    return user
