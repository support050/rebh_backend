"""Admin "Run Now" dispatcher — used by app/api/routes/system_map.py.

Why this module exists
----------------------
The Runbook lists jobs by the name they are recorded under in ``job_runs``.
Pressing "Run Now" has to run the *same* callable the cron uses and write the
*same* job_name, otherwise "last run" stops being comparable between a scheduled
night and a manual click. So the capability list is read from the code that
actually runs jobs:

  · app/core/scheduler.JOB_CALLABLES      → cron job keys (sp500, fred:*,
                                            macro_importer, breadth_daily, …)
  · app/services/scraper_service.run_*    → the scraper:* keys (admin-triggered
                                            runners that record their own row)

DB discipline honoured here (from the Runbook task rules):
  · This module opens a session ONLY for two short read-only SELECTs — the
    "is a run already in flight" check and the status read — each in its own
    transaction with a statement timeout, always closed in ``finally``.
    The job body owns its own sessions; ``track_job`` already uses its own
    short-lived SessionLocal and never re-raises a tracking failure.
  · Both reads go through the composite index ``ix_job_runs_name_started
    (job_name, started_at DESC)`` created by 2026_10_11_jobruns.
  · No DB locks and no isolation-level changes. Two best-effort guards exist —
    an in-process dict behind a threading.Lock, and the job_runs 'running'
    check. They catch a double click in one worker; they cannot make two
    workers atomic. The race window is disclosed, not solved.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal

logger = logging.getLogger(__name__)

# A 'running' row older than this is treated as abandoned (killed process), so
# it can never lock the button forever.
STALE_RUNNING_MINUTES = 120
_READ_TIMEOUT_MS = 3000

# Wall clock of THIS process's birth. ``uvicorn --reload`` destroys the server
# child on every saved file, and a Run Now job is a daemon thread inside it, so
# the job dies mid-run and leaves its job_runs row open forever. A 'running'
# admin row that predates BOOT_AT and is absent from _ACTIVE therefore cannot
# belong to any thread that exists: it is a corpse, and must not read "running"
# (nor lock its own Run Now button) until STALE_RUNNING_MINUTES has elapsed.
# Rows opened AFTER this process booted stay trusted — a sibling worker in a
# multi-worker deploy owns those, and so does a live Run Now thread.
BOOT_AT = datetime.now(timezone.utc)

# job_name -> {fn, label, self_tracked}. self_tracked=True means the callable
# writes its own job_runs row (scraper_service does that) ⇒ do NOT wrap again.
_TARGETS: Dict[str, Dict[str, Any]] = {}
_ACTIVE: Dict[str, datetime] = {}
_targets_lock = threading.Lock()
# Guards check-and-reserve against the MAX_CONCURRENT_JOBS cap below; the
# existing in-process double-click caveat (module docstring) applies here too.
_active_lock = threading.Lock()
_registered = False


def _ensure_targets() -> None:
    """Build the dispatch table lazily, at first use, not at import.

    scheduler.py imports scrapers through lazy __import__ closures, and
    scraper_service imports the DB layer — touching either at module import
    time would make `import app.main` fragile. Failures here are logged and
    leave the table partial: an unknown job simply shows no Run Now button.
    """
    global _registered
    with _targets_lock:
        if _registered:
            return
        targets: Dict[str, Dict[str, Any]] = {}

        try:
            from app.core.scheduler import JOB_CALLABLES, JOB_LABELS
            for name, fn in JOB_CALLABLES.items():
                targets[name] = {
                    "fn": fn,
                    "label": JOB_LABELS.get(name, name),
                    "self_tracked": False,
                }
        except Exception as e:                                  # noqa: BLE001
            logger.warning("job_runner: scheduler callables unavailable: %s", e)

        try:
            from app.services import scraper_service
            # These write scraper:<name> themselves via _run()/track_job().
            for name, fn in (
                ("scraper:sp500_price", lambda: scraper_service.run_sp500_price()),
                ("scraper:sp500_pe", scraper_service.run_sp500_pe),
                ("scraper:sp500_ey", scraper_service.run_sp500_ey),
                ("scraper:treasury_gov", lambda: scraper_service.run_treasury_gov()),
                ("scraper:tasi_components", scraper_service.run_tasi_components),
            ):
                targets[name] = {"fn": fn, "label": name, "self_tracked": True}
        except Exception as e:                                  # noqa: BLE001
            logger.warning("job_runner: scraper_service runners unavailable: %s", e)

        _TARGETS.clear()
        _TARGETS.update(targets)
        _registered = True


def runnable_names() -> list:
    _ensure_targets()
    return sorted(_TARGETS)


def is_runnable(job_name: str) -> bool:
    _ensure_targets()
    return job_name in _TARGETS


def describe(job_name: str) -> Optional[Dict[str, Any]]:
    """UI-facing capability record. None ⇒ no Run Now button for this job."""
    _ensure_targets()
    target = _TARGETS.get(job_name)
    if target is None:
        return None
    return {
        "runnable": True,
        "self_tracked": bool(target["self_tracked"]),
        "label": target["label"],
    }


def _read(db: Session, sql: str, **params) -> Optional[list]:
    """One SELECT in its own short transaction, with a statement timeout."""
    try:
        db.rollback()                     # clean tx; SET LOCAL applies to it only
        db.execute(text("SET LOCAL statement_timeout = :ms"), {"ms": _READ_TIMEOUT_MS})
        return [tuple(r) for r in db.execute(text(sql), params).fetchall()]
    except Exception as e:                # noqa: BLE001 — read-only, never fatal
        db.rollback()
        logger.debug("job_runner read failed: %s", e)
        return None


def latest_run(job_name: str) -> Optional[Dict[str, Any]]:
    """Newest job_runs row for one job. 1 query, ix_job_runs_name_started.

    ORDER BY started_at DESC LIMIT 1 on (job_name, started_at DESC) is a single
    backward index scan — it does not read the whole partition.
    """
    db = SessionLocal()
    try:
        rows = _read(
            db,
            "SELECT status, started_at, finished_at, rows_written, error_message, "
            "triggered_by FROM job_runs WHERE job_name = :job_name "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            job_name=job_name,
        )
        if not rows:
            return None
        status_, started, finished, rows_written, error, triggered = rows[0]
        return {
            "status": status_,
            "started_at": started.isoformat() if started else None,
            "finished_at": finished.isoformat() if finished else None,
            "rows_written": rows_written,
            "error_message": error,
            "triggered_by": triggered,
        }
    finally:
        db.close()


def row_is_a_corpse(job_name: str, started_at: Optional[datetime],
                    triggered_by: Optional[str]) -> bool:
    """True when an open 'running' row belongs to a process that no longer exists.

    Only Run Now rows qualify: ``triggered_by='admin'`` is written exclusively by
    this module's own thread, so a reload is the only thing that can abandon one.
    A cron or terminal row is owned by a DIFFERENT process, which this one cannot
    see — guessing about those is what STALE_RUNNING_MINUTES is for.
    """
    if started_at is None or triggered_by != "admin":
        return False
    if job_name in _ACTIVE:
        return False                        # this process really is running it
    if started_at.tzinfo is None:           # a naive column value is UTC by convention
        started_at = started_at.replace(tzinfo=timezone.utc)
    return started_at < BOOT_AT


def is_in_flight(job_name: str) -> bool:
    """True when this process started the run, or the ledger shows a young
    'running' row that a live process still owns."""
    if job_name in _ACTIVE:
        return True
    db = SessionLocal()
    try:
        rows = _read(
            db,
            "SELECT started_at, triggered_by FROM job_runs "
            "WHERE job_name = :job_name AND status = 'running' "
            "AND started_at > NOW() - (:minutes * INTERVAL '1 minute') "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            job_name=job_name, minutes=STALE_RUNNING_MINUTES,
        )
        if not rows:
            return False
        started, triggered = rows[0]
        return not row_is_a_corpse(job_name, started, triggered)
    finally:
        db.close()


def _post_run_hooks(job_name: str) -> None:
    """Bust whatever cache this job just made stale.

    One table for the whole system (``cache_bust.JOB_GROUPS``), instead of the
    three special cases this used to be — economic jobs, market_reports, and
    breadth_daily returning early. Two consequences worth naming:

    * jobs whose output is served from ``terminal:*`` / ``scraper:*``
      (engine_vintages, filings_importer, scraper:tasi_components) are now busted
      too, where they previously left the admin staring at pre-run numbers;
    * a bust that reached nothing is logged at WARNING. It used to be
      ``logger.debug("cache clear skipped")`` — invisible by default, which is
      exactly how "the script ran and the site did not update" stayed unexplained.

    ``remote=False``: this runs on the server, next to the server's Redis, so the
    HTTP door would only be the app calling itself. The door is for scripts run
    somewhere else (see app/core/cache_bust.py).

    Part 2 / Phase 4: the bust goes through ``cache_warmers.bust_job_caches``,
    which first rewrites the hot keys of any namespace this job clears and then
    protects exactly those from the eviction behind them. Ordering is the whole
    point — warm, then sweep the rest — so a reader never finds the page they are
    about to open missing from the cache. The warm-up is optional by design: if
    it cannot run, the report is the same bust this function always performed.
    """
    if job_name == "breadth_daily":
        return                       # _breadth_daily_body + the job itself both bust
    try:
        from app.services import cache_warmers
        report = cache_warmers.bust_job_caches(job_name, local=True, remote=False)
    except Exception as e:                        # noqa: BLE001 — cache is optional
        logger.warning("job_runner: post-run cache hook failed for %s: %s", job_name, e)
        return
    if report["ok"]:
        logger.info("job_runner: busted %s after %s", report["summary"], job_name)
    else:
        reasons = "; ".join(str(c.get("error")) for c in report["channels"])
        logger.warning("job_runner: CACHE NOT BUSTED after %s (%s) — %s; readers keep "
                       "the previous payload until the TTL lapses.",
                       job_name, report["summary"], reasons)


def _execute(job_name: str, target: Dict[str, Any]) -> None:
    """Thread body. Never raises: the caller already got its 202."""
    from app.services.job_tracking import track_job, with_result_handling

    started = datetime.now(timezone.utc)
    _ACTIVE[job_name] = started
    try:
        if target["self_tracked"]:
            target["fn"]()
        else:
            with track_job(job_name, "admin") as run:
                result = target["fn"]()
                with_result_handling(run, result)
        _post_run_hooks(job_name)
        logger.info("run-now %s finished", job_name)
    except Exception as e:                        # noqa: BLE001
        logger.exception("run-now %s failed: %s", job_name, e)
    finally:
        _ACTIVE.pop(job_name, None)


def start(job_name: str) -> Dict[str, Any]:
    """Kick off a run in a daemon thread. Returns a verdict, never raises.

    statuses: 'started' | 'already_running' | 'not_runnable'.
    """
    _ensure_targets()
    target = _TARGETS.get(job_name)
    if target is None:
        return {"job_name": job_name, "status": "not_runnable",
                "message": "No callable registered for this job_name in "
                           "scheduler.JOB_CALLABLES or scraper_service."}
    if is_in_flight(job_name):
        return {"job_name": job_name, "status": "already_running",
                "message": "A run is already in flight (in this process or a "
                           "job_runs row younger than %d min)." % STALE_RUNNING_MINUTES}

    # Concurrency cap (2026-11-03): three heavy run-now jobs at once from one
    # machine saturated the home↔Render Postgres uplink and black-holed every
    # API request — admin pages went blank until the jobs finished. Reserve a
    # slot here (not inside the thread) so two fast clicks cannot both pass the
    # count.
    with _active_lock:
        active = [k for k in _ACTIVE if k != job_name]
        if len(active) >= settings.MAX_CONCURRENT_JOBS:
            return {"job_name": job_name, "status": "already_running",
                    "message": "Concurrency cap reached (%d/%d slots busy: %s). "
                               "Jobs share this machine's DB uplink — run it "
                               "after one finishes."
                               % (len(active), settings.MAX_CONCURRENT_JOBS,
                                  ", ".join(sorted(active)))}
        _ACTIVE[job_name] = datetime.now(timezone.utc)

    thread = threading.Thread(
        target=_execute, args=(job_name, target),
        name=f"run-now-{job_name}", daemon=True,
    )
    try:
        thread.start()
    except Exception as e:                        # noqa: BLE001 — verdict API
        with _active_lock:
            _ACTIVE.pop(job_name, None)
        logger.exception("run-now %s could not start: %s", job_name, e)
        return {"job_name": job_name, "status": "not_runnable",
                "message": "Thread could not be started: %s" % e}
    return {"job_name": job_name, "status": "started",
            "message": "Started in the background. Follow it in the Last run column.",
            "label": target["label"], "self_tracked": target["self_tracked"]}


def status_for(job_name: str) -> Dict[str, Any]:
    """Polling payload for the button: latest ledger row + in-process flag."""
    _ensure_targets()
    return {
        "job_name": job_name,
        "runnable": job_name in _TARGETS,
        "in_flight_this_process": job_name in _ACTIVE,
        "started_this_process": (_ACTIVE[job_name].isoformat()
                                 if job_name in _ACTIVE else None),
        "last_run": latest_run(job_name),
    }
