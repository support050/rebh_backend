from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_admin
from app.core.auth import invalidate_all_sessions
from app.core.database import get_db
from app.core.limiter import limiter
from app.core.redis import redis_cache
from app.models.user import User
from app.schemas.auth import UserResponse

router = APIRouter(prefix="/admin", tags=["Admin"])


@router.get("/users", response_model=List[UserResponse])
async def list_users(
    skip: int = 0,
    limit: int = 100,
    approved: bool = None,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """عرض قائمة المستخدمين (للمدير فقط)"""
    _ = current_admin
    query = db.query(User)
    if approved is not None:
        query = query.filter(User.is_approved == approved)
    users = query.offset(skip).limit(min(limit, 500)).all()
    return users


@router.get("/pending-users", response_model=List[UserResponse])
async def get_pending_users(
    skip: int = 0,
    limit: int = 100,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """عرض المستخدمين المنتظرين للموافقة"""
    _ = current_admin
    users = (
        db.query(User)
        .filter(User.is_approved == False)  # noqa: E712
        .offset(skip)
        .limit(min(limit, 500))
        .all()
    )
    return users


@router.post("/approve-user/{user_id}")
@limiter.limit("10/minute")
async def approve_user(
    request: Request,
    user_id: int,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """موافقة المدير على مستخدم"""
    _ = request
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.is_approved:
        return {"message": "User is already approved"}

    user.is_approved = True
    user.approved_at = datetime.utcnow()
    user.approved_by = current_admin.id
    db.commit()

    await redis_cache.publish(f"user_approval_{user.id}", "approved")
    return {"message": f"User {user.email} approved successfully"}


@router.delete("/users/{user_id}")
@limiter.limit("5/minute")
async def delete_user(
    request: Request,
    user_id: int,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """حذف مستخدم معين (للمدير فقط)"""
    _ = request
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")

    if user.id == current_admin.id:
        raise HTTPException(status_code=400, detail="لا يمكنك حذف حسابك الخاص من هنا")

    if user.is_admin:
        admin_count = db.query(User).filter(User.is_admin == True).count()  # noqa: E712
        if admin_count <= 1:
            raise HTTPException(
                status_code=400,
                detail="لا يمكن حذف آخر حساب مدير في النظام",
            )

    email = user.email
    db.delete(user)
    db.commit()
    await invalidate_all_sessions(user_id)
    return {"message": f"تم حذف المستخدم {email} بنجاح"}


@router.post("/users/{user_id}/unlock")
@limiter.limit("10/minute")
async def unlock_user(
    request: Request,
    user_id: int,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """إلغاء قفل حساب مستخدم بعد محاولات فاشلة"""
    _ = request
    _ = current_admin
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_locked = False
    user.locked_until = None
    user.failed_login_attempts = 0
    db.commit()
    return {"message": f"User {user.email} unlocked"}


@router.post("/refresh-data")
@limiter.limit("3/minute")
async def refresh_stock_data(
    request: Request,
    page: int = 1,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """مسح كاش الأسهم وإرجاع إحصائيات الكاش (لا يعتمد على stock_cache القديم)."""
    _ = request
    _ = db
    _ = current_admin
    _ = page
    try:
        stock_keys = await redis_cache.keys("tadawul_stocks:*")
        deleted = 0
        for key in stock_keys:
            if await redis_cache.delete(key):
                deleted += 1

        # Also clear common tadawul bulk keys
        tadawul_keys = await redis_cache.keys("tadawul:*")
        for key in tadawul_keys:
            if await redis_cache.delete(key):
                deleted += 1

        return {
            "message": f"✅ تم مسح كاش الأسهم ({deleted} مفتاح)",
            "keys_deleted": deleted,
            "page": page,
            "limit": limit,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"❌ خطأ في تحديث البيانات: {type(e).__name__}",
        )


@router.get("/stats")
async def get_system_stats(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """إحصائيات النظام"""
    _ = current_admin
    try:
        total_users = db.query(User).count()
        pending_users = db.query(User).filter(User.is_approved == False).count()  # noqa: E712
        stock_keys = await redis_cache.keys("tadawul_stocks:*")
        redis_ok = bool(redis_cache.is_connected)

        return {
            "total_users": total_users,
            "pending_users": pending_users,
            "cached_stock_keys": len(stock_keys),
            "database": "PostgreSQL",
            "cache": "Redis" if redis_ok else "Redis (disconnected)",
            "redis_connected": redis_ok,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"❌ خطأ في جلب إحصائيات النظام: {type(e).__name__}",
        )


@router.post("/force-api-refresh/{symbol}")
@limiter.limit("10/minute")
async def force_api_refresh(
    request: Request,
    symbol: str,
    current_admin: User = Depends(get_current_admin),
):
    """مسح كاش سهم محدد لإجبار إعادة الجلب في الطلب التالي."""
    _ = request
    _ = current_admin
    try:
        clean = "".join(ch for ch in symbol if ch.isalnum()).upper()
        if not clean or len(clean) > 32:
            raise HTTPException(status_code=400, detail="Invalid symbol")

        patterns = [
            f"tadawul_stocks:symbol:{clean}*",
            f"tadawul:*:{clean}*",
            f"financials:*:{clean}:*",
        ]
        deleted = 0
        for pattern in patterns:
            keys = await redis_cache.keys(pattern)
            for key in keys:
                if await redis_cache.delete(key):
                    deleted += 1

        # Exact common key form used elsewhere
        await redis_cache.delete(f"tadawul_stocks:symbol:{clean}")

        return {
            "message": f"✅ تم مسح كاش السهم {clean}",
            "symbol": clean,
            "keys_deleted": deleted,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"❌ خطأ في تحديث بيانات السهم: {type(e).__name__}",
        )
