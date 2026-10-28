"""Route-level access-control regression tests.

Why this file exists
--------------------
Guards in this project are attached in two places: per-route ``Depends(...)``
and the ``dependencies=[...]`` argument of ``app.include_router()`` in
``app/main.py``. A source grep therefore cannot tell whether a route is really
protected — only the resolved application can. Batch 1 of the auth audit found
seven state-changing routes mounted with no dependency at all
(``/api/market-pulse`` writes, ``POST /api/tasi-settings``,
``POST /api/rebh/classification/{symbol}``, ``POST /api/prices/static-info/import``)
plus a public ``GET /api/economic-indicators/clear-cache``.

These tests walk the LIVE route graph of ``app.main.app`` and fail the build if
a mutating route (or an action-shaped GET) ever resolves without an identity or
internal-key dependency, and cover the path-traversal fixes themselves:
symbol validation and the confined disk reader.

Note (2026-10-29): ``POST /api/upload/xbrl`` and
``POST /api/prices/static-info/import`` were retired as dead endpoints, so they
are no longer part of the pinned surface; their sanitisation tests went with
the module.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.main import app

# ── vocabulary ──────────────────────────────────────────────────────────────
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Anything that resolves a caller's identity or the internal ingest secret.
AUTH_DEPS = {
    "get_current_user",
    "get_current_admin",
    "get_current_user_optional",
    "verify_token",
    "require_current_user",
    "verify_internal_key",
}

# Public by design: the auth lifecycle (nothing to authenticate against yet) +
# the contact form. Kept as exact paths, and asserted to still exist below, so
# the allow-list cannot silently rot into a blanket exemption.
PUBLIC_MUTATION_ALLOWLIST = {
    "POST /api/auth/register",
    "POST /api/auth/login",
    "POST /api/auth/refresh-token",
    "POST /api/auth/forget-password",
    "POST /api/auth/reset-password",
    "POST /api/auth/activate-session",
    "POST /api/auth/oauth/confirm-link",
    "POST /api/auth/google/callback",
    "POST /api/auth/facebook/callback",
    "POST /api/contact/",
}

# Routes that Batch 1 pinned to an administrator (plus Batch 3's /health/detail).
# /api/upload/xbrl and /api/prices/static-info/import were deleted 2026-10-29
# as dead endpoints (zero callers), so they are intentionally absent here.
ADMIN_ONLY_ROUTES = {
    "POST /api/market-pulse/",
    "POST /api/market-pulse/ingest",
    "POST /api/market-pulse/recalculate-all",
    "PATCH /api/market-pulse/{record_id}",
    "DELETE /api/market-pulse/{record_id}",
    "POST /api/tasi-settings",
    "POST /api/rebh/classification/{symbol}",
    "POST /api/economic-indicators/clear-cache",
    "GET /health/detail",
}

# A GET must never change state. A route is "action-shaped" when one of its
# static path segments starts with one of these verbs — matched per segment, not
# as a substring of the whole path, or every /api/scraper/* read would look
# like a scrape trigger and every /importers/health like a write.
ACTION_VERBS = (
    "clear-cache", "flush", "purge", "invalidate", "reset", "delete", "remove",
    "import", "ingest", "recalculate", "recompute", "rebuild", "seed", "migrate",
    "trigger", "scrape", "run", "start", "stop", "cancel", "update", "write",
)


def _is_action_path(path: str) -> bool:
    """True when a static segment IS an action verb (boundary-matched).

    ``importers`` must not read as ``import`` and ``runbook`` not as ``run``, so
    a verb only counts when the segment equals it or continues it with ``-``/``_``.
    """
    for segment in path.strip("/").split("/"):
        if not segment or segment.startswith("{"):
            continue
        for verb in ACTION_VERBS:
            if segment == verb or segment.startswith((verb + "-", verb + "_")):
                return True
    return False


def _dep_names(route: APIRoute) -> set[str]:
    """Every callable reachable from a route's dependency graph, by name."""
    found: set[str] = set()

    def walk(dep) -> None:
        if dep is None:
            return
        call = getattr(dep, "call", None)
        name = getattr(call, "__name__", None)
        if name and name not in {"<lambda>", "None"}:
            found.add(name)
        for sub in getattr(dep, "dependencies", None) or []:
            walk(sub)

    walk(route.dependant)
    return found


def _routes() -> list[APIRoute]:
    return [r for r in app.routes if isinstance(r, APIRoute)]


def _keys(routes: list[APIRoute]) -> set[str]:
    return {f"{m} {r.path}" for r in routes for m in r.methods}


# ── the graph itself ────────────────────────────────────────────────────────
def test_route_graph_is_loaded():
    """Sanity: the app really exposes the surface we are auditing."""
    routes = _routes()
    assert len(routes) > 100, f"expected the full API, got {len(routes)} routes"
    keys = _keys(routes)
    for required in ADMIN_ONLY_ROUTES:
        assert required in keys, f"{required} disappeared from the route graph"


def test_no_unauthenticated_mutating_routes():
    """No POST/PUT/PATCH/DELETE may resolve without an identity or key dep."""
    exposed = sorted(
        f"{m} {route.path}"
        for route in _routes()
        for m in route.methods
        if m in MUTATING_METHODS
        and f"{m} {route.path}" not in PUBLIC_MUTATION_ALLOWLIST
        and not (_dep_names(route) & AUTH_DEPS)
    )
    assert not exposed, "unauthenticated mutating routes:\n  " + "\n  ".join(exposed)


def test_no_unauthenticated_action_gets():
    """A GET that performs an action must be guarded too (CSRF can't help it)."""
    offenders = sorted(
        f"GET {route.path}"
        for route in _routes()
        if "GET" in route.methods
        and _is_action_path(route.path)
        and not (_dep_names(route) & AUTH_DEPS)
    )
    assert not offenders, (
        "unguarded action-shaped GET routes (prefer POST + admin): " + ", ".join(offenders)
    )


def test_public_allowlist_has_no_dead_entries():
    """Every exempted route must still exist — no stale blanket exemptions."""
    keys = _keys(_routes())
    dead = sorted(PUBLIC_MUTATION_ALLOWLIST - keys)
    assert not dead, f"allow-list entries no longer in the graph: {dead}"


def test_allowlist_covers_only_auth_lifecycle_and_contact():
    """The public mutation set may not quietly grow into business endpoints."""
    assert all(
        path.startswith(("/api/auth/", "/api/contact"))
        for _m, path in (k.split(" ", 1) for k in PUBLIC_MUTATION_ALLOWLIST)
    ), "only the auth lifecycle and the contact form may mutate without identity"


@pytest.mark.parametrize("method_path", sorted(ADMIN_ONLY_ROUTES))
def test_batch1_routes_require_admin(method_path: str):
    """The routes closed in Batch 1 stay pinned to get_current_admin."""
    method, path = method_path.split(" ", 1)
    route = next(r for r in _routes() if r.path == path and method in r.methods)
    assert "get_current_admin" in _dep_names(route), f"{method_path} lost its admin guard"


def test_public_reads_stay_public():
    """Guarding writes must not have locked the landing page out."""
    routes = {f"{m} {r.path}": r for r in _routes() for m in r.methods}
    for public_get in (
        "GET /api/market-pulse/",
        "GET /api/market-pulse/averages",
        "GET /api/tasi-settings",
        "GET /api/prices/latest",
        "GET /api/rebh/classification/{symbol}",
    ):
        route = routes.get(public_get)
        assert route is not None, f"{public_get} vanished"
        assert not (_dep_names(route) & AUTH_DEPS), f"{public_get} is no longer public"


# ── symbol sanitisation (C-1 service layer) ─────────────────────────────────
# The xbrl_upload module and its _safe_destination filename tests were deleted
# 2026-10-29 (dead endpoint, zero callers). The service-layer symbol guard below
# still protects every writer of XBRL JSON, including the ingestion scripts.
@pytest.mark.parametrize(
    "symbol",
    ["../../etc/passwd", "1010/../../../x", "..", "", "a" * 20, "10 10", "1010.json"],
)
def test_symbols_that_escape_the_output_dir_are_rejected(symbol):
    from app.services import xbrl_data_service

    with pytest.raises(ValueError):
        xbrl_data_service.save_company(symbol, {"meta": {}})

    # Reads fail closed the same way, and never touch another file.
    assert xbrl_data_service.get_company(symbol) is None


def test_real_symbol_still_round_trips(tmp_path, monkeypatch):
    from app.services import xbrl_data_service

    monkeypatch.setattr(xbrl_data_service, "OUTPUT_DIR", tmp_path)
    fp = xbrl_data_service.save_company("1010", {"meta": {"symbol": "1010"}})
    assert fp == tmp_path / "1010_financials.json"
    assert fp.read_text(encoding="utf-8").startswith("{")


# ── disk reader confinement (C-2) ───────────────────────────────────────────
# The /api/prices/static-info/import endpoint and its _resolve_import_path
# helper were deleted 2026-10-29 (dead endpoint, zero callers); the three
# confinement tests that exercised the helper went with the code.
