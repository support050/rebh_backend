"""Admin — System Map (Runbook) API.

Read-only view over ``app/system_registry.py`` joined with two live signals:
    · job_runs  → "did the script actually run, and how did it end?"
    · the tables themselves → "is the data it wrote fresh?"

Design rules (all of them hard requirements from the Runbook spec):
  1. NEVER 500. A missing table, a missing column, a missing job_runs table
     (the migration has not been run yet on a fresh deploy) all degrade to a
     ``bad`` entry with a human message. An admin page that itself crashes is
     worse than the outage it is trying to show. Only an unknown system id 404s.
  2. Its own DB session, never ``Depends(get_db)``. This endpoint probes schema
     that may not exist; a failed statement poisons the session that ran it.
     Owning one session for the whole snapshot (and rolling back per probe)
     keeps that damage inside this module.
  3. One build per ``IN_MEMORY_TTL_SYSTEM_MAP`` for the whole map. Opening the page
     fires /systems and then /systems/{id} for the selected system; both are
     served from the same snapshot, so the sidebar and the detail never disagree.
  4. SQL identifiers come from the registry, never from the client. Only the
     system id is taken from the URL, and it is looked up in a dict.

Auth: mounted in app/main.py with ``admin_dependencies`` (get_current_admin)
under prefix="/api" → the paths below are /api/admin/system-map/*.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import system_registry as registry
from app.api.deps import get_current_admin
from app.core.cache_config import IN_MEMORY_TTL_SYSTEM_MAP
from app.core.database import SessionLocal
from app.services import job_runner

router = APIRouter(prefix="/admin/system-map", tags=["Admin — System Map"])
logger = logging.getLogger(__name__)

# Canonical value lives in cache_config.IN_MEMORY_TTL_SYSTEM_MAP (frontend
# app/admin/system-map polls with the same interval).
CACHE_TTL_SECONDS = IN_MEMORY_TTL_SYSTEM_MAP
REGISTRY_FILE = "backend/app/system_registry.py"

# The probes of one build share a single armed transaction (see _arm) carrying
# this budget, so one slow query cannot hang the admin page (and cannot become
# a full scan on a big table). MAX() on an indexed date column is an index scan;
# if an index is missing in this environment the probe dies at 2.5 s and
# degrades to 'unknown' instead of stalling the request.
PROBE_TIMEOUT_MS = 2500
# Branches per UNION ALL freshness probe. Chunked so one timeout (or one bad
# column) costs a chunk, not the whole map.
_MAX_UNION_BRANCHES = 12

# Staleness budget per cadence, in trading days, for the integrity scan. The
# scanner is the safety net now that sukuk/macro are off the daily pipeline: a
# weekly snapshot must be caught stale in ~2 weeks, not 3 months, so the old
# flat non-daily threshold (60) was far too loose for sukuk_market_data.
_STALE_LAG_BY_CADENCE = {"daily": 1, "weekly": 10, "monthly": 40,
                         "quarterly": 100, "manual": 40}

# daily_market_update is deliberately NOT dispatched here: it has its own
# endpoint with a DB mutex and a 409 on concurrent runs. The UI calls that.
_PIPELINE_RUN_ENDPOINT = "/api/admin/pipeline/daily-market-update/run"

# Statuses that must not drag a system's health down.
_NEUTRAL = ("ok", "unknown", "untracked")
_RANK = {"ok": 0, "untracked": 0, "unknown": 0, "late": 1, "bad": 2}

_lock = threading.Lock()
_REBUILDING = False              # one background refresh at a time (guarded by _lock)
_CACHE: Dict[str, Any] = {"built_at": None, "systems": {}, "error": None,
                          "queries": {}, "ledger_missing": False}


# ══════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ══════════════════════════════════════════════════════════════════════════════
def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Postgres returns tz-aware values; naive ones are assumed to be UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _as_date(value: Any) -> Optional[date]:
    """MAX() over a Date column gives date, over a timestamptz gives datetime,
    and a String column (`period`, `as_of`) gives a str we may still read."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m", "%Y-Q%q"):
            try:
                return datetime.strptime(value[:19], fmt).date()
            except ValueError:
                continue
    return None


def _worst(*statuses: str) -> str:
    worst = "ok"
    for s in statuses:
        if _RANK.get(s, 0) > _RANK.get(worst, 0):
            worst = s
    return worst


def _arm(db: Session, timeout_ms: int = PROBE_TIMEOUT_MS) -> None:
    """Open the one transaction a build's probes share, with the budget set once.

    `SET LOCAL` lives for the whole transaction, so a single pragma covers every
    SELECT that follows. Sending it per probe (the old shape) put THREE round
    trips behind every query — rollback, pragma, SELECT — and against the live
    Render Postgres each hop measured ~700 ms of pure latency: ~33 hops for an
    11-query build, which was most of the 8 s the Runbook took to open. Armed
    once, the build costs one hop per SELECT.

    The shared transaction is also one consistent read snapshot, so the chunked
    freshness probes cannot see a table mid-write between two of them.
    """
    try:
        db.rollback()
        db.execute(text("SET LOCAL statement_timeout = :ms"), {"ms": timeout_ms})
    except SQLAlchemyError:
        db.rollback()


def _probe(db: Session, sql: str, timeout_ms: int = PROBE_TIMEOUT_MS, **params) -> Optional[List[tuple]]:
    """One SELECT, in the already-armed transaction -> rows, or None on failure.

    Two rules that must not be undone:
      · the caller arms first (_arm). No pragma is sent here, which is what keeps
        a build at one round trip per query.
      · the rollback on failure: a dead statement leaves the session in an aborted
        transaction, and without this the *next* probe would fail too and the
        whole snapshot would light up red for one missing table. That rollback
        ends the transaction, so the timeout is re-armed here for whatever is
        still left to run — cost only on the path that already failed.
    """
    try:
        return [tuple(r) for r in db.execute(text(sql), params).fetchall()]
    except SQLAlchemyError as e:
        logger.debug("system-map probe failed: %s", e)
        db.rollback()
        _arm(db, timeout_ms)
        return None
    except Exception as e:                              # driver-level surprises
        logger.debug("system-map probe failed (non-SQLAlchemy): %s", e)
        db.rollback()
        _arm(db, timeout_ms)
        return None


# ═════════════════════════════════════════════════════════════════════════════ 

# Keeping the pragma at one per build is deliberate: 11 SELECTs at ~700 ms of
# network latency each is the difference between an instant Runbook and an 8 s
# one, so new probes must reuse the armed transaction, never re-arm themselves.


# ══════════════════════════════════════════════════════════════════════════════
#  job_runs side — "did they run today?"
# ══════════════════════════════════════════════════════════════════════════════
def _fetch_last_runs(db: Session) -> Optional[Dict[str, Dict[str, Any]]]:
    """Newest job_runs row per job_name. None ⇒ the ledger is not readable yet."""
    rows = _probe(db, """
        SELECT job_name, status, started_at, finished_at,
               rows_written, error_message, triggered_by
        FROM (
            SELECT DISTINCT ON (job_name) *
            FROM job_runs
            ORDER BY job_name, started_at DESC, id DESC
        ) t
    """)
    if rows is None:
        return None
    out: Dict[str, Dict[str, Any]] = {}
    for job_name, status_, started, finished, rows_written, error, triggered in rows:
        out[job_name] = {
            "status": status_,
            "started_at": _aware(started),
            "finished_at": _aware(finished),
            "rows_written": rows_written,
            "error_message": error,
            "triggered_by": triggered,
        }
    return out


def _fetch_counts(db: Session, sql: str) -> Dict[str, int]:
    rows = _probe(db, sql)
    return {r[0]: int(r[1]) for r in (rows or [])}


def _run_capability(job_name: str) -> Dict[str, Any]:
    """Can "Run Now" be offered for this job, and through which door?

    Capabilities come from the code that runs jobs (job_runner), not from the
    registry, so the button can never promise something the backend cannot do.
    """
    if job_name == "daily_market_update":
        return {
            "runnable": True,
            "delegated_to": _PIPELINE_RUN_ENDPOINT,
            "reason": "existing endpoint owns the DB mutex and the 409-on-busy "
                      "behaviour; this panel only links to it",
        }
    cap = job_runner.describe(job_name)
    if cap:
        return {
            "runnable": True,
            "delegated_to": None,
            "reason": None,
            "self_tracked": cap["self_tracked"],
        }
    return {"runnable": False, "delegated_to": None,
            "reason": "no callable registered for this job_name"}


def _data_supersedes(owned: List[Dict[str, Any]],
                     since: Optional[datetime]) -> Optional[str]:
    """Newest data date, when the job's own tables prove a run the ledger missed.

    ``job_runs`` can only ever see cron, Run Now and the Admin endpoints. Running
    the script from a terminal — the very command printed on this card — opens no
    row for most jobs (breadth_job.py self-records since 2026-11-03; the others
    still do not), so a genuine failure can sit on the card long after the data
    was rebuilt. The data is the ground truth, and the test is strict: every table
    the job writes whose freshness was actually MEASURED must be un-stale, and the
    newest of them must be on or after the run being judged. Anything less keeps
    the ledger's verdict.

    "Un-stale" means anything but ``late``/``bad`` — so a manual-only table with no
    lag threshold (``eurodollar_futures``, status ``unknown`` because nothing is
    expected of it) still counts, as long as a date was read from it. A table whose
    probe timed out has no ``latest`` and is simply not evidence either way.
    Returns the newest date seen (ISO) when the ledger was overtaken, else None.
    """
    measured = [t for t in owned if t["latest"]]
    if not measured or any(t["status"] in ("late", "bad") for t in measured):
        return None
    # The LOWEST date among them, not the highest: "every table this job writes
    # reaches at least this far" is the claim that actually proves the run
    # happened, and a forward-dated table (weekly stages carry a period-end date)
    # must not be allowed to speak for the daily ones.
    through = min(t["latest"] for t in measured)
    # Both sides are ISO strings, so ``<`` is a date comparison; ``latest`` may
    # carry a time suffix when the registry's date column is a timestamp.
    return None if since and through < since.date().isoformat() else through


def _build_job(job: Dict[str, Any], last: Optional[Dict[str, Any]],
               failures_7d: int, stuck_running: int,
               ledger_missing: bool, now: datetime,
               owned: Optional[List[Dict[str, Any]]] = None,
               *, corpse: bool = False) -> Dict[str, Any]:
    """One job entry + its verdict.

    Rules (shown in the UI legend, derived from the Runbook spec):
      · never recorded             → bad   (no row since job_runs existed)
      · last run failed            → bad
      · last run still 'running'   → late while inside the runner's own
                                     STALE_RUNNING_MINUTES, then a dead process
                                     (a reload kills the Run Now thread mid-run).
                                     A corpse of a previous server generation
                                     (``corpse=True``) is judged dead at once.
      · success older than budget  → late up to 2× budget, bad beyond
      · success with 0 rows        → late (the process worked, wrote nothing)
      · untracked entry            → 'untracked'; never affects system health
    The three ledger-only verdicts (never recorded / failed / 0 rows) are then
    handed to ``_data_supersedes``: fresh tables outrank a stale ledger line, and
    the row says WHY instead of flipping silently (``superseded: true``). An
    abandoned 'running' row is judged the same way — it is a bookkeeping gap,
    not a fact about the data.
    """
    budget = job["max_allowed_age_hours"]
    payload: Dict[str, Any] = {
        "job_name": job["job_name"],
        "file": job["file"],
        "expected_schedule": job["expected_schedule"],
        "trigger": job["trigger"],
        "tracked": job.get("tracked", True),
        "max_allowed_age_hours": budget,
        "writes": job["writes"],
        "note": job["note"],
        # Same command list the Schedule tab shows: a job is copy-pasteable from
        # either surface, and registry.commands_for() is the only place that
        # decides what that command is.
        "commands": registry.commands_for(job),
        # When the list above is legitimately empty (a library module with no
        # __main__ door), the card says WHY instead of showing a blank slot.
        "cli_absence_reason": registry.cli_absence_reason(job),
        "failures_7d": failures_7d,
        "stuck_running": stuck_running,
        "last_run": None,
        "status": "unknown",
        "message": "",
        # True when the ledger said worse but the tables proved otherwise.
        "superseded": False,
        "run": _run_capability(job["job_name"]),
    }

    if not job.get("tracked", True):
        payload["status"] = "untracked"
        payload["message"] = "Not wrapped by track_job() — job_runs cannot show this run."
        return payload

    if ledger_missing:
        payload["status"] = "bad"
        payload["message"] = "job_runs is not readable — run `alembic upgrade head`."
        return payload

    if last is None:
        through = _data_supersedes(owned or [], None)
        if through:
            payload["status"] = "ok"
            payload["superseded"] = True
            payload["message"] = ("No job_runs row for this job_name, but all its "
                                  f"tables hold data through {through} — it runs "
                                  "outside the ledger.")
            return payload
        payload["status"] = "bad"
        payload["message"] = "Never recorded — no job_runs row for this job_name."
        return payload

    started = last["started_at"]
    age_hours = round((now - started).total_seconds() / 3600, 1) if started else None
    payload["last_run"] = {
        "status": last["status"],
        "started_at": _iso(started),
        "finished_at": _iso(last["finished_at"]),
        "age_hours": age_hours,
        "rows_written": last["rows_written"],
        "error_message": last["error_message"],
        "triggered_by": last["triggered_by"],
    }

    if last["status"] == "failed":
        reason = (f"Last run failed {age_hours}h ago "
                  f"(triggered by {last['triggered_by']}).")
        through = _data_supersedes(owned or [], started)
        if through:
            payload["status"] = "ok"
            payload["superseded"] = True
            payload["message"] = (reason + " Superseded — all its tables hold data "
                                  f"through {through}, written by a run job_runs never "
                                  "saw (the terminal command on this card).")
            return payload
        payload["status"] = "bad"
        payload["message"] = reason
        return payload

    if last["status"] == "running":
        # The runner's OWN abandonment threshold, not the cadence budget: a
        # weekly job whose thread died 2h ago would otherwise read "Running for
        # 2.1h" for six days. A server reload kills the Run Now thread before it
        # closes its row, so an open row past this age is a dead process — say
        # that, and let the tables prove whether the work still got done.
        #
        # ``corpse`` short-circuits the waiting: the row predates this process,
        # so no thread that exists can own it and it is dead NOW, one minute
        # after the reload or not. Kept separate from the age rule because only
        # Run Now rows can be proven this way (job_runner.row_is_a_corpse).
        stale_hours = job_runner.STALE_RUNNING_MINUTES / 60.0
        if corpse or (age_hours and age_hours > stale_hours):
            why = ("opened by a server generation that no longer exists — a reload "
                   "kills the Run Now thread mid-run" if corpse else
                   "abandoned past %.0fh, the same limit Run Now uses to unlock itself"
                   % stale_hours)
            reason = (f"Row opened {age_hours}h ago is still 'running' — the process "
                      f"died without closing it ({why}).")
            through = _data_supersedes(owned or [], started)
            if through:
                payload["status"] = "ok"
                payload["superseded"] = True
                payload["message"] = (reason + " Superseded — its tables hold data "
                                      f"through {through}, so a later run finished "
                                      "the work.")
                return payload
            payload["status"] = "bad"
            payload["message"] = reason
            return payload
        if budget and age_hours and age_hours > budget:
            payload["status"] = "bad"
            payload["message"] = (f"Still 'running' after {age_hours}h — the process "
                                  f"died without closing the row.")
        else:
            payload["status"] = "late"
            payload["message"] = f"Running for {age_hours}h."
        return payload

    if budget and age_hours is not None and age_hours > budget:
        payload["status"] = "bad" if age_hours > budget * 2 else "late"
        payload["message"] = (f"Last success {age_hours}h ago; budget is {budget}h "
                              f"({job['expected_schedule']}).")
        return payload

    if last["rows_written"] == 0:
        # A "success, nothing to write" is NOT a failed run: the ledger row is
        # green, the process worked, the source simply had no new observation.
        # So the failed/never-recorded rule ("the data must be dated on or AFTER
        # this run, to prove a LATER run rebuilt it") does not apply — the run is
        # already the proof. Requiring it here would strand every economic job:
        # ``report_date`` is the SOURCE's publication date, not a run timestamp,
        # so GuruFocus EY/PE legitimately top out at 2026-10-30 while a correct
        # scrape on 2026-11-01 writes 0 rows. Each table's own lag threshold
        # (its ``status``) is the real freshness guard, so pass ``since=None`` and
        # let only a genuinely stale table keep the card amber.
        through = _data_supersedes(owned or [], None)
        if through:
            payload["status"] = "ok"
            payload["superseded"] = True
            payload["message"] = ("0 rows written because nothing was missing — all its "
                                  f"tables hold data through {through}.")
            return payload
        payload["status"] = "late"
        payload["message"] = "Last run succeeded but reported 0 rows written."
        return payload

    payload["status"] = "ok"
    payload["message"] = f"Last run succeeded {age_hours}h ago."
    return payload


# ══════════════════════════════════════════════════════════════════════════════
#  Table side — "is the data fresh?"
# ══════════════════════════════════════════════════════════════════════════════
def _quote(ident: str) -> str:
    """Quote an identifier. Registry-owned names only — never client input."""
    return '"%s"' % ident.replace('"', '""')


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _in_list(values) -> str:
    return ", ".join(_lit(v) for v in values)


def _collect_table_specs() -> Dict[str, set]:
    """table -> set(date columns) across the registry, deduplicated.

    The same table is listed under several systems (prices appears 4 times);
    probing it once per system is the N+1 this replaces.
    """
    specs: Dict[str, set] = {}
    for system in registry.list_systems():
        for entry in system["tables"]:
            specs.setdefault(entry["table"], set())
            if entry["date_column"]:
                specs[entry["table"]].add(entry["date_column"])
    return specs


def _fetch_class_info(db: Session, tables, counter: Dict[str, int]) -> Dict[str, int]:
    """reltuples for the existing tables. Absent key ⇒ table missing here."""
    if not tables:
        return {}
    rows = _probe(db,
                  "SELECT c.relname, c.reltuples::bigint "
                  "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                  "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                  "AND c.relname IN (%s)" % _in_list(tables))
    counter["class"] += 1
    if rows is None:
        return {}
    return {r[0]: int(r[1]) for r in rows if r[0] is not None}


def _fetch_columns(db: Session, tables, counter: Dict[str, int]) -> Dict[str, set]:
    """table -> its real column names, for the subset we intend to probe."""
    if not tables:
        return {}
    rows = _probe(db,
                  "SELECT table_name, column_name FROM information_schema.columns "
                  "WHERE table_schema = 'public' AND table_name IN (%s)" % _in_list(tables))
    counter["columns"] += 1
    out: Dict[str, set] = {}
    for table, column in (rows or []):
        out.setdefault(table, set()).add(column)
    return out


def _fetch_latest_values(db: Session, pairs, columns: Dict[str, set],
                         counter: Dict[str, int]):
    """MAX(col) for every (table, col) pair, in chunked UNION ALL probes.

    Only pairs whose table AND column exist go into the statement: one unknown
    relation would abort the transaction and cost the whole chunk. A failed
    chunk is retried pair by pair, so one bad table cannot black out eleven
    others. Returns (values, probed).
    """
    out: Dict[tuple, Any] = {}
    probed = set()
    valid = [(t, c) for (t, c) in pairs if t in columns and c in columns[t]]
    for i in range(0, len(valid), _MAX_UNION_BRANCHES):
        part = valid[i:i + _MAX_UNION_BRANCHES]
        sql = " UNION ALL ".join(
            "SELECT %s AS tbl, %s AS col, MAX(%s) AS latest FROM %s"
            % (_lit(t), _lit(c), _quote(c), _quote(t)) for (t, c) in part
        )
        rows = _probe(db, sql)
        counter["latest_grouped"] += 1
        if rows is None:
            for (t, c) in part:
                solo = _probe(db, "SELECT MAX(%s) FROM %s" % (_quote(c), _quote(t)))
                counter["latest_fallback"] += 1
                if solo is not None:
                    probed.add((t, c))
                    out[(t, c)] = solo[0][0]
            continue
        for tbl, col, latest in rows:
            probed.add((tbl, col))
            out[(tbl, col)] = latest
    return out, probed


def _fetch_series_latest(db: Session, counter: Dict[str, int]) -> Dict[str, date]:
    """Newest report_date per indicator_code — ONE SELECT for every shared-table job.

    ``economic_indicators`` is written by nine registered jobs but the registry
    can attribute its table row to exactly one of them, so the aggregate freshness
    probe proves nothing for the other eight (manual:gurufocus included). Reading
    every series in one GROUP BY costs the build a single round trip however many
    jobs start sharing the table.
    """
    rows = _probe(db,
                  "SELECT %s, MAX(%s) FROM %s GROUP BY 1"
                  % (_quote(registry.SERIES_KEY_COLUMN),
                     _quote(registry.SERIES_DATE_COLUMN),
                     _quote(registry.SERIES_TABLE)))
    counter["series"] += 1
    if rows is None:
        return {}
    out: Dict[str, date] = {}
    for code, latest in ((r[0], _as_date(r[1])) for r in rows):
        if code and latest:
            out[str(code)] = latest
    return out


def _build_table(entry: Dict[str, Any], today: date, estimates: Dict[str, int],
                 columns: Dict[str, set], latest_values: Dict[tuple, Any],
                 probed: set) -> Dict[str, Any]:
    """Pure computation over the grouped probes — runs no queries itself."""
    table = entry["table"]
    col = entry["date_column"]
    max_lag = entry["max_lag_trading_days"]

    payload: Dict[str, Any] = {
        "table": table,
        "date_column": col,
        "max_lag_trading_days": max_lag,
        "source_job": entry["source_job"],
        "note": entry["note"],
        "verified": entry.get("verified", True),
        "latest": None,
        "lag_trading_days": None,
        "row_estimate": None,
        "status": "unknown",
        "message": "",
    }

    if table in estimates:
        payload["row_estimate"] = estimates[table]
    else:
        payload["status"] = "bad"
        payload["message"] = '"%s" is not in pg_class on this environment.' % table
        return payload

    if not col:
        payload["status"] = "unknown"
        payload["message"] = "No date column — freshness cannot be measured here."
        return payload

    if col not in columns.get(table, set()):
        payload["status"] = "bad"
        payload["message"] = ('"%s" has no column "%s" here — the registry is wrong '
                              'or this environment is behind.' % (table, col))
        return payload

    if (table, col) not in probed:
        payload["status"] = "unknown"
        payload["message"] = ("Freshness probe skipped or timed out (%d ms) — "
                              "run the SQL in Verify instead." % PROBE_TIMEOUT_MS)
        return payload

    latest = _as_date(latest_values.get((table, col)))
    payload["latest"] = latest.isoformat() if latest else None

    if latest is None:
        payload["status"] = "bad"
        payload["message"] = "Table is empty (or that column is entirely NULL)."
        return payload

    lag = registry.trading_days_between(latest, today)
    payload["lag_trading_days"] = lag

    if max_lag is None:
        payload["status"] = "unknown"
        payload["message"] = f"Newest row {latest} — {lag} trading day(s) behind; no threshold set."
        return payload
    if lag <= max_lag:
        payload["status"] = "ok"
        payload["message"] = f"Newest row {latest} — {lag} trading day(s) behind."
        return payload
    payload["status"] = "late" if lag <= max_lag + 3 else "bad"
    payload["message"] = (f"Newest row {latest} — {lag} trading days behind, "
                          f"threshold is {max_lag}.")
    return payload


def _series_evidence(codes: List[str], series: Dict[str, date], today: date,
                     max_lag: Optional[int]) -> Optional[Dict[str, Any]]:
    """A table-shaped evidence entry built from ONE job's own indicator codes.

    Shaped like a freshness row because that is what ``_data_supersedes`` reads
    (``latest`` + ``status``), so a job whose data lives in a shared table gets
    judged by exactly the same rule as a job that owns its table outright.

    Deliberately unforgiving: a declared series with no rows at all is a gap, not
    a clean bill of health, and the LOWEST of its dates is what counts — the same
    "prove the whole job ran" logic that stops one forward-dated breadth table
    from speaking for the daily ones.
    """
    if not codes or not series:
        return None                      # nothing declared, or the probe failed
    dates = [series.get(c) for c in codes]
    if any(d is None for d in dates):
        return None                      # a declared series was never written
    latest = min(dates)
    lag = registry.trading_days_between(latest, today)
    if max_lag is None:
        status = "unknown"               # nothing expected of it — still a date
    elif lag <= max_lag:
        status = "ok"
    else:
        status = "late" if lag <= max_lag + 3 else "bad"
    return {
        "table": "%s[%s]" % (registry.SERIES_TABLE, ", ".join(codes)),
        "date_column": registry.SERIES_DATE_COLUMN,
        "latest": latest.isoformat(),
        "lag_trading_days": lag,
        "status": status,
        "source_job": None,
        "row_estimate": None,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Snapshot builder + cache
# ══════════════════════════════════════════════════════════════════════════════
def _system_checks_sql(system: Dict[str, Any]) -> List[Dict[str, str]]:
    """Queries an admin can paste into psql to confirm what the panel claims."""
    out: List[Dict[str, str]] = []
    names = ", ".join("'%s'" % j["job_name"] for j in system["jobs"])
    if names:
        out.append({
            "label": "Recent runs of this system's jobs",
            "sql": "SELECT job_name, status, triggered_by, started_at, finished_at, "
                   "rows_written, LEFT(error_message, 120) AS err FROM job_runs "
                   "WHERE job_name IN (%s) ORDER BY started_at DESC LIMIT 20;" % names,
        })
    for entry in system["tables"]:
        col = entry["date_column"]
        if not col:
            continue
        table = entry["table"]
        out.append({
            "label": "%s — newest row" % table,
            "sql": 'SELECT MAX("%s") AS latest, COUNT(*) AS rows FROM "%s";' % (col, table),
        })
        if entry["source_job"]:
            out.append({
                "label": "%s — last 10 rows" % table,
                "sql": 'SELECT * FROM "%s" ORDER BY "%s" DESC LIMIT 10;' % (table, col),
            })
    return out


def _build_snapshot() -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int], bool]:
    """Everything the map needs, in a query count that does NOT depend on how
    many systems, jobs or tables are registered.

    Returns ``(systems, query_counter, ledger_missing)`` — the last flag is part
    of the payload because /schedule serves from this snapshot and must still be
    able to say "job_runs is not readable" rather than "never recorded".

    Data SELECTs per build:
      1  job_runs   — newest row per job_name (single DISTINCT ON)
      1  job_runs   — failures in the last 7 days (GROUP BY)
      1  job_runs   — rows still 'running' (GROUP BY)
      1  pg_class   — existence + reltuples for every registry table
      1  information_schema.columns — real columns of those tables
      1  economic_indicators — newest date per indicator_code (GROUP BY), the
                               evidence for jobs that share the table
      ⌈pairs/12⌉   — MAX(date) freshness, chunked UNION ALL (≈5 today)
    = ≈10 SELECTs, and because _arm() sets the timeout once for the whole build
    that is ≈10 round trips over the wire — not the 3× it used to cost when
    every probe rolled back and re-armed itself first, which against a remote
    Postgres was most of the 8 s the page took to open. The per-pair fallback
    only runs for the branches of a failed chunk.
    """
    now = datetime.now(timezone.utc)
    today = now.date()
    counter: Dict[str, int] = {"last_runs": 0, "failures": 0, "stuck": 0,
                               "class": 0, "columns": 0, "latest_grouped": 0,
                               "latest_fallback": 0, "series": 0}
    db = SessionLocal()
    try:
        # One pragma for the whole build — see _arm().
        _arm(db)
        last_runs = _fetch_last_runs(db)
        counter["last_runs"] += 1
        ledger_missing = last_runs is None
        runs = last_runs or {}
        failures = _fetch_counts(db, """
            SELECT job_name, COUNT(*) FROM job_runs
            WHERE status = 'failed' AND started_at > NOW() - INTERVAL '7 days'
            GROUP BY job_name
        """)
        counter["failures"] += 1
        stuck = _fetch_counts(db, """
            SELECT job_name, COUNT(*) FROM job_runs
            WHERE status = 'running' GROUP BY job_name
        """)
        counter["stuck"] += 1

        # Table layer: dedupe across systems first, then probe once.
        specs = _collect_table_specs()
        table_names = sorted(specs)
        estimates = _fetch_class_info(db, table_names, counter)
        columns = _fetch_columns(db, table_names, counter)
        pairs = [(t, c) for t in table_names for c in sorted(specs[t])]
        latest_values, probed = _fetch_latest_values(db, pairs, columns, counter)

        # Shared-table evidence, read once for every system: which indicator_code
        # reaches how far. series_lag is the table's own threshold, so a scoped
        # entry is held to the same freshness bar as the aggregate one.
        series = _fetch_series_latest(db, counter)
        series_lag = next(
            (e["max_lag_trading_days"] for s in registry.list_systems()
             for e in s["tables"] if e["table"] == registry.SERIES_TABLE), None)

        systems: Dict[str, Dict[str, Any]] = {}
        consumers = registry.job_consumers()
        for system in registry.list_systems():
            # Tables BEFORE jobs, on purpose: the table verdicts are the evidence
            # a stale ledger line is judged against (_data_supersedes). Both are
            # pure computation over the probes above, so the query count is
            # unchanged — only the order of the two loops.
            tables = [
                _build_table(entry, today, estimates, columns, latest_values, probed)
                for entry in system["tables"]
            ]
            owned_by_job: Dict[str, List[Dict[str, Any]]] = {}
            for t in tables:
                if t["source_job"]:
                    owned_by_job.setdefault(t["source_job"], []).append(t)
            # A job that writes a table shared with others owns no table ROW, so
            # nothing above can overturn its ledger line. Hand it its own series
            # evidence here, before the jobs are judged.
            for job in system["jobs"]:
                scoped = _series_evidence(registry.series_evidence(job["job_name"]),
                                          series, today, series_lag)
                if scoped:
                    owned_by_job.setdefault(job["job_name"], []).append(scoped)
            jobs = []
            for job in system["jobs"]:
                last = runs.get(job["job_name"])
                jobs.append(_build_job(
                    job, last,
                    failures.get(job["job_name"], 0),
                    stuck.get(job["job_name"], 0),
                    ledger_missing, now,
                    owned_by_job.get(job["job_name"], []),
                    # Proven-dead rather than merely old: costs no extra query,
                    # because _ACTIVE and BOOT_AT are already in this process.
                    corpse=job_runner.row_is_a_corpse(
                        job["job_name"],
                        (last or {}).get("started_at"),
                        (last or {}).get("triggered_by"))))
            # Reverse lineage: if this job stops, these are the pages that go
            # stale. Computed from the registry (table.pages → job.writes).
            for j in jobs:
                j["pages"] = consumers.get(j["job_name"], [])
            verdicts = [j["status"] for j in jobs if j["status"] not in _NEUTRAL]
            verdicts += [t["status"] for t in tables if t["status"] not in _NEUTRAL]
            systems[system["id"]] = {
                "id": system["id"],
                "name": system["name"],
                "blurb": system["blurb"],
                "in_navbar": system.get("in_navbar", False),
                "navbar_label": system.get("navbar_label"),
                "health": _worst(*verdicts) if verdicts else "ok",
                "built": system["built"],
                "pages": system["pages"],
                "jobs": jobs,
                "tables": tables,
                "issues": system["issues"],
                "checks_sql": _system_checks_sql(system),
            }
        return systems, counter, ledger_missing
    finally:
        db.close()


def _rebuild() -> None:
    """Refresh the snapshot off the request path (the stale-while-revalidate arm).

    Builds OUTSIDE the lock, then swaps the result in under it -- so the thread
    never holds the lock for seconds while a page load waits on it.
    """
    global _REBUILDING
    try:
        systems, queries, ledger_missing = _build_snapshot()
        with _lock:
            _CACHE["systems"], _CACHE["queries"], _CACHE["error"] = systems, queries, None
            _CACHE["ledger_missing"] = ledger_missing
            _CACHE["built_at"] = datetime.now(timezone.utc)
    except Exception as e:                       # keep serving the last good snapshot
        logger.exception("system-map background rebuild failed")
        with _lock:
            _CACHE["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        _REBUILDING = False


def _get_snapshot(force: bool = False) -> Dict[str, Any]:
    """The cached snapshot, with stale-while-revalidate. Building never raises.

    · fresh          -> return the cache (0 SQL)
    · stale          -> return the cache NOW and rebuild on a daemon thread, so the
                        60 s poll never makes an admin pay for a build; the panels
                        simply pick up the new verdicts on the next tick
    · cold / force   -> build here. Cold because there is nothing to show yet, and
                        force because "Refresh" means *now* -- the button is
                        disabled until the fresh payload arrives
    A failed build keeps the last good data and records the error for the banner.
    """
    global _REBUILDING
    with _lock:
        built_at = _CACHE["built_at"]
        fresh = built_at and (datetime.now(timezone.utc) - built_at) < timedelta(seconds=CACHE_TTL_SECONDS)
        if fresh and not force:
            return _CACHE
        if built_at and not force:
            if not _REBUILDING:
                _REBUILDING = True
                threading.Thread(target=_rebuild, name="system-map-rebuild", daemon=True).start()
            return _CACHE
        try:
            (_CACHE["systems"], _CACHE["queries"],
             _CACHE["ledger_missing"]) = _build_snapshot()
            _CACHE["error"] = None
        except Exception as e:
            logger.exception("system-map snapshot failed")
            _CACHE["error"] = "%s: %s" % (type(e).__name__, e)
            _CACHE["queries"] = {}
        if not _CACHE["systems"] and _CACHE["error"]:
            # Nothing usable at all: hand back an empty map rather than a 500.
            _CACHE["systems"] = {s["id"]: _degraded_system(s, _CACHE["error"])
                                 for s in registry.list_systems()}
        _CACHE["built_at"] = datetime.now(timezone.utc)
        return _CACHE


def _degraded_system(system: Dict[str, Any], error: str) -> Dict[str, Any]:
    """Registry-only view, with every live check marked unverified."""
    return {
        "id": system["id"],
        "name": system["name"],
        "blurb": system["blurb"],
        "in_navbar": system.get("in_navbar", False),
        "navbar_label": system.get("navbar_label"),
        "health": "unknown",
        "built": system["built"],
        "pages": system["pages"],
        "jobs": [],
        "tables": [],
        "issues": system["issues"],
        "checks_sql": [],
        "unavailable": error,
    }


def _generated(raw: Dict[str, Any]) -> str:
    return raw["built_at"].isoformat() if raw.get("built_at") else None


def _query_summary(raw: Dict[str, Any]) -> Dict[str, Any]:
    """How many SELECTs the build actually ran — shown in the page footer so a
    future N+1 cannot be added to this endpoint unnoticed."""
    queries = raw.get("queries") or {}
    selects = sum(queries.values())
    return {"selects": selects, "by_layer": queries,
            "note": "each SELECT is preceded by its own SET LOCAL "
                    "statement_timeout pragma"}


# ══════════════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════════════
def _systems_rows(systems: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The sidebar list: registry summaries joined with the snapshot verdicts."""
    rows = []
    for meta in registry.system_summaries():
        live = systems.get(meta["id"]) or {}
        rows.append({
            **meta,
            "health": live.get("health", "unknown"),
            "jobs_bad": sum(1 for j in live.get("jobs", []) if j["status"] == "bad"),
            "jobs_late": sum(1 for j in live.get("jobs", []) if j["status"] == "late"),
            "tables_bad": sum(1 for t in live.get("tables", []) if t["status"] == "bad"),
            "tables_late": sum(1 for t in live.get("tables", []) if t["status"] == "late"),
        })
    return rows


def _envelope(raw: Dict[str, Any]) -> Dict[str, Any]:
    """The provenance fields every read on this router stamps onto its payload."""
    return {
        "generated_at": _generated(raw),
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "registry_file": REGISTRY_FILE,
        "db_queries": _query_summary(raw),
        "build_error": raw.get("error"),
    }


@router.get("/systems", summary="System Map — sidebar list with live health")
def get_systems(refresh: bool = False) -> Dict[str, Any]:
    """Every registered system, its static description and a health verdict.

    health = worst status among the system's tracked jobs and measurable tables.
    The Runbook page asks for this plus its detail plus the lineage; see
    ``get_runbook`` for the single-request form.
    """
    raw = _get_snapshot(force=refresh)
    return {"systems": _systems_rows(raw["systems"]), **_envelope(raw)}


@router.get("/systems/{system_id}", summary="System Map — full Runbook for one system")
def get_system_detail(system_id: str, refresh: bool = False) -> Dict[str, Any]:
    if registry.get_system(system_id) is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown system '%s'. Known ids: %s" % (
                system_id, ", ".join(s["id"] for s in registry.list_systems())),
        )
    raw = _get_snapshot(force=refresh)
    live = raw["systems"].get(system_id) or _degraded_system(
        registry.get_system(system_id), raw.get("error") or "snapshot unavailable")
    return {**live, **_envelope(raw), "global_checks_sql": registry.checks_sql()[:4]}


@router.get("/pipelines", summary="scraper → table → pages map (legacy /admin tab)")
def get_pipelines(all_tables: bool = False) -> List[Dict[str, Any]]:
    """Serves the map that used to be a hand-maintained array in the frontend.

    ``all_tables=true`` adds reference/seeded/user tables that have no writer.
    Runs ZERO queries — the registry is pure Python.
    """
    rows = registry.pipelines(include_unwired=all_tables)
    for row in rows:
        if row.get("warn") is None:
            row.pop("warn", None)
    return rows


# ════════════════════════════════════════════════════════════════════════════
#  Schedule tab + gap scanner (both read-only)
# ════════════════════════════════════════════════════════════════════════════
def _scan_one_table(db: Session, t: Dict[str, Any], window_days: int) -> Dict[str, Any]:
    """One table → newest date, trading-day lag, and (daily tables only) an
    approximate missing-trading-day count inside the recent window.

    Two bounded, indexed SELECTs (MAX on the date column; COUNT(DISTINCT) over a
    date range). Any failure degrades to 'unknown'/'missing' with a message — this
    never raises, so one absent table cannot 500 the whole scan. Holidays are NOT
    excluded (registry note), so the gap count is approximate and only flagged past
    a small threshold.
    """
    table, col = t["table"], t["date_column"]
    finding: Dict[str, Any] = {
        "table": table, "date_column": col, "cadence": t["cadence"],
        "responsible_job": t["responsible_job"], "responsible_file": t["responsible_file"],
        "fix_commands": t["fix_commands"], "latest": None, "lag_trading_days": None,
        "distinct_days": None, "missing_days": None, "status": "unknown", "message": "",
    }
    rows = _probe(db, "SELECT MAX(%s) FROM %s" % (_quote(col), _quote(table)))
    if rows is None:
        finding["message"] = "probe failed — table/column missing or timed out"
        return finding
    latest = _as_date(rows[0][0]) if rows and rows[0] else None
    if latest is None:
        finding["status"] = "missing"
        finding["message"] = "no readable date rows — the table is empty"
        return finding
    finding["latest"] = latest.isoformat()
    today = date.today()
    finding["lag_trading_days"] = registry.age_in_trading_days(latest, today)

    if t["cadence"] == "daily" and t.get("count_gaps", True):
        window_start = today - timedelta(days=window_days)
        crows = _probe(
            db,
            "SELECT COUNT(DISTINCT %s) FROM %s WHERE %s >= :start"
            % (_quote(col), _quote(table), _quote(col)),
            start=window_start,
        )
        if crows and crows[0][0] is not None:
            distinct = int(crows[0][0])
            finding["distinct_days"] = distinct
            expected = registry.trading_days_between(window_start, latest) + 1
            finding["missing_days"] = max(0, expected - distinct)

    lag = finding["lag_trading_days"] or 0
    missing = finding["missing_days"]
    if t["cadence"] == "daily" and missing is not None and missing >= 3:
        finding["status"] = "gap"
        finding["message"] = (
            f"\u2248{missing} trading days missing in the last {window_days} days "
            f"(holidays not excluded). Newest row {latest.isoformat()}.")
    elif lag > _STALE_LAG_BY_CADENCE.get(t["cadence"], 60):
        finding["status"] = "late"
        finding["message"] = (f"Newest {col} is {latest.isoformat()} — "
                              f"{lag} trading days behind (budget "
                              f"{_STALE_LAG_BY_CADENCE.get(t['cadence'], 60)}).")
    else:
        finding["status"] = "ok"
        finding["message"] = f"Fresh — newest {latest.isoformat()}."
    return finding


@router.get("/pages", summary="Page ↔ script lineage — navbar spine, both directions")
def get_page_lineage(refresh: bool = False) -> Dict[str, Any]:
    """Which scripts feed each page, and which pages depend on each script.

    The navbar itself lives in the frontend (components/Navbar.tsx and
    components/MarketSidebar.tsx): this returns the DATA half keyed by router
    pathname, and the frontend joins its own menu labels onto it. A spine page
    that is absent here has no registered lineage — the panel shows that as a
    gap instead of pretending the page needs nothing.

    Costs no SQL: it inverts the snapshot /systems already built, so the page
    list and the job verdicts cannot disagree with the Runbook.
    """
    raw = _get_snapshot(force=refresh)
    return _lineage_payload(raw)


@router.get("/runbook", summary="One request for the whole page: list + details + lineage")
def get_runbook(system: Optional[str] = None,
                refresh: bool = False) -> Dict[str, Any]:
    """The page's single round trip: sidebar, every system's detail, the lineage.

    /systems, /systems/{id} and /pages used to fire together on mount. All three
    read the same cached snapshot, so they never disagreed — but they DID queue:
    _get_snapshot() holds the build lock, so the second and third request sat
    waiting for the first to finish building, and against a remote Postgres that
    is what made opening the page cost seconds for no extra information.

    One payload removes the queue and makes switching systems free: every
    system's full entry ships in ``details``, so clicking another row in the
    sidebar renders from data already in the browser — no request, no wait.

    An unknown ``system`` falls back to the first registered system instead of
    404ing: a stale ?system= bookmark must still open the Runbook.
    """
    raw = _get_snapshot(force=refresh)
    systems = raw["systems"]
    env = _envelope(raw)
    checks = registry.checks_sql()[:4]
    known = [m["id"] for m in registry.system_summaries()]
    return {
        "systems": _systems_rows(systems),
        # The shared envelope is stamped into each entry so the frontend keeps
        # using the SystemMapDetail shape it already has.
        "details": {sid: {**live, **env, "global_checks_sql": checks}
                    for sid, live in systems.items()},
        "active": system if system in systems else (known[0] if known else None),
        "lineage": _lineage_payload(raw),
        **env,
    }


def warm_snapshot(delay_seconds: float = 1.5) -> None:
    """Build the snapshot off the request path so the first page open is instant.

    Called from the app startup hook. It never blocks startup and never raises:
    the build runs in a daemon thread a moment after boot (a cold build against
    the live Render Postgres is ~11 round trips, and uvicorn must not wait for
    that). If the thread dies or the DB is unreachable, the endpoint simply
    builds on demand exactly as it always did.
    """
    import os
    if os.environ.get("PYTEST_CURRENT_TEST"):        # keep tests silent & hermetic
        return

    def _run() -> None:
        try:
            time.sleep(delay_seconds)
            _get_snapshot()
            logger.info("system-map snapshot warmed for the first page open")
        except Exception as e:                        # pragma: no cover - best effort
            logger.warning("system-map warm-up skipped: %s", e)

    threading.Thread(target=_run, name="system-map-warm", daemon=True).start()


def _lineage_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    """The snapshot re-keyed by page path: feeders per page, pages per script.

    Pure Python over the cached snapshot — no SQL — so /pages and /runbook can
    never disagree with the Runbook panels, and 137 pages cost zero queries.
    """
    systems = raw["systems"]

    live_tables: Dict[Tuple[str, str], Dict[str, Any]] = {}
    live_jobs: Dict[str, Dict[str, Any]] = {}
    for sid, sys in systems.items():
        for t in sys["tables"]:
            live_tables[(sid, t["table"])] = t
        for j in sys["jobs"]:
            live_jobs.setdefault(j["job_name"], j)
    # Registry commands, for the rare job no live snapshot carries (untracked
    # entry points are still copy-pasteable from a terminal).
    cmd_index = {j["job_name"]: j["commands"] for j in registry.schedule_jobs()}

    pages = []
    for path, row in sorted(registry.page_lineage().items()):
        sid = row["systems"][0]["id"] if row["systems"] else None
        tables = []
        for t in row["tables"]:
            live = live_tables.get((t.get("system_id") or sid, t["table"])) or {}
            tables.append({
                **t,
                "status": live.get("status", "unknown"),
                "latest": live.get("latest"),
                "lag_trading_days": live.get("lag_trading_days"),
                "row_estimate": live.get("row_estimate"),
                "measurable": live.get("date_column", t["date_column"]) is not None,
            })
        jobs = []
        for j in row["jobs"]:
            live = live_jobs.get(j["job_name"]) or {}
            last = live.get("last_run") or {}
            jobs.append({
                **j,
                "status": live.get("status", "unknown"),
                "message": live.get("message", ""),
                "last_run_at": last.get("started_at"),
                "last_run_status": last.get("status"),
                "rows_written": last.get("rows_written"),
                "commands": live.get("commands") or cmd_index.get(j["job_name"]) or [],
            })
        verdicts = ([x["status"] for x in jobs if x["status"] not in _NEUTRAL]
                    + [x["status"] for x in tables if x["status"] not in _NEUTRAL])
        pages.append({
            "path": path,
            "systems": row["systems"],
            "jobs": jobs,
            "tables": tables,
            "no_writer_tables": row["no_writer_tables"],
            "has_feeder": bool(jobs),
            "status": _worst(*verdicts) if verdicts else "ok",
        })

    consumers = registry.job_consumers()
    scripts = []
    for job_name, paths in sorted(consumers.items()):
        live = live_jobs.get(job_name) or {}
        scripts.append({
            "job_name": job_name,
            "file": live.get("file", ""),
            "cadence": live.get("expected_schedule", ""),
            "status": live.get("status", "unknown"),
            "writes": live.get("writes", []),
            "pages": paths,
            "commands": live.get("commands") or [],
            "runnable": bool((live.get("run") or {}).get("runnable")),
        })
    registered = {p["path"] for p in pages}
    return {
        "pages": pages,
        "scripts": scripts,
        "counts": {"pages": len(registered), "scripts": len(scripts)},
        "registry_file": REGISTRY_FILE,
        "generated_at": _generated(raw),
        "snapshot_error": raw.get("error"),
        "note": "Page → feeders comes from registry.page_lineage(); a table with "
                "no source_job is listed under no_writer_tables instead of being "
                "counted as a feeder. Navbar labels come from the frontend, so "
                "adding a menu item needs no registry edit — but a menu item with "
                "no lineage here shows up as an unregistered page.",
    }


@router.get("/schedule", summary="Schedule tab — every job with cadence/role/storage/commands")
def get_schedule(refresh: bool = False) -> Dict[str, Any]:
    """The /admin 'What to Run & When' catalog, served from the SAME snapshot the
    Runbook reads. ``registry.schedule_jobs()`` and the map's job lists are the
    same 28 jobs (verified equal), so sharing one build loses nothing.

    That sharing is the fix: this surface used to open its own session and read
    ONLY job_runs, so it printed the raw ledger row ("failed") beside a Runbook
    that had already overturned it with fresh tables. ``verdict`` is the rule
    (ledger + table freshness); ``last_run`` stays the untouched ledger row, so
    an admin can still read what the ledger literally says.

    Cadence/role/storage/commands come straight from the registry; ``runnable`` is
    the ONLY gate for a working Run Now — heavy scripts without a registered
    callable render as Copy-command only on the frontend."""
    raw = _get_snapshot(force=refresh)
    live_by_name = {j["job_name"]: j
                    for sys_ in raw["systems"].values() for j in sys_["jobs"]}
    out: List[Dict[str, Any]] = []
    for job in registry.schedule_jobs():
        cap = _run_capability(job["job_name"])
        live = live_by_name.get(job["job_name"]) or {}
        out.append({
            **job,
            "cli_absence_reason": registry.cli_absence_reason(job),
            "runnable": cap["runnable"],
            "delegated_to": cap.get("delegated_to"),
            "run_reason": cap.get("reason"),
            # The derived verdict, and the ledger row it was judged from.
            "verdict": live.get("status", "unknown"),
            "verdict_message": live.get("message", ""),
            "superseded": bool(live.get("superseded", False)),
            "last_run": live.get("last_run"),
        })
    return {
        "jobs": out,
        "cadences": list(registry.CADENCES),
        "roles": list(registry.ROLES),
        "storages": list(registry.STORAGES),
        "ledger_missing": bool(raw.get("ledger_missing")),
        "generated_at": _generated(raw),
        "registry_file": REGISTRY_FILE,
    }


@router.get("/integrity/scan", summary="Gap scanner — read-only freshness + missing-day count")
def scan_integrity(window_days: int = 90) -> Dict[str, Any]:
    """'Check Data Integrity & Scan Gaps'. Read-only over the vital tables; each
    finding names the responsible job + its exact fix command (from the registry).
    Runs ZERO writes and never 500s — a dead table degrades to one 'unknown' row."""
    try:
        window_days = max(7, min(365, int(window_days)))
    except (TypeError, ValueError):
        window_days = 90
    targets = registry.integrity_targets()
    findings: List[Dict[str, Any]] = []
    db = SessionLocal()
    try:
        _arm(db)
        for t in targets:
            findings.append(_scan_one_table(db, t, window_days))
    finally:
        db.close()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": window_days,
        "findings": findings,
        "gaps": sum(1 for f in findings if f["status"] in ("gap", "late", "missing")),
        "registry_file": REGISTRY_FILE,
    }


# ── Run Now (Phase 5) ─────────────────────────────────────────────────────────
# These are the only two non-read-only things on this page, and neither writes
# from the request thread: the job runs in a daemon thread and job_tracking.py
# owns its own short sessions. Auth is enforced twice on purpose — once at the
# mount in app/main.py (admin_dependencies) and once here, so re-mounting this
# router without the guard cannot expose a trigger endpoint.
@router.post("/jobs/{job_name}/run", summary="Run Now — start one tracked job")
def run_job_now(job_name: str, current_admin=Depends(get_current_admin)) -> Dict[str, Any]:
    """Start `job_name` in the background and return immediately (202-shaped).

    Poll the result with GET /jobs/{job_name}/status, or just reload the panel:
    the Last run column reads the same job_runs row the thread writes.
    """
    if not job_runner.is_runnable(job_name):
        if job_name == "daily_market_update":
            raise HTTPException(
                status_code=400,
                detail="daily_market_update must be started with POST %s "
                       "(it has its own DB mutex)." % _PIPELINE_RUN_ENDPOINT)
        raise HTTPException(
            status_code=404,
            detail="No Run Now callable for '%s'. Runnable: %s" % (
                job_name, ", ".join(job_runner.runnable_names())),
        )

    verdict = job_runner.start(job_name)
    if verdict["status"] == "already_running":
        raise HTTPException(status_code=409, detail=verdict)
    # 202 through the body rather than a status_code= argument: the router is
    # shared and the frontend reads `status` anyway.
    return {
        **verdict,
        "requested_by": getattr(current_admin, "email", None) or getattr(current_admin, "id", None),
        "poll_url": "/api/admin/system-map/jobs/%s/status" % job_name,
    }


@router.get("/jobs/{job_name}/status", summary="Run Now — latest job_runs row for one job")
def get_job_status(job_name: str) -> Dict[str, Any]:
    """1 SELECT, newest row per job_name via ix_job_runs_name_started, read-only,
    3 s statement timeout (app/services/job_runner.py)."""
    payload = job_runner.status_for(job_name)
    _expire_snapshot_after(last_run_finished(payload))
    return payload


def last_run_finished(payload: Dict[str, Any]) -> Optional[datetime]:
    """The terminal timestamp of the polled run, or None while it is still open."""
    last = payload.get("last_run") or {}
    if last.get("status") not in ("success", "failed"):
        return None
    raw = last.get("finished_at") or last.get("started_at")
    try:
        return datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _expire_snapshot_after(finished_at: Optional[datetime]) -> None:
    """Drop the cached snapshot when a run just reached a terminal state.

    The Runbook serves a 60 s stale-while-revalidate snapshot, so without this a
    successful Run Now keeps showing the PRE-run verdict for up to a minute after
    /status already reports the fresh row — the card looked stuck on the old
    failure. Clearing ``built_at`` makes the next /systems or /schedule read
    rebuild cold; the poll only reads the ledger, so this stays cheap.
    """
    if finished_at is None:
        return
    built_at = _CACHE.get("built_at")
    if built_at and finished_at <= built_at:
        return                                   # snapshot already covers this run
    with _lock:
        _CACHE["built_at"] = None
