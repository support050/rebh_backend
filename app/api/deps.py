"""FastAPI auth dependencies — clean DI, no double-decode, strict token types."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.auth import parse_user_id, verify_token
from app.core.database import get_db
from app.models.user import User


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

    if getattr(user, "is_locked", False):
        # Locked accounts cannot use the API even with a still-valid JWT
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


def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db)
) -> Optional[User]:
    """
    Returns the authenticated User if valid token present, otherwise None.
    Does not raise 401, permitting guest mode with fallback.
    """
    auth_header = request.headers.get("Authorization")
    cookie_token = request.cookies.get("access_token")
    raw_token = None
    if auth_header and auth_header.startswith("Bearer "):
        raw_token = auth_header.split(" ", 1)[1].strip()
    elif cookie_token:
        raw_token = cookie_token.strip()

    if not raw_token:
        return None

    try:
        from app.core.auth import decode_token
        payload = decode_token(raw_token)
        user_id = parse_user_id(payload)
        if user_id:
            user = db.query(User).filter(User.id == user_id).first()
            if user and not getattr(user, "is_locked", False):
                return user
    except Exception:
        pass
    return None
