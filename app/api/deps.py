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
