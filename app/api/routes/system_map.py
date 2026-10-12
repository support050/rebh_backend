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
  3. One build per ``CACHE_TTL_SECONDS`` for the whole map. Opening the page
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
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import system_registry as registry
from app.api.deps import get_current_admin
from app.core.database import SessionLocal
from app.services import job_runner

router = APIRouter(prefix="/admin/system-map", tags=["Admin — System Map"])
logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60
REGISTRY_FILE = "backend/app/system_registry.py"

# Every probe runs inside its own transaction with this timeout, so one slow
# query cannot hang the admin page (and cannot become a full scan on a big
# table). MAX() on an indexed date column is an index scan; if an index is
# missing in this environment the probe dies at 2.5 s and degrades to 'unknown'
# instead of stalling the request.
PROBE_TIMEOUT_MS = 2500
# Branches per UNION ALL freshness probe. Chunked so one timeout (or one bad
# column) costs a chunk, not the whole map.
_MAX_UNION_BRANCHES = 12

# daily_market_update is deliberately NOT dispatched here: it has its own
# endpoint with a DB mutex and a 409 on concurrent runs. The UI calls that.
_PIPELINE_RUN_ENDPOINT = "/api/admin/pipeline/daily-market-update/run"

# Statuses that must not drag a system's health down.
_NEUTRAL = ("ok", "unknown", "untracked")
_RANK = {"ok": 0, "untracked": 0, "unknown": 0, "late": 1, "bad": 2}

_lock = threading.Lock()
_CACHE: Dict[str, Any] = {"built_at": None, "systems": {}, "error": None,
                          "queries": {}}


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


def _probe(db: Session, sql: str, timeout_ms: int = PROBE_TIMEOUT_MS, **params) -> Optional[List[tuple]]:
    """One SELECT → rows, or None when it failed.

    Two things happen here on purpose:
      · db.rollback() first ⇒ every probe gets its OWN short transaction, so
        `SET LOCAL statement_timeout` applies to exactly this statement and is
        gone when the transaction ends (nothing leaks into the pooled
        connection).
      · the rollback on failure: a failed statement leaves the session in an
        aborted transaction, and without this the *next* probe would fail too
        and the whole snapshot would light up red for one missing table.
    """
    try:
        db.rollback()
        db.execute(text("SET LOCAL statement_timeout = :ms"), {"ms": timeout_ms})
        return [tuple(r) for r in db.execute(text(sql), params).fetchall()]
    except SQLAlchemyError as e:
        db.rollback()
        logger.debug("system-map probe failed: %s", e)
        return None
    except Exception as e:                              # driver-level surprises
        db.rollback()
        logger.debug("system-map probe failed (non-SQLAlchemy): %s", e)
        return None


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


def _build_job(job: Dict[str, Any], last: Optional[Dict[str, Any]],
               failures_7d: int, stuck_running: int,
               ledger_missing: bool, now: datetime) -> Dict[str, Any]:
    """One job entry + its verdict.

    Rules (shown in the UI legend, derived from the Runbook spec):
      · never recorded             → bad   (no row since job_runs existed)
      · last run failed            → bad
      · last run still 'running'   → late, or bad once past the age budget
                                     (a killed process leaves the row open)
      · success older than budget  → late up to 2× budget, bad beyond
      · success with 0 rows        → late (the process worked, wrote nothing)
      · untracked entry            → 'untracked'; never affects system health
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
        "failures_7d": failures_7d,
        "stuck_running": stuck_running,
        "last_run": None,
        "status": "unknown",
        "message": "",
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
        payload["status"] = "bad"
        payload["message"] = (f"Last run failed {age_hours}h ago "
                              f"(triggered by {last['triggered_by']}).")
        return payload

    if last["status"] == "running":
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


def _build_snapshot() -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    """Everything the map needs, in a query count that does NOT depend on how
    many systems, jobs or tables are registered.

    Data SELECTs per build:
      1  job_runs   — newest row per job_name (single DISTINCT ON)
      1  job_runs   — failures in the last 7 days (GROUP BY)
      1  job_runs   — rows still 'running' (GROUP BY)
      1  pg_class   — existence + reltuples for every registry table
      1  information_schema.columns — real columns of those tables
      ⌈pairs/12⌉   — MAX(date) freshness, chunked UNION ALL (≈5 today)
    = ≈10 SELECTs. Each is preceded by its own `SET LOCAL statement_timeout`,
    so ≈2× that many statements hit the wire, and the timeout pragmas are not
    queries. The per-pair fallback only runs for the branches of a failed chunk.
    """
    now = datetime.now(timezone.utc)
    today = now.date()
    counter: Dict[str, int] = {"last_runs": 0, "failures": 0, "stuck": 0,
                               "class": 0, "columns": 0, "latest_grouped": 0,
                               "latest_fallback": 0}
    db = SessionLocal()
    try:
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

        systems: Dict[str, Dict[str, Any]] = {}
        for system in registry.list_systems():
            jobs = [
                _build_job(job, runs.get(job["job_name"]),
                           failures.get(job["job_name"], 0),
                           stuck.get(job["job_name"], 0),
                           ledger_missing, now)
                for job in system["jobs"]
            ]
            tables = [
                _build_table(entry, today, estimates, columns, latest_values, probed)
                for entry in system["tables"]
            ]
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
        return systems, counter
    finally:
        db.close()


def _get_snapshot(force: bool = False) -> Dict[str, Any]:
    """The cached snapshot. Building never raises — a failure keeps the last good data."""
    with _lock:
        built_at = _CACHE["built_at"]
        fresh = built_at and (datetime.now(timezone.utc) - built_at) < timedelta(seconds=CACHE_TTL_SECONDS)
        if fresh and not force:
            return _CACHE
        try:
            _CACHE["systems"], _CACHE["queries"] = _build_snapshot()
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
@router.get("/systems", summary="System Map — sidebar list with live health")
def get_systems(refresh: bool = False) -> Dict[str, Any]:
    """Every registered system, its static description and a health verdict.

    health = worst status among the system's tracked jobs and measurable tables.
    """
    raw = _get_snapshot(force=refresh)
    systems = raw["systems"]
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
    return {
        "systems": rows,
        "generated_at": _generated(raw),
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "registry_file": REGISTRY_FILE,
        "db_queries": _query_summary(raw),
        "build_error": raw.get("error"),
    }


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
    return {
        **live,
        "generated_at": _generated(raw),
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "registry_file": REGISTRY_FILE,
        "global_checks_sql": registry.checks_sql()[:4],
        "db_queries": _query_summary(raw),
        "build_error": raw.get("error"),
    }


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
    return job_runner.status_for(job_name)
