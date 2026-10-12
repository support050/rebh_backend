from datetime import datetime
from collections import deque
from typing import List, Optional

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_current_admin
from app.core.auth import invalidate_all_sessions
from app.core.database import get_db
from app.core.limiter import limiter
from app.core.redis import redis_cache
from app.models.user import User
from app.schemas.auth import UserResponse
from app.services.user_cleanup import delete_user_related_data

router = APIRouter(prefix="/admin", tags=["Admin"])
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  Daily Market Update — manual trigger (runs in-process, next to the DB)
# ═══════════════════════════════════════════════════════════════
# The pipeline is heavy and long-running, so it is executed in a daemon
# thread and the request returns immediately. Two guards stop double runs:
#   1. _PIPELINE["thread"].is_alive()  → same worker, same process
#   2. update_status.is_updating mutex → across workers / across machines
_pipeline_logger = logging.getLogger("scripts.daily_market_update")
_PIPELINE: dict = {
    "thread": None,
    "run_id": None,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "target_date": None,
    "run_report": None,
}
_PIPELINE_LOG_TAIL: deque = deque(maxlen=300)


class _TailCaptureHandler(logging.Handler):
    """Keeps the last lines of the pipeline log so the admin UI can watch it."""

    def emit(self, record):
        try:
            _PIPELINE_LOG_TAIL.append(
                "%s %s: %s" % (datetime.utcnow().strftime("%H:%M:%S"),
                               record.levelname[:4],
                               record.getMessage())
            )
        except Exception:
            pass


_tail_handler: Optional[_TailCaptureHandler] = None


def _run_pipeline_in_thread(target_date: Optional[str], run_id: str):
    """Thread body: import the script lazily, run main(), record the outcome."""
    global _tail_handler
    from app.services.job_tracking import track_job
    _PIPELINE_LOG_TAIL.append(f"▶ run {run_id} started (target_date={target_date or 'auto'})")
    if _tail_handler is None:
        _tail_handler = _TailCaptureHandler(level=logging.INFO)
        _pipeline_logger.addHandler(_tail_handler)
    try:
        import importlib
        dmu = importlib.import_module("scripts.daily_market_update")
        # update_daily() never raises — it traps its own top-level exception and
        # returns a run report with ok/stage_failures. So the job_runs verdict
        # is read from the report, not from an exception.
        with track_job("daily_market_update", "admin") as run:
            report = dmu.main(target_date)
            _PIPELINE["error"] = None
            _PIPELINE["run_report"] = report
            _quality = (report or {}).get("quality") or {}
            if isinstance(_quality.get("rows"), int):
                run.rows_written = _quality["rows"]
            failures = (report or {}).get("stage_failures") or []
            if failures:
                run.status = "failed"
                run.error_message = "; ".join(
                    f"[{f['stage']}] {f['error']}" for f in failures)[:4000]
            elif report is None or (report or {}).get("ready_date_advanced") is False:
                run.status = "failed"
                run.error_message = "latest_ready_date was not advanced"
            # Stage failures are NOT exceptions — update_daily() swallows them on
            # purpose so one stage cannot kill the finalize. Surface them here.
            critical = (report or {}).get("critical_stages") or []
            advisories = (report or {}).get("stage_advisories") or []
        if failures:
            _PIPELINE_LOG_TAIL.append(
                f"⚠ run {run_id}: {len(failures)} stage(s) failed — "
                + "; ".join(f"[{f['stage']}] {f['error']}" for f in failures)[:600]
            )
            if critical and not (report or {}).get("ready_date_advanced", True):
                _PIPELINE_LOG_TAIL.append(
                    f"⛔ latest_ready_date was NOT advanced — critical stage(s): "
                    + ", ".join(critical)
                )
        else:
            _PIPELINE_LOG_TAIL.append(f"✔ run {run_id} finished — all stages OK")
        # Advisory notes are explained, non-blocking conditions (e.g. a stock listed
        # yesterday cannot have SMA200 yet) — reported, but never as a stage failure.
        if advisories:
            _PIPELINE_LOG_TAIL.append(
                f"ℹ run {run_id}: {len(advisories)} advisory note(s) — "
                + "; ".join(f"[{a['stage']}] {a['error']}" for a in advisories)[:400]
            )
    except Exception as exc:  # noqa: BLE001 — the thread must never die silently
        _PIPELINE["error"] = f"{type(exc).__name__}: {exc}"
        _PIPELINE["run_report"] = None
        logger.exception("❌ [Admin] pipeline run %s failed", run_id)
        _PIPELINE_LOG_TAIL.append(f"✖ run {run_id} failed: {_PIPELINE['error']}")
    finally:
        _PIPELINE["finished_at"] = datetime.utcnow().isoformat()
        if _tail_handler is not None:
            _pipeline_logger.removeHandler(_tail_handler)
            _tail_handler = None


@router.post("/pipeline/daily-market-update/run")
def trigger_daily_market_update(
    target_date: Optional[str] = None,
    force: bool = False,
    current_admin: User = Depends(get_current_admin),
):
    """
    Start the Daily Market Update pipeline as a background task on this server.

    `target_date` (YYYY-MM-DD) is optional — omit it to let the pipeline
    auto-detect the latest trading day. `force=true` is needed only to start a
    run when the DB mutex is held by a run younger than the stale threshold.
    """
    existing = _PIPELINE.get("thread")
    if existing is not None and existing.is_alive() and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Pipeline is already running in this process",
                "run_id": _PIPELINE.get("run_id"),
                "started_at": _PIPELINE.get("started_at"),
            },
        )

    if target_date:
        try:
            datetime.strptime(target_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="target_date must be YYYY-MM-DD")

    run_id = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    thread = threading.Thread(
        target=_run_pipeline_in_thread,
        args=(target_date, run_id),
        name=f"daily-market-update-{run_id}",
        daemon=True,
    )
    _PIPELINE.update({
        "thread": thread,
        "run_id": run_id,
        "started_at": datetime.utcnow().isoformat(),
        "finished_at": None,
        "error": None,
        "target_date": target_date,
        "run_report": None,
    })
    thread.start()

    logger.info("🚀 [Admin] %s triggered pipeline run %s (target_date=%s)",
                current_admin.email, run_id, target_date)
    return {
        "started": True,
        "run_id": run_id,
        "target_date": target_date,
        "triggered_by": current_admin.email,
        "note": "The update_status mutex still blocks a second concurrent run "
                "from another process; the API returns immediately.",
    }


@router.get("/pipeline/daily-market-update/status")
def pipeline_daily_market_update_status(
    lines: int = 60,
    current_admin: User = Depends(get_current_admin),
):
    """
    Live status of the pipeline. The DB row is the source of truth (it is shared
    across workers); the process fields are best-effort for this worker only.
    """
    from scripts.daily_market_update import get_pipeline_status

    thread = _PIPELINE.get("thread")
    return {
        "db": get_pipeline_status(),
        "process": {
            "run_id": _PIPELINE.get("run_id"),
            "running_this_worker": bool(thread is not None and thread.is_alive()),
            "started_at": _PIPELINE.get("started_at"),
            "finished_at": _PIPELINE.get("finished_at"),
            "error": _PIPELINE.get("error"),
            "target_date": _PIPELINE.get("target_date"),
        },
        # Last run of THIS worker (None if the run came from the CLI or another
        # worker — db.unpublished_days is the cross-process alert in that case).
        "run_report": _PIPELINE.get("run_report"),
        "log_tail": list(_PIPELINE_LOG_TAIL)[-max(1, min(lines, 300)):],
    }


@router.post("/pipeline/daily-market-update/unlock")
def clear_pipeline_lock(
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """
    Force-clear a stuck is_updating mutex (a run that died without unlocking).
    Refused while a live thread is running in this worker.
    """
    thread = _PIPELINE.get("thread")
    if thread is not None and thread.is_alive():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A pipeline run is alive in this worker — stop it first.",
        )
    row = db.execute(text(
        "SELECT is_updating, started_at FROM update_status WHERE id = 1"
    )).fetchone()
    if row and row[0]:
        db.execute(text("UPDATE update_status SET is_updating = FALSE WHERE id = 1"))
        db.commit()
    return {
        "cleared": bool(row and row[0]),
        "was_started_at": str(row[1]) if row and row[1] else None,
        "by": current_admin.email,
    }


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


@router.post("/revoke-user/{user_id}")
@limiter.limit("10/minute")
async def revoke_user(
    request: Request,
    user_id: int,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """سحب صلاحية مستخدم وحذف حسابه وبياناته بالكامل"""
    _ = request
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == current_admin.id:
        raise HTTPException(status_code=400, detail="لا يمكنك حذف حسابك الخاص")

    if user.is_admin:
        admin_count = db.query(User).filter(User.is_admin == True).count()  # noqa: E712
        if admin_count <= 1:
            raise HTTPException(
                status_code=400,
                detail="لا يمكن حذف آخر حساب مدير في النظام",
            )

    email = user.email

    # 1) Invalidate all sessions FIRST (fail-closed: if Redis fails, abort before DB changes)
    await invalidate_all_sessions(user_id)

    # 2) Delete all FK-dependent data, then the user — single transaction
    try:
        delete_user_related_data(db, user_id)
        db.delete(user)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"message": f"User {email} has been revoked and deleted successfully"}


@router.delete("/users/{user_id}")
@limiter.limit("5/minute")
async def delete_user(
    request: Request,
    user_id: int,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """حذف مستخدم وجميع بياناته بالكامل (للمدير فقط)"""
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

    # Invalidate sessions first (no-op if user had no active sessions)
    await invalidate_all_sessions(user_id)

    # Delete all FK-dependent data, then the user
    try:
        delete_user_related_data(db, user_id)
        db.delete(user)
        db.commit()
    except Exception:
        db.rollback()
        raise

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
