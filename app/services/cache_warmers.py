"""
Cache warm-up suppliers (Part 2 / Phase 4) — the hot keys of a namespace, rebuilt
right after the job that made them stale, BEFORE the rest of the namespace is
evicted.

Why these exist
---------------
Every daily job ends by clearing a namespace (app/core/cache_bust.py), so the
next reader finds nothing cached and pays for the whole page. On the heavy
namespaces that is the worst possible moment: the engine recomputes XBRL
statements and the terminal services run multi-query aggregations, and the first
five visitors get the five identical computations that single-flight then has to
serialise. Pre-computing the handful of keys that the next reader will actually
ask for removes the gap entirely for the pages people open first.

What is deliberately NOT warmed
-------------------------------
`rebh:breadth:*`. Its payload shapes live inside the route handlers (each one
slices the single stored snapshot row), so warming them would mean re-implement
nine response shapes here — and a silent drift between the two is exactly the
parity bug this codebase has been audited for twice. The breadth payloads are
also slices of one 8 MB JSONB row: shipping that row from a job running on a
laptop over the home↔Render uplink (see job_runner's concurrency-cap note) costs
more than the one cold query single-flight already reduces it to.

Contract
--------
A warmer is a generator of ``(key, value_or_callable, ttl)`` items:

  * the key comes from the SAME helper the route uses, so a warmed key is the
    key a reader will ask for — never a guess;
  * the TTL is the SAME constant the route passes to cache_read_through, so a
    warm entry expires no later (and no sooner) than one written by a reader;
  * a callable value is computed by refresh_namespace under WARM_UP_PER_KEY_TIMEOUT_SECONDS,
    which is what keeps one slow symbol from holding the eviction hostage.

Nothing here touches Redis: app/core/cache_helpers.refresh_namespace owns the
connection, so a warm-up running in a worker thread stays on its own event loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Dict, Iterable, List, Tuple

from sqlalchemy import text

from app.core.cache_config import (
    CACHE_TTL_REBH_ENGINE,
    CACHE_TTL_TERMINAL_DAILY,
    CACHE_TTL_TERMINAL_QUARTERLY,
    WARM_UP_TOP_SYMBOLS,
)
from app.core.cache_helpers import (
    make_rebh_engine_key,
    make_terminal_all_ratios_key,
    make_terminal_audit_summary_key,
    make_terminal_company_fundamental_key,
    make_terminal_market_machine_key,
    make_terminal_quant_lab_key,
)

logger = logging.getLogger(__name__)

# Read-only guard on the hot-symbol ranking: a warm-up must never be the reason
# a job finishes late.
_TOP_SYMBOLS_TIMEOUT_MS = 5000
_TOP_SYMBOLS_LIMIT_CAP = 25


def top_symbols(limit: int = WARM_UP_TOP_SYMBOLS) -> List[str]:
    """The biggest companies on the most recent priced session.

    Market cap on the latest date is the cheapest defensible "hot" proxy this
    schema already indexes (idx_prices_date_desc), and it is stable across a
    night's run. Any failure yields an empty list, which simply means "no
    per-symbol warm-up" — the fixed page keys still get warmed.
    """
    count = max(0, min(int(limit or 0), _TOP_SYMBOLS_LIMIT_CAP))
    if not count:
        return []
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("SET LOCAL statement_timeout = :ms"),
                   {"ms": _TOP_SYMBOLS_TIMEOUT_MS})
        rows = db.execute(text(
            "SELECT symbol FROM prices "
            "WHERE date = (SELECT MAX(date) FROM prices) "
            "AND market_cap IS NOT NULL "
            "ORDER BY market_cap DESC NULLS LAST LIMIT :n"
        ), {"n": count}).fetchall()
        db.commit()
        return [str(r[0]) for r in rows if r[0]]
    except Exception as e:                            # noqa: BLE001 - warm-up is optional
        try:
            db.rollback()
        except Exception:                             # noqa: BLE001
            pass
        logger.warning("cache_warmers: hot symbol list unavailable (%s)",
                       type(e).__name__)
        return []
    finally:
        db.close()


def warm_terminal(top: int = WARM_UP_TOP_SYMBOLS) -> Iterable[Tuple[str, Callable, int]]:
    """Terminal pages: four board-level keys plus the top-N company overviews.

    Lazy imports on purpose — these services pull the DB layer and the terminal
    analysis modules, and a warmer must not make importing this file expensive
    for the thread that only needs the registry.
    """
    from app.services.terminal.market_machine_service import get_market_machine_data
    from app.services.terminal.quant_lab_service import (
        get_all_ratios_data, get_quant_lab_data,
    )
    from app.services.terminal.forensic_service import (
        get_audit_summary_data, get_company_unified_page_data,
    )

    # (key helper, service call, TTL) — each pair mirrors its route 1:1
    # (app/api/routes/rebh_engine.py, TERMINAL SUITE ENDPOINTS).
    yield make_terminal_market_machine_key(), get_market_machine_data, CACHE_TTL_TERMINAL_DAILY
    yield make_terminal_quant_lab_key(), get_quant_lab_data, CACHE_TTL_TERMINAL_DAILY
    yield make_terminal_all_ratios_key(), get_all_ratios_data, CACHE_TTL_TERMINAL_DAILY
    yield make_terminal_audit_summary_key(), get_audit_summary_data, CACHE_TTL_TERMINAL_QUARTERLY

    for symbol in top_symbols(top):
        yield (make_terminal_company_fundamental_key(symbol),
               (lambda s=symbol: get_company_unified_page_data(s)),
               CACHE_TTL_TERMINAL_QUARTERLY)


def warm_rebh_engine(top: int = WARM_UP_TOP_SYMBOLS) -> Iterable[Tuple[str, Callable, int]]:
    """The two engine shapes for the top-N companies, at their DEFAULT price.

    ``/api/engine/{symbol}`` keys on ``make_rebh_engine_key(symbol, price,
    market_cap)``. Both overrides are optional query params, so the key a reader
    gets without touching anything is ``...:none:none`` — that default is the one
    worth precomputing. A reader who does override the price asks for a different
    key and pays once, protected by single-flight.
    """
    from app.services.rebh_unified_engine import calculate_full_company_payload
    from app.api.routes.rebh_engine import _compat_payload

    for symbol in top_symbols(top):
        yield (make_rebh_engine_key(symbol, None, None),
               (lambda s=symbol: calculate_full_company_payload(
                   symbol=s, price_override=None, market_cap_override=None)),
               CACHE_TTL_REBH_ENGINE)
        yield (make_rebh_engine_key("compat:" + symbol, None, None),
               (lambda s=symbol: _compat_payload(s, None, None)),
               CACHE_TTL_REBH_ENGINE)


# ── Registry: namespace stem → supplier ────────────────────────────────────
# Keyed by the NARROWEST namespace the supplier actually produces. A job that
# busts a wider stem (``rebh``) still gets the engine warmed, because matching is
# "the busted stem is a prefix of the registered stem" (see warmers_for_prefix).
WARMERS: Dict[str, Callable[[], Iterable[Tuple[str, Callable, int]]]] = {
    "terminal": warm_terminal,
    "rebh:engine": warm_rebh_engine,
}


def warmers_for_prefix(prefix: str) -> List[Tuple[str, Callable]]:
    """Suppliers whose namespace falls under a namespace about to be evicted."""
    stem = (prefix or "").strip().rstrip(":").replace(":*", "").strip(":")
    if not stem:
        return []
    return [(name, fn) for name, fn in sorted(WARMERS.items())
            if name == stem or name.startswith(stem + ":") or stem.startswith(name + ":")]


def patterns_for_namespace(stem: str) -> List[str]:
    """The globs refresh_namespace sweeps for one registered namespace."""
    return [f"{stem}:*"]


def namespaces_for_job(job_name: str) -> List[str]:
    """The namespace stems a finished job is about to clear.

    Read from the SAME table the bust itself consults (cache_bust.JOB_GROUPS),
    so a warm-up can never drift ahead of, or behind, what a job invalidates —
    which is the failure mode this whole file exists to close.
    """
    try:
        from app.core import cache_bust
        patterns, _notes = cache_bust.patterns_for(job=job_name)
    except Exception as e:                            # noqa: BLE001 - warm-up is optional
        logger.warning("cache_warmers: job %s namespaces unresolved (%s)",
                       job_name, type(e).__name__)
        return []
    stems: List[str] = []
    for pattern in patterns:
        stem = str(pattern).strip().replace(":*", "").rstrip(":").strip()
        if stem and stem not in stems:
            stems.append(stem)
    return stems


def warm_plan_for_job(job_name: str) -> List[Tuple[str, Callable, List[str]]]:
    """``[(namespace, supplier, patterns)]`` for the parts of a job's bust that
    have a supplier. Namespaces nobody supplies keep the plain eviction path."""
    plan: List[Tuple[str, Callable, List[str]]] = []
    seen: set = set()
    for stem in namespaces_for_job(job_name):
        for name, fn in warmers_for_prefix(stem):
            if name in seen:
                continue
            seen.add(name)
            plan.append((name, fn, patterns_for_namespace(name)))
    return plan


def run_coroutine_blocking(coro: Any, timeout: float = 0.0) -> Any:
    """Drive one coroutine to completion from a thread that has no event loop.

    Jobs run in worker threads (APScheduler, job_runner, the scripts), while
    refresh_namespace is async and must own its own Redis pool — a redis-py pool
    is bound to the loop that created it, so the app singleton cannot be reused
    here (see scheduler._clear_cache's SYNC comment for the same hazard). When
    this thread DOES already have a live loop, the work is handed to a fresh
    thread rather than nested, so no caller blocks a loop it shares.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: Dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:                  # noqa: BLE001 - reported to caller
            box["error"] = exc

    thread = threading.Thread(target=_worker, name="cache-warm-up", daemon=True)
    thread.start()
    thread.join(timeout or None)
    if thread.is_alive():
        raise TimeoutError("cache warm-up still running when the deadline passed")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def warm_before_bust(job_name: str) -> List[str]:
    """Rebuild the hot keys of the namespaces this job is about to clear.

    Returns the stems whose sweep verifiably completed — the caller passes them
    to the bust as ``skip_prefixes``. A namespace is NEVER returned unless its
    own eviction finished AND nothing was left in it, because skipping one that
    still holds cold keys would leave stale data on screen, which is a
    correctness bug dressed as an optimisation.
    Never raises: a warm-up that cannot run simply costs the next reader one
    (single-flight-protected) cold query.
    """
    try:
        from app.core.cache_helpers import refresh_namespace
        plan = warm_plan_for_job(job_name)
    except Exception as e:                            # noqa: BLE001
        logger.warning("cache_warmers: warm-up skipped for %s (%s)", job_name, e)
        return []
    if not plan:
        return []
    protected: List[str] = []
    for stem, warm_fn, patterns in plan:
        try:
            report = run_coroutine_blocking(
                refresh_namespace(stem, warm_fn, patterns=patterns)
            )
        except Exception as e:                        # noqa: BLE001 - the bust follows anyway
            logger.warning("cache_warmers: %s warm-up failed (%s) — busting it fully",
                           stem, type(e).__name__)
            continue
        if not report.get("ok") or report.get("scan_failed") or report.get("residual"):
            # Incomplete sweep, a failed scan, or keys left behind: in every case
            # the regular bust must still run over this namespace, or the warm-up
            # optimisation would be quietly protecting stale data from eviction.
            logger.warning("cache_warmers: %s refresh incomplete (error=%s note=%s "
                           "scan_failed=%s residual=%s) — busting it fully", stem,
                           report.get("error"), report.get("note"),
                           report.get("scan_failed"), report.get("residual"))
            continue
        protected.append(stem)
        logger.info("cache_warmers: %s warmed=%s failed=%s kept=%s evicted=%s in %sms",
                    stem, report.get("warmed"), report.get("warm_failed"),
                    report.get("kept_warm"), report.get("evicted"),
                    report.get("elapsed_ms"))
    return protected


def bust_job_caches(job_name: str, **bust_kwargs) -> Dict[str, Any]:
    """Warm, then bust — the one call a finished job should make.

    A thin wrapper over cache_bust.bust(): the eviction stays the single
    implementation (and still happens when no warm-up was possible), this only
    adds the keys the bust must not undo.
    """
    skip = warm_before_bust(job_name)
    from app.core import cache_bust
    return cache_bust.bust(job=job_name, skip_prefixes=skip, **bust_kwargs)
