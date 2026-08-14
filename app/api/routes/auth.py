"""Authentication routes — hardened login, refresh rotation, OAuth, pending SSE."""

import asyncio
import base64
import hashlib
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user as require_current_user
from app.core.auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
    burn_password_hash_cpu,
    consume_refresh_token,
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_token,
    get_password_hash,
    hash_token,
    invalidate_all_sessions,
    invalidate_refresh_token,
    invalidate_token,
    parse_user_id,
    service_unavailable,
    store_refresh_token_in_redis,
    store_token_in_redis,
    verify_password,
    verify_token,
    verify_token_exists,
)
from app.core.config import settings
from app.core.database import SessionLocal, get_db
from app.core.limiter import limiter
from app.core.redis import RedisUnavailableError, redis_cache
from app.core.redis import (
    delete_verification_token,
    get_verification_token,
    store_verification_token,
)
from app.models.user import User
from app.services.user_cleanup import delete_user_related_data
from app.schemas.auth import (
    ForgetPasswordRequest,
    OAuthConfirmLinkRequest,
    ResetPasswordRequest,
    UserLogin,
    UserRegister,
    UserResponse,
    UserUpdate,
)
from app.services.email_service import send_email

router = APIRouter(prefix="/auth", tags=["authentication"])
logger = logging.getLogger(__name__)

GENERIC_AUTH_ERROR = "بيانات الدخول غير صحيحة"
GENERIC_RESET_MESSAGE = "إذا كان البريد مسجلاً، سيتم إرسال رابط الاستعادة."
UNIFIED_REGISTER_MESSAGE = (
    "تم استلام الطلب. إذا لم يكن البريد مسجلاً مسبقاً، ستتلقى رسالة لتأكيد البريد الإلكتروني."
)


def validate_password_strength(password: str):
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="كلمة المرور يجب أن تكون 8 أحرف على الأقل")
    if not re.search(r"[A-Z]", password):
        raise HTTPException(status_code=400, detail="كلمة المرور يجب أن تحتوي على حرف كبير واحد على الأقل")
    if not re.search(r"[a-z]", password):
        raise HTTPException(status_code=400, detail="كلمة المرور يجب أن تحتوي على حرف صغير واحد على الأقل")
    if not re.search(r"\d", password):
        raise HTTPException(status_code=400, detail="كلمة المرور يجب أن تحتوي على رقم واحد على الأقل")
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
        raise HTTPException(status_code=400, detail="كلمة المرور يجب أن تحتوي على رمز خاص واحد على الأقل")


def _cookie_secure() -> bool:
    return settings.ENVIRONMENT == "production"


def set_csrf_cookie(response: Response, token: str | None = None) -> str:
    """Non-HttpOnly CSRF cookie for double-submit validation."""
    value = token or secrets.token_urlsafe(32)
    response.set_cookie(
        key="csrf_token",
        value=value,
        httponly=False,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        path="/",
    )
    return value


def set_auth_cookies(response: Response, access_token: str, refresh_token: str):
    is_secure = _cookie_secure()
    response.set_cookie(
        key="session_token",
        value=access_token,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        path="/",
    )
    set_csrf_cookie(response)


def clear_auth_cookies(response: Response):
    is_secure = _cookie_secure()
    for name in ("session_token", "refresh_token", "pending_token"):
        response.delete_cookie(name, path="/", secure=is_secure, httponly=True, samesite="lax")
    response.delete_cookie("csrf_token", path="/", secure=is_secure, httponly=False, samesite="lax")


def set_pending_cookie(response: Response, pending_token: str):
    response.set_cookie(
        key="pending_token",
        value=pending_token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )
    set_csrf_cookie(response)


async def create_and_store_tokens(user: User):
    try:
        access_token = create_access_token(
            data={
                "sub": str(user.id),
                "email": user.email,
                "is_approved": user.is_approved,
                "is_admin": user.is_admin,
            }
        )
        refresh_token = create_refresh_token(data={"sub": str(user.id), "email": user.email})
        await store_token_in_redis(user.id, access_token)
        await store_refresh_token_in_redis(user.id, refresh_token)
        return access_token, refresh_token
    except RedisUnavailableError:
        raise service_unavailable()


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


async def create_oauth_state(provider: str):
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    try:
        await redis_cache.auth_set(f"oauth_state:{provider}:{state}", verifier, expire=600)
    except RedisUnavailableError:
        ok = await redis_cache.set(f"oauth_state:{provider}:{state}", verifier, expire=600)
        if not ok:
            raise service_unavailable()
    return state, verifier


async def consume_oauth_state(provider: str, state: str):
    """Atomic one-time consume of OAuth state (replay protection)."""
    key = f"oauth_state:{provider}:{state}"
    try:
        verifier = await redis_cache.auth_getdel(key)
        return verifier
    except RedisUnavailableError:
        raise service_unavailable()


async def _store_oauth_link_challenge(provider: str, provider_subject: str, email: str, name: str | None) -> str:
    """Store a short-lived challenge requiring password proof to link OAuth to an existing account."""
    link_token = secrets.token_urlsafe(32)
    # Single string value (auth_set JSON-encodes strings safely)
    value = f"{provider}\x1f{provider_subject}\x1f{email}\x1f{name or ''}"
    try:
        await redis_cache.auth_set(f"oauth_link:{link_token}", value, expire=600)
    except RedisUnavailableError:
        raise service_unavailable()
    return link_token


async def _resolve_oauth_login(
    db: Session,
    *,
    provider: str,
    provider_subject: str,
    email: str,
    name: str | None,
):
    """
    Resolve OAuth identity to a User or a link challenge.

    Returns (user, None) on success, or (None, link_token) when password confirmation is required.
    Never auto-attaches OAuth to a verified password account by email alone.
    """
    subject_field = "google_sub" if provider == "google" else "facebook_id"

    user = db.query(User).filter(getattr(User, subject_field) == provider_subject).first()
    if user:
        return user, None

    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(
            email=email,
            hashed_password=get_password_hash(secrets.token_urlsafe(32)),
            full_name=name,
            is_verified=True,
            is_approved=False,
        )
        setattr(user, subject_field, provider_subject)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user, None

    existing_sub = getattr(user, subject_field, None)
    if existing_sub and existing_sub != provider_subject:
        raise HTTPException(status_code=400, detail="This email is linked to a different OAuth identity")

    if existing_sub == provider_subject:
        return user, None

    # Existing password account (verified or not): require password proof to link.
    # Never auto-claim by email alone.
    link_token = await _store_oauth_link_challenge(provider, provider_subject, email, name)
    return None, link_token


def _account_is_temporarily_locked(user: User) -> bool:
    if user.locked_until and user.locked_until > datetime.utcnow():
        return True
    if user.is_locked and user.locked_until and user.locked_until <= datetime.utcnow():
        # Auto-unlock expired lockouts
        user.is_locked = False
        user.locked_until = None
        user.failed_login_attempts = 0
        return False
    if user.is_locked and not user.locked_until:
        # Legacy permanent lock — treat as locked (admin must unlock)
        return True
    return False


async def _register_failed_login(db: Session, user: User) -> None:
    """Increment failure counter and apply temporary lockout. Uses row lock."""
    locked_user = db.query(User).filter(User.id == user.id).with_for_update().first()
    if not locked_user:
        return
    locked_user.failed_login_attempts = (locked_user.failed_login_attempts or 0) + 1
    just_locked = False
    if locked_user.failed_login_attempts >= settings.MAX_FAILED_LOGIN_ATTEMPTS:
        locked_user.is_locked = True
        locked_user.locked_until = datetime.utcnow() + timedelta(
            minutes=settings.LOCKOUT_DURATION_MINUTES
        )
        just_locked = True
        logger.warning("Account temporarily locked after failed logins user_id=%s", locked_user.id)
    db.commit()
    if just_locked:
        try:
            await invalidate_all_sessions(locked_user.id)
        except RedisUnavailableError:
            raise service_unavailable()


def _reset_failed_login(db: Session, user: User) -> None:
    if user.failed_login_attempts or user.is_locked or user.locked_until:
        locked_user = db.query(User).filter(User.id == user.id).with_for_update().first()
        if locked_user:
            locked_user.failed_login_attempts = 0
            locked_user.is_locked = False
            locked_user.locked_until = None
            db.commit()


async def _issue_pending_response(user: User) -> JSONResponse:
    try:
        temp_token = create_access_token(
            data={
                "sub": str(user.id),
                "email": user.email,
                "is_approved": False,
                "is_admin": False,
                "scope": "check_approval_only",
            }
        )
        await store_token_in_redis(user.id, temp_token)
    except RedisUnavailableError:
        raise service_unavailable()

    pending_response = JSONResponse(
        status_code=403,
        content={
            "detail": "الحساب بانتظار موافقة الإدارة. سيتم إشعارك عند التفعيل.",
            "user": {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
                "is_approved": False,
            },
        },
    )
    set_pending_cookie(pending_response, temp_token)
    return pending_response


@router.get("/csrf")
async def get_csrf(response: Response):
    """Issue / rotate CSRF cookie for double-submit protection."""
    token = set_csrf_cookie(response)
    return {"csrf_token": token}


@router.post("/register", status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def register(
    request: Request,
    user: UserRegister,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    validate_password_strength(user.password)

    # Identical response for existing and new emails (anti-enumeration).
    # No pending_token cookie here — cookie presence would leak account existence.
    identical_body = {
        "message": UNIFIED_REGISTER_MESSAGE,
        "user": {
            "id": 0,
            "email": user.email,
            "full_name": user.full_name,
            "is_approved": False,
        },
    }

    db_user = db.query(User).filter(User.email == user.email).first()
    if db_user:
        burn_password_hash_cpu(user.password)
        return JSONResponse(status_code=201, content=identical_body)

    hashed_password = get_password_hash(user.password)
    db_user = User(
        email=user.email,
        hashed_password=hashed_password,
        full_name=user.full_name,
        is_verified=False,
        is_approved=False,
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)

    try:
        verification_token = generate_token()
        await store_verification_token(
            db_user.id,
            verification_token,
            expire_minutes=settings.VERIFICATION_TOKEN_EXPIRE_MINUTES,
        )
        verification_link = f"{settings.FRONTEND_URL}/auth/verify-email?token={verification_token}"
        email_body = f"""
        <h1>مرحباً {db_user.full_name or ''}</h1>
        <p>شكراً لتسجيلك. يرجى تأكيد بريدك الإلكتروني بالضغط على الرابط أدناه:</p>
        <a href="{verification_link}">تأكيد البريد الإلكتروني</a>
        """
        background_tasks.add_task(
            send_email,
            db_user.email,
            "تأكيد البريد الإلكتروني - LUMIVST",
            email_body,
            user_id=db_user.id,
            purpose="verification",
        )
    except Exception as e:
        logger.warning(
            "Failed to queue verification email for user_id=%s: %s",
            db_user.id,
            type(e).__name__,
        )

    return JSONResponse(status_code=201, content=identical_body)


@router.post("/refresh-token")
@limiter.limit("30/minute")
async def refresh_token_endpoint(request: Request, response: Response, db: Session = Depends(get_db)):
    raw_refresh = request.cookies.get("refresh_token")
    if not raw_refresh:
        raise HTTPException(status_code=401, detail="Refresh token missing")

    payload = decode_token(raw_refresh)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token type")

    user_id = parse_user_id(payload)

    # Atomic consume — concurrent refresh of same token: only one wins
    await consume_refresh_token(user_id, raw_refresh)

    user = db.query(User).filter(User.id == user_id).first()
    if not user or _account_is_temporarily_locked(user):
        raise HTTPException(status_code=401, detail="Refresh token expired or revoked")
    if not user.is_approved:
        raise HTTPException(status_code=403, detail="Account pending admin approval")

    access_token, new_refresh_token = await create_and_store_tokens(user)
    set_auth_cookies(response, access_token, new_refresh_token)

    return {
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "is_verified": user.is_verified,
            "is_approved": user.is_approved,
            "is_admin": user.is_admin,
        },
    }


@router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, response: Response, user: UserLogin, db: Session = Depends(get_db)):
    try:
        db_user = db.query(User).filter(User.email == user.email).first()

        if not db_user:
            # Burn verify-equivalent work against dummy to reduce timing oracle
            burn_password_hash_cpu(user.password)
            raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR)

        if _account_is_temporarily_locked(db_user):
            db.commit()  # persist auto-unlock if any
            # Same error as wrong password — no lock enumeration
            burn_password_hash_cpu(user.password)
            raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR)

        if not verify_password(user.password, db_user.hashed_password):
            await _register_failed_login(db, db_user)
            raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR)

        _reset_failed_login(db, db_user)

        if not db_user.is_approved:
            return await _issue_pending_response(db_user)

        access_token, refresh_token = await create_and_store_tokens(db_user)
        set_auth_cookies(response, access_token, refresh_token)

        return {
            "token_type": "bearer",
            "user": {
                "id": db_user.id,
                "email": db_user.email,
                "full_name": db_user.full_name,
                "is_verified": db_user.is_verified,
                "is_approved": db_user.is_approved,
                "is_admin": db_user.is_admin,
            },
        }
    except HTTPException:
        raise
    except RedisUnavailableError:
        raise service_unavailable()
    except Exception:
        logger.exception("Unexpected login error")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    token: str = Depends(verify_token),
    current_user: User = Depends(require_current_user),
):
    payload = getattr(request.state, "token_payload", None) or decode_token(token)
    access_jti = payload.get("jti")
    try:
        if access_jti:
            await invalidate_token(current_user.id, access_jti)
        else:
            await invalidate_token(current_user.id)

        raw_refresh = request.cookies.get("refresh_token")
        if raw_refresh:
            try:
                refresh_payload = decode_token(raw_refresh)
                refresh_jti = refresh_payload.get("jti")
                if refresh_jti:
                    await invalidate_refresh_token(current_user.id, refresh_jti)
            except HTTPException:
                pass
    except RedisUnavailableError:
        # Do not clear cookies or claim success — revocation did not complete
        raise service_unavailable()

    clear_auth_cookies(response)
    return {"message": "تم تسجيل الخروج بنجاح"}


@router.post("/logout-all")
async def logout_all(
    response: Response,
    current_user: User = Depends(require_current_user),
):
    try:
        await invalidate_all_sessions(current_user.id)
    except RedisUnavailableError:
        raise service_unavailable()
    clear_auth_cookies(response)
    return {"message": "تم تسجيل الخروج من جميع الجلسات"}


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(require_current_user)):
    return current_user


@router.put("/profile", response_model=UserResponse)
async def update_profile(
    user_update: UserUpdate,
    current_user: User = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    db_user = db.query(User).filter(User.id == current_user.id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")

    # Never allow privilege escalation via profile update
    update_data = user_update.model_dump(exclude_unset=True)
    for forbidden in ("is_admin", "is_approved", "is_verified", "is_locked", "failed_login_attempts"):
        update_data.pop(forbidden, None)

    if user_update.full_name is not None:
        db_user.full_name = user_update.full_name

    if user_update.email and user_update.email != db_user.email:
        if not user_update.current_password:
            raise HTTPException(status_code=400, detail="يجب إدخال كلمة المرور الحالية لتغيير البريد الإلكتروني")
        if not verify_password(user_update.current_password, db_user.hashed_password):
            raise HTTPException(status_code=400, detail="كلمة المرور الحالية غير صحيحة")

        existing_user = db.query(User).filter(User.email == user_update.email).first()
        if existing_user and existing_user.id != current_user.id:
            raise HTTPException(status_code=400, detail="البريد الإلكتروني مستخدم بالفعل")
        db_user.email = user_update.email
        db_user.is_verified = False

    if user_update.password:
        validate_password_strength(user_update.password)
        try:
            await invalidate_all_sessions(db_user.id)
        except RedisUnavailableError:
            raise service_unavailable()
        db_user.hashed_password = get_password_hash(user_update.password)

    db.commit()
    db.refresh(db_user)
    return db_user


@router.delete("/delete-account")
async def delete_account(
    response: Response,
    current_user: User = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    db_user = db.query(User).filter(User.id == current_user.id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")

    if db_user.is_admin:
        admin_count = db.query(User).filter(User.is_admin == True).count()  # noqa: E712
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="لا يمكن حذف آخر حساب مدير")

    # Invalidate sessions first
    await invalidate_all_sessions(current_user.id)
    clear_auth_cookies(response)

    try:
        delete_user_related_data(db, current_user.id)
        db.delete(db_user)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"message": "تم حذف الحساب بنجاح"}


@router.post("/forget-password")
@limiter.limit("3/minute")
async def forget_password(
    request: Request,
    payload: ForgetPasswordRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user:
        # Timing: still burn some work
        burn_password_hash_cpu("dummy-reset-timing")
        return {"message": GENERIC_RESET_MESSAGE}

    raw_token = generate_token()
    token_hash = hash_token(raw_token)
    user.reset_token_hash = token_hash
    user.reset_token_expires_at = datetime.utcnow() + timedelta(minutes=settings.RESET_TOKEN_EXPIRE_MINUTES)
    db.commit()

    reset_link = f"{settings.FRONTEND_URL}/auth/reset-password?token={raw_token}"
    email_body = f"""
    <h1>استعادة كلمة المرور</h1>
    <p>لقد طلبت استعادة كلمة المرور لحسابك.</p>
    <a href="{reset_link}">تغيير كلمة المرور</a>
    <p>هذا الرابط صالح لمدة {settings.RESET_TOKEN_EXPIRE_MINUTES} دقيقة.</p>
    """
    background_tasks.add_task(
        send_email,
        user.email,
        "استعادة كلمة المرور - LUMIVST",
        email_body,
        user_id=user.id,
        purpose="password_reset",
    )
    return {"message": GENERIC_RESET_MESSAGE}


@router.post("/reset-password")
@limiter.limit("3/minute")
async def reset_password(request: Request, payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    validate_password_strength(payload.password)

    incoming_token_hash = hash_token(payload.token)
    user = (
        db.query(User)
        .filter(User.reset_token_hash == incoming_token_hash)
        .with_for_update()
        .first()
    )

    if not user:
        raise HTTPException(status_code=400, detail="الرابط غير صالح أو منتهي")

    if not user.reset_token_expires_at or user.reset_token_expires_at < datetime.utcnow():
        user.reset_token_hash = None
        user.reset_token_expires_at = None
        db.commit()
        raise HTTPException(status_code=400, detail="انتهت صلاحية الرابط")

    # Revoke sessions BEFORE committing password change — fail closed if Redis is down
    try:
        await invalidate_all_sessions(user.id)
    except RedisUnavailableError:
        raise service_unavailable()

    user.hashed_password = get_password_hash(payload.password)
    user.reset_token_hash = None
    user.reset_token_expires_at = None
    user.failed_login_attempts = 0
    user.is_locked = False
    user.locked_until = None
    db.commit()

    return {"message": "تم تغيير كلمة المرور بنجاح"}


@router.post("/activate-session")
async def activate_session(request: Request, db: Session = Depends(get_db)):
    """Convert pending_token into a full session after admin approval."""
    pending_token = request.cookies.get("pending_token")
    if not pending_token:
        raise HTTPException(status_code=401, detail="Pending token missing")

    payload = decode_token(pending_token)
    if payload.get("scope") != "check_approval_only":
        raise HTTPException(status_code=401, detail="Invalid token scope")
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")

    user_id = parse_user_id(payload)
    try:
        if not await verify_token_exists(user_id, pending_token):
            raise HTTPException(status_code=401, detail="Pending token expired")
    except RedisUnavailableError:
        raise service_unavailable()

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_approved:
        raise HTTPException(status_code=403, detail="Account not yet approved")

    access_token, refresh_token = await create_and_store_tokens(user)

    pending_jti = payload.get("jti")
    if pending_jti:
        await invalidate_token(user_id, pending_jti)

    response = JSONResponse(
        content={
            "token_type": "bearer",
            "user": {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
                "is_verified": user.is_verified,
                "is_approved": user.is_approved,
                "is_admin": user.is_admin,
            },
        }
    )
    set_auth_cookies(response, access_token, refresh_token)
    response.delete_cookie(
        "pending_token",
        path="/",
        secure=_cookie_secure(),
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/pending-status/check")
async def pending_status_check(request: Request):
    """Polling endpoint — short-lived DB session only. Requires valid Redis JTI."""
    pending_token = request.cookies.get("pending_token")
    if not pending_token:
        raise HTTPException(status_code=401, detail="Pending token missing")

    payload = decode_token(pending_token)
    if payload.get("scope") != "check_approval_only":
        raise HTTPException(status_code=401, detail="Invalid pending token scope")

    user_id = parse_user_id(payload)
    try:
        if not await verify_token_exists(user_id, pending_token):
            raise HTTPException(status_code=401, detail="Pending token expired")
    except RedisUnavailableError:
        raise service_unavailable()

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        approved = bool(user and user.is_approved)
    finally:
        db.close()
    return {"approved": approved}


@router.get("/pending-status/stream")
async def pending_status_stream(request: Request):
    """
    SSE approval notifications.

    DB session is NOT held open for the stream lifetime:
    1) Validate token + initial approval check with a short-lived session
    2) Close DB
    3) Subscribe to Redis pub/sub (or poll with short-lived sessions)
    """
    pending_token = request.cookies.get("pending_token")
    if not pending_token:
        raise HTTPException(status_code=401, detail="Pending token missing")

    payload = decode_token(pending_token)
    if payload.get("scope") != "check_approval_only":
        raise HTTPException(status_code=401, detail="Invalid pending token scope")

    user_id = parse_user_id(payload)
    try:
        if not await verify_token_exists(user_id, pending_token):
            raise HTTPException(status_code=401, detail="Pending token expired")
    except RedisUnavailableError:
        raise service_unavailable()

    # Initial DB check — close immediately
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        initially_approved = bool(user and user.is_approved)
    finally:
        db.close()

    async def event_generator():
        yield f'data: {{"approved": {str(initially_approved).lower()}}}\n\n'
        if initially_approved:
            return

        pubsub = await redis_cache.pubsub()
        if not pubsub:
            # Fallback polling with short-lived sessions
            while True:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(10)
                poll_db = SessionLocal()
                try:
                    u = poll_db.query(User).filter(User.id == user_id).first()
                    approved = bool(u and u.is_approved)
                finally:
                    poll_db.close()
                yield f'data: {{"approved": {str(approved).lower()}}}\n\n'
                if approved:
                    break
            return

        channel_name = f"user_approval_{user_id}"
        try:
            await pubsub.subscribe(channel_name)
            async for message in pubsub.listen():
                if await request.is_disconnected():
                    break
                if message.get("type") == "message" and message.get("data") == "approved":
                    yield 'data: {"approved": true}\n\n'
                    break
        finally:
            try:
                await pubsub.unsubscribe(channel_name)
                await pubsub.close()
            except Exception:
                pass

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/verify-email")
@limiter.limit("5/minute")
async def verify_email(request: Request, token: str, db: Session = Depends(get_db)):
    user_id = await get_verification_token(token)
    if not user_id:
        raise HTTPException(status_code=400, detail="توكن غير صالح أو منتهي")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        await delete_verification_token(token)
        raise HTTPException(status_code=400, detail="توكن غير صالح أو منتهي")

    user.is_verified = True
    # Email verification must NOT grant admin approval
    db.commit()
    await delete_verification_token(token)
    return {"message": "تم التحقق من البريد الإلكتروني بنجاح"}


# ── Social Login - Google ────────────────────────────────────────────────

@router.post("/oauth/confirm-link")
@limiter.limit("5/minute")
async def oauth_confirm_link(
    request: Request,
    payload: OAuthConfirmLinkRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    """
    Complete OAuth linking for an existing verified password account.
    Requires the short-lived link_token from a 409 account_link_required response
    plus the account password (proof of control).
    """
    try:
        raw = await redis_cache.auth_getdel(f"oauth_link:{payload.link_token}")
    except RedisUnavailableError:
        raise service_unavailable()

    if not raw:
        raise HTTPException(status_code=400, detail="Invalid or expired link token")

    parts = str(raw).split("\x1f")
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="Invalid or expired link token")
    provider, provider_subject, email, name = parts

    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR)

    if _account_is_temporarily_locked(user):
        db.commit()
        raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR)

    subject_field = "google_sub" if provider == "google" else "facebook_id"
    existing_sub = getattr(user, subject_field, None)
    if existing_sub and existing_sub != provider_subject:
        raise HTTPException(status_code=400, detail="This email is linked to a different OAuth identity")

    setattr(user, subject_field, provider_subject)
    user.is_verified = True
    if name and not user.full_name:
        user.full_name = name
    db.commit()
    db.refresh(user)

    _reset_failed_login(db, user)

    if not user.is_approved:
        return await _issue_pending_response(user)

    access_token, refresh_token = await create_and_store_tokens(user)
    set_auth_cookies(response, access_token, refresh_token)
    return {
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "is_verified": user.is_verified,
            "is_approved": user.is_approved,
            "is_admin": user.is_admin,
        },
    }


@router.get("/google/login")
async def google_login():
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Google OAuth configuration is missing")
    redirect_uri = f"{settings.FRONTEND_URL}/auth/callback/google"
    state, verifier = await create_oauth_state("google")
    challenge = _pkce_challenge(verifier)
    params = {
        "response_type": "code",
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return {"url": f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"}


@router.post("/google/callback")
@limiter.limit("5/minute")
async def google_callback(
    request: Request, code: str, state: str, response: Response, db: Session = Depends(get_db)
):
    try:
        verifier = await consume_oauth_state("google", state)
        if not verifier:
            raise HTTPException(status_code=400, detail="Invalid OAuth state")

        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
            raise HTTPException(status_code=500, detail="Google OAuth configuration is missing")

        redirect_uri = f"{settings.FRONTEND_URL}/auth/callback/google"
        data = {
            "code": code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            token_response = await client.post("https://oauth2.googleapis.com/token", data=data)
            token_data = token_response.json()

        if "error" in token_data:
            raise HTTPException(status_code=400, detail="Google Login Failed")

        access_token_from_google = token_data.get("access_token")
        if not access_token_from_google:
            raise HTTPException(status_code=400, detail="لم يتم الحصول على رمز الولوج من Google")

        async with httpx.AsyncClient(timeout=30.0) as client:
            userinfo_response = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token_from_google}"},
            )
            user_info = userinfo_response.json()

        if userinfo_response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get user info from Google")

        email = user_info.get("email")
        name = user_info.get("name")
        google_sub = user_info.get("sub")
        if not email:
            raise HTTPException(status_code=400, detail="لم تقدم Google بريدك الإلكتروني")
        if not google_sub:
            raise HTTPException(status_code=400, detail="Google identity subject missing")

        user, link_token = await _resolve_oauth_login(
            db,
            provider="google",
            provider_subject=google_sub,
            email=email,
            name=name,
        )
        if link_token:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "account_link_required",
                    "message": "An account with this email already exists. Confirm your password to link Google.",
                    "link_token": link_token,
                    "email": email,
                    "provider": "google",
                },
            )

        if _account_is_temporarily_locked(user):
            db.commit()
            raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR)

        if not user.is_approved:
            return await _issue_pending_response(user)

        access_token, refresh_token = await create_and_store_tokens(user)
        set_auth_cookies(response, access_token, refresh_token)
        return {
            "token_type": "bearer",
            "user": {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
                "is_verified": user.is_verified,
                "is_approved": user.is_approved,
                "is_admin": user.is_admin,
            },
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Google callback failed")
        raise HTTPException(status_code=500, detail="Google login failed")


# ── Social Login - Facebook ──────────────────────────────────────────────

@router.get("/facebook/login")
async def facebook_login():
    if not settings.FACEBOOK_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Facebook OAuth configuration is missing")
    redirect_uri = f"{settings.FRONTEND_URL}/auth/callback/facebook"
    state, _ = await create_oauth_state("facebook")
    params = {
        "client_id": settings.FACEBOOK_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "public_profile,email",
        "state": state,
    }
    return {"url": f"https://www.facebook.com/v18.0/dialog/oauth?{urlencode(params)}"}


@router.post("/facebook/callback")
@limiter.limit("5/minute")
async def facebook_callback(
    request: Request, code: str, state: str, response: Response, db: Session = Depends(get_db)
):
    try:
        verifier = await consume_oauth_state("facebook", state)
        if not verifier:
            raise HTTPException(status_code=400, detail="Invalid OAuth state")

        if not settings.FACEBOOK_CLIENT_ID or not settings.FACEBOOK_CLIENT_SECRET:
            raise HTTPException(status_code=500, detail="Facebook OAuth configuration is missing")

        redirect_uri = f"{settings.FRONTEND_URL}/auth/callback/facebook"
        params = {
            "client_id": settings.FACEBOOK_CLIENT_ID,
            "client_secret": settings.FACEBOOK_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "code": code,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            token_response = await client.get(
                "https://graph.facebook.com/v18.0/oauth/access_token", params=params
            )
            token_data = token_response.json()

        if "error" in token_data:
            raise HTTPException(status_code=400, detail="Facebook Login Failed")

        access_token_from_facebook = token_data.get("access_token")
        if not access_token_from_facebook:
            raise HTTPException(status_code=400, detail="لم يتم الحصول على رمز الولوج من Facebook")

        async with httpx.AsyncClient(timeout=30.0) as client:
            userinfo_response = await client.get(
                "https://graph.facebook.com/me",
                params={
                    "fields": "id,name,email",
                    "access_token": access_token_from_facebook,
                },
            )
            user_info = userinfo_response.json()

        if "error" in user_info:
            raise HTTPException(status_code=400, detail="Failed to get user info from Facebook")

        email = user_info.get("email")
        name = user_info.get("name")
        facebook_id = user_info.get("id")
        if not email:
            raise HTTPException(status_code=400, detail="لم تقدم Facebook بريدك الإلكتروني")
        if not facebook_id:
            raise HTTPException(status_code=400, detail="Facebook identity missing")

        user, link_token = await _resolve_oauth_login(
            db,
            provider="facebook",
            provider_subject=str(facebook_id),
            email=email,
            name=name,
        )
        if link_token:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "account_link_required",
                    "message": "An account with this email already exists. Confirm your password to link Facebook.",
                    "link_token": link_token,
                    "email": email,
                    "provider": "facebook",
                },
            )

        if _account_is_temporarily_locked(user):
            db.commit()
            raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR)

        if not user.is_approved:
            return await _issue_pending_response(user)

        jwt_token, refresh_token = await create_and_store_tokens(user)
        set_auth_cookies(response, jwt_token, refresh_token)
        return {
            "token_type": "bearer",
            "user": {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
                "is_verified": user.is_verified,
                "is_approved": user.is_approved,
                "is_admin": user.is_admin,
            },
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Facebook callback failed")
        raise HTTPException(status_code=500, detail="Facebook login failed")
