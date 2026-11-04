"""Cache management endpoints.

Two doors, deliberately separate:

* ``/api/cache/*``      — admin cookie session, for humans (dashboard buttons).
* ``/api/internal/cache/*`` — ``X-Internal-Key``, for machines (scripts, jobs).

The machine door is the reason a script run from a laptop can refresh the
deployed site: the script writes to the production database, so the bust has to
happen next to the production cache. Before this existed the only working move
was opening a Render shell and running ``redis_cache.flush_all()`` by hand.

Everything here delegates to ``app.core.cache_bust`` — the eviction logic that
used to be copy-pasted across two scripts, the scheduler and this module.

History (2026-11-04): this file used to evict ``tadawul_stocks:*``, ``tadawul:*``
and ``financials:*`` — none of which is a namespace the application writes (see
``cache_config.PREFIX_*``). Every call returned "cleared (0 keys)" while real
payloads stayed cached, i.e. a placebo button. It also mounted at ``/cache/*``
while the registry documented ``/api/cache/*``.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from app.api.deps import get_current_admin
from app.core import cache_bust
from app.core.cache_config import (
    APP_CACHE_PREFIXES,
    DAILY_MARKET_PREFIXES,
    PREFIX_REBH,
    PREFIX_TERMINAL,
)
from app.core.limiter import limiter
from app.core.security import verify_internal_key
from app.models.user import User

router = APIRouter(prefix="/cache", tags=["Cache Management"])
internal_router = APIRouter(prefix="/internal/cache", tags=["Cache Management (machine)"])


async def _bust(patterns: List[str]) -> dict:
    """Evict in a worker thread: cache_bust is sync on purpose (scripts).

    Running it on the event loop would freeze every other request for the
    duration of the SCAN walk.
    """
    report = await run_in_threadpool(cache_bust.evict, patterns)
    if not report["reachable"]:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Cache is unreachable — nothing was evicted. "
                           "Readers will keep seeing the old payload until the TTL lapses.",
                "target": report["target"],
                "error": report["error"],
            },
        )
    return report


# ── Human door (admin session) ─────────────────────────────────────────────

@router.get("/namespaces")
async def list_namespaces(current_admin: User = Depends(get_current_admin)):
    """What can be busted, and what each group means. Keeps the UI honest."""
    _ = current_admin
    return {
        "daily_market_prefixes": list(DAILY_MARKET_PREFIXES),
        "app_cache_prefixes": list(APP_CACHE_PREFIXES),
        "groups": {name: [f"{p}:*" for p in prefixes] for name, prefixes in cache_bust.GROUPS.items()},
        "jobs": {job: list(groups) for job, groups in cache_bust.JOB_GROUPS.items()},
    }


@router.get("/status")
async def cache_status(current_admin: User = Depends(get_current_admin)):
    """Redis reachability + hit ratio.

    A fail-open cache hides its own absence: when the URL was wrong the site
    kept working, just rebuilding every page from Postgres (measured 22-28 s
    for one breadth row). ``hit_ratio`` is what makes that visible.
    """
    _ = current_admin
    snapshot = await run_in_threadpool(cache_bust.info)
    return {
        "redis_status": "connected" if snapshot["reachable"] else "unreachable",
        "target": snapshot["target"],
        "redis_version": snapshot["redis_version"],
        "keyspace_hits": snapshot["keyspace_hits"],
        "keyspace_misses": snapshot["keyspace_misses"],
        "hit_ratio": snapshot["hit_ratio"],
        "expired_keys": snapshot["expired_keys"],
        "evicted_keys": snapshot["evicted_keys"],
        "used_memory_human": snapshot["used_memory_human"],
        "maxmemory_policy": snapshot["maxmemory_policy"],
        "key_counts": snapshot["key_counts"],
        "error": snapshot["error"],
    }


@router.post("/bust")
async def bust_cache(
    request: Request,
    job: Optional[str] = Query(None, description="bust what this job invalidates"),
    groups: Optional[str] = Query(None, description="comma-separated group names"),
    namespaces: Optional[str] = Query(None, description="comma-separated prefixes or globs"),
    current_admin: User = Depends(get_current_admin),
):
    """Bust the cache for one job / group / namespace — never ``FLUSHALL``.

    ``?job=breadth_daily`` clears ``rebh:breadth:*`` and nothing else, so the
    other 200 pages keep their cached payload.
    """
    _ = current_admin
    parsed_groups = [g.strip() for g in (groups or "").split(",") if g.strip()] or None
    parsed_namespaces = [n.strip() for n in (namespaces or "").split(",") if n.strip()] or None
    try:
        patterns, _notes = cache_bust.patterns_for(job=job, groups=parsed_groups,
                                                   namespaces=parsed_namespaces)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    report = await _bust(patterns)
    return {
        "message": f"cleared {report['deleted']} key(s)",
        "deleted_count": report["deleted"],
        "per_pattern": report["per_pattern"],
        "skipped_auth_keys": report["skipped_auth_keys"],
        "target": report["target"],
    }


@router.post("/clear/stocks")
async def clear_stocks_cache(current_admin: User = Depends(get_current_admin)):
    """Prices, RS ratings, screeners, industry groups (the daily market reads)."""
    _ = current_admin
    patterns = [f"{p}:*" for p in cache_bust.GROUPS["market"]]
    report = await _bust(patterns)
    return {"message": f"cleared {report['deleted']} key(s)",
            "deleted_count": report["deleted"], "per_pattern": report["per_pattern"]}


@router.post("/clear/financials")
async def clear_financial_cache(
    symbol: Optional[str] = Query(None, description="one symbol, or several comma-separated"),
    current_admin: User = Depends(get_current_admin),
):
    """Per-company payloads: scraped financials, terminal page, engine contract.

    With a symbol the sweep is narrow (that company only); without one it is
    the whole financial/terminal/rebh keyspace.
    """
    _ = current_admin
    symbols = [s.strip().lower() for s in (symbol or "").split(",") if s.strip()]
    if symbols:
        patterns = []
        for sym in symbols:
            patterns += [
                f"scraper:financials:*{sym}*",
                f"scraper:table:*{sym}*",
                f"{PREFIX_TERMINAL}:company:{sym}:*",
                f"{PREFIX_REBH}:engine:{sym}:*",
            ]
    else:
        patterns = ["scraper:*", f"{PREFIX_TERMINAL}:*", f"{PREFIX_REBH}:*"]
    report = await _bust(patterns)
    return {"message": f"cleared {report['deleted']} key(s)",
            "deleted_count": report["deleted"], "per_pattern": report["per_pattern"],
            "scoped_to": symbols or "all financial namespaces"}


# ── Machine door (X-Internal-Key, CSRF-exempt, no cookies) ─────────────────

@internal_router.post("/bust")
@limiter.limit("20/minute")
async def internal_bust(
    request: Request,
    patterns: List[str] = Query(..., description="cache namespaces or globs to evict"),
    _key: bool = Depends(verify_internal_key),
):
    """Called by scripts/jobs when their Redis is not the one the API reads.

    Body-less POST; the list is in the query so a one-line curl works:

        curl -XPOST "https://<api>/api/internal/cache/bust?patterns=rebh:breadth:*" \\
             -H "X-Internal-Key: $INTERNAL_API_KEY"
    """
    _ = _key
    accepted, rejected = cache_bust.validate_patterns(patterns)
    if rejected:
        raise HTTPException(
            status_code=400,
            detail={"message": "refused patterns (only app cache namespaces may be "
                               "evicted remotely; '*' is never allowed)",
                    "rejected": rejected, "allowed": list(APP_CACHE_PREFIXES)},
        )
    report = await _bust(accepted)
    return {"ok": True, "deleted": report["deleted"], "per_pattern": report["per_pattern"],
            "skipped_auth_keys": report["skipped_auth_keys"], "target": report["target"]}
