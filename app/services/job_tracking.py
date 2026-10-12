"""job_runs write helper — see app/models/job_run.py.

Two entry points:

    with track_job("sukuk_importer", "cron") as run:
        count = do_work()
        run.rows_written = count

    @tracked_job("sukuk_importer", "cron")
    def do_work(): ...

Non-negotiable rules, both learned from real failures in this codebase:

1. NEVER crash the wrapped script. Every write is inside try/except and the
   exception is logged, not re-raised. The original exception from the job
   body is re-raised by the context manager so the caller's own handling
   (scheduler._safe, scraper_service._run) keeps working exactly as before.

2. NEVER share the caller's SQLAlchemy session. A caught DB exception without
   rollback poisons a session into a PendingRollbackError cascade — in
   scripts/daily_market_update.py that kills the whole pipeline. So every
   write here opens and closes its own short-lived SessionLocal().

3. Degrade quietly when the table does not exist yet (the Alembic revision is
   applied separately from the code deploy). The first failure logs one
   warning per process; later ones are silent.
"""

import functools
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.models.job_run import JobRun

logger = logging.getLogger(__name__)

VALID_STATUS = ("running", "success", "failed")
VALID_TRIGGER = ("cron", "manual", "admin")

# Keys a job's return value may use to report how many rows it touched.
_ROW_KEYS = ("rows_written", "rows", "records", "record_count", "count", "total")
# Keys that carry the failure reason when a job returns a dict instead of raising.
_ERROR_KEYS = ("error", "error_message", "message", "detail")

_table_missing_warned = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session():
    """Local import: keeps this module importable even if DB config is broken."""
    from app.core.database import SessionLocal
    return SessionLocal()


def _note_table_missing(exc: Exception) -> None:
    global _table_missing_warned
    if not _table_missing_warned:
        _table_missing_warned = True
        logger.warning(
            "job_tracking: cannot write job_runs (%s). Run "
            "'alembic upgrade head' to enable job run tracking; scripts keep working.",
            type(exc).__name__,
        )
    else:
        logger.debug("job_tracking: job_runs write skipped: %s", exc)


def _pick(d: dict, keys) -> Optional[int]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
    return None


def _infer_rows(result: Any) -> Optional[int]:
    """Best-effort rows_written from the job's return value."""
    if isinstance(result, bool):
        return None
    if isinstance(result, int):
        return result
    if isinstance(result, dict):
        n = _pick(result, _ROW_KEYS)
        if n is not None:
            return n
        # Nested one level, e.g. {"run_report": {"rows": 148}}
        for v in result.values():
            if isinstance(v, dict):
                n = _pick(v, _ROW_KEYS)
                if n is not None:
                    return n
    return None


def _dict_failure(result: Any) -> Optional[str]:
    """Some jobs swallow their own exception and report failure in the return value.

    Two shapes exist in this codebase:
      * rebh_importers_service.run_*() -> {"status": "failed", "error": "..."}
      * scripts/daily_market_update.update_daily() -> {"ok": False,
        "stage_failures": [{"stage", "error"}, ...]} — it traps its own top-level
        exception, so an exception-based check would always say "success".
    """
    if not isinstance(result, dict):
        return None
    status = str(result.get("status", "")).lower()
    if status in ("failed", "error"):
        for k in _ERROR_KEYS:
            if result.get(k):
                return str(result[k])[:4000]
        return "returned status=failed with no error text"
    if result.get("ok") is False:
        failures = result.get("stage_failures") or []
        if failures:
            return "; ".join(
                f"[{f.get('stage')}] {f.get('error')}" for f in failures)[:4000]
        return "returned ok=False"
    return None


class _JobHandle:
    """Mutable handle the job body can decorate before we close the row."""

    __slots__ = ("job_name", "run_id", "rows_written", "status", "error_message", "_closed")

    def __init__(self, job_name: str, run_id: Optional[int] = None):
        self.job_name = job_name
        self.run_id = run_id              # None => the opening insert failed ⇒ no-op close
        self.rows_written: Optional[int] = None
        self.status: Optional[str] = None      # set to force success/failed
        self.error_message: Optional[str] = None
        self._closed = False


@contextmanager
def track_job(job_name: str, triggered_by: str = "cron", *, rows: Optional[int] = None):
    """Record a run of `job_name`. Re-raises the body's exception unchanged."""
    if triggered_by not in VALID_TRIGGER:
        triggered_by = "manual"

    run_id: Optional[int] = None
    started = _now()
    db = None
    try:
        db = _session()
        row = JobRun(
            job_name=job_name[:100],
            started_at=started,
            status="running",
            triggered_by=triggered_by,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        run_id = row.id
    except Exception as exc:                       # noqa: BLE001 — tracking must not break a job
        if db is not None:
            try:
                db.rollback()
            except Exception:                      # noqa: BLE001
                pass
        _note_table_missing(exc)
    finally:
        if db is not None:
            db.close()

    handle = _JobHandle(job_name, run_id)
    handle.rows_written = rows

    try:
        yield handle
    except BaseException as exc:
        _close(handle, "failed", f"{type(exc).__name__}: {exc}"[:4000])
        raise
    else:
        _close(handle, handle.status or "success", handle.error_message, handle.rows_written)


def _close(handle: _JobHandle, status: str, error_message: Optional[str],
           rows_written: Optional[int] = None) -> None:
    """Write the final state. Never raises."""
    if handle.run_id is None or handle._closed:
        return
    handle._closed = True
    if rows_written is None:
        rows_written = handle.rows_written
    if status not in VALID_STATUS:
        status = "failed"
    finished = _now()
    db = None
    try:
        db = _session()
        row = db.get(JobRun, handle.run_id)
        if row is not None:
            row.status = status
            row.finished_at = finished
            row.rows_written = rows_written
            row.error_message = error_message
            db.commit()
    except Exception as exc:                       # noqa: BLE001
        if db is not None:
            try:
                db.rollback()
            except Exception:                      # noqa: BLE001
                pass
        logger.warning("job_tracking: could not close run %s: %s", handle.run_id, exc)
    finally:
        if db is not None:
            db.close()


def tracked_job(job_name: str, triggered_by: str = "cron") -> Callable:
    """Decorator form of track_job for a whole function."""
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with track_job(job_name, triggered_by) as handle:
                result = fn(*args, **kwargs)
                # Honour dict-style "failed" returns and inferred row counts.
                failure = _dict_failure(result)
                if failure:
                    handle.status = "failed"
                    handle.error_message = failure
                else:
                    inferred = _infer_rows(result)
                    if inferred is not None and handle.rows_written is None:
                        handle.rows_written = inferred
                return result
        return wrapper
    return decorator


def with_result_handling(handle: _JobHandle, result: Any) -> Any:
    """Apply the dict-failure / inferred-rows rules to an open handle.

    Used by callers that wrap the job body themselves (scheduler._safe,
    scraper_service._run) and therefore cannot use @tracked_job.
    """
    failure = _dict_failure(result)
    if failure:
        handle.status = "failed"
        handle.error_message = failure
    else:
        inferred = _infer_rows(result)
        if inferred is not None and handle.rows_written is None:
            handle.rows_written = inferred
    return result


def record_job_run(job_name: str, status: str, *, triggered_by: str = "manual",
                   rows_written: Optional[int] = None,
                   error_message: Optional[str] = None,
                   duration_seconds: Optional[float] = None) -> None:
    """One-shot write for a job that already finished (no live wrapping)."""
    if status not in VALID_STATUS:
        status = "failed"
    started = _now()
    finished = None
    if duration_seconds is not None:
        from datetime import timedelta
        started = started - timedelta(seconds=float(duration_seconds))
        finished = _now()
    db = None
    try:
        db = _session()
        db.add(JobRun(
            job_name=job_name[:100],
            started_at=started,
            finished_at=finished,
            status="success" if status == "running" else status,
            rows_written=rows_written,
            error_message=error_message,
            triggered_by=triggered_by if triggered_by in VALID_TRIGGER else "manual",
        ))
        db.commit()
    except Exception as exc:                       # noqa: BLE001
        if db is not None:
            try:
                db.rollback()
            except Exception:                      # noqa: BLE001
                pass
        _note_table_missing(exc)
    finally:
        if db is not None:
            db.close()
