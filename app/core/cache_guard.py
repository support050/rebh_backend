"""
Cache Guard — validate a request BEFORE it can reach the cache or the database.

Why this module exists
----------------------
Almost every cache key in this app is built from a user-supplied parameter
(``prices:history:symbol:{symbol}``, ``economic:sp500:{limit}:...``). Without a
gate, an arbitrary string becomes an arbitrary key:

  * ``?symbol=<random>`` on a route that legitimately answers "no such symbol"
    cost a full query every time — the key filled with junk, and the junk keys
    multiplied with the traffic (unbounded keyspace growth from free input).
    Worse on the routes that answered ``None``: a stored None reads back as
    "absent", so nothing was ever cached and the database took 100 % of that
    traffic (cache PENETRATION);
  * ``?limit=999999999`` is one row-fetch away from an OOM, and every distinct
    value of it is a distinct cache key (keyspace flooding);
  * a parameter containing ``*`` or ``?`` lands inside a namespace glob and
    corrupts every invalidation built on it.

Rules
-----
1. Validation happens FIRST: the guard runs before ``cache_read_through`` and
   before any query, so a rejected request costs one index lookup, not a join.
2. Rejections are ordinary HTTP statuses — 404 for an unknown symbol (the
   resource does not exist), 422 for a malformed/oversized parameter. Nothing
   here may answer 500: that would turn an attack into an error budget.
3. The catalog lookup FAILS OPEN. If the symbol catalog cannot be built
   (DB timeout, unreadable CSV, first-run before any seed), every symbol is
   accepted, because blocking real tickers off a broken lookup is a worse
   outage than a missing optimisation. The same fail-open discipline the
   cache itself uses (see app/core/redis.py).
4. ``guard_symbol`` belongs only on routes whose rows come FROM a catalog table.
   The catalog is prices + static_stock_info + tasi_components + the shipped CSV,
   so a route reading a different table can legitimately be asked for a symbol the
   catalog has never seen — technical_screener's stock_indicators is the live
   example, and it is therefore guarded by shape only. 404ing real data is a
   regression, not a protection.

Pure-ish import: ``fastapi`` for HTTPException only, plus stdlib. No settings,
no redis — so a route may import this at module level without cycles.
"""

from __future__ import annotations

import csv
import logging
import time
from pathlib import Path
from typing import Any, Optional, Set

from fastapi import HTTPException
from sqlalchemy import text

from app.core.cache_config import (
    CACHE_KEY_MAX_LENGTH,
    IN_MEMORY_TTL_KNOWN_SYMBOLS,
    MAX_DATE_RANGE_DAYS,
    MAX_QUERY_LIMIT,
    MAX_QUERY_OFFSET,
)
from app.core.cache_helpers import cache_key_problem

logger = logging.getLogger(__name__)

# The catalog is a few index-only scans over ~300 rows, but it is still a DB
# round trip, so it is read once per process and reused (see module docstring 3).
_CATALOG: dict = {"symbols": frozenset(), "loaded_at": 0.0, "ready": False}

# The catalog query is bounded: a lookup that could take longer than this is
# treated as "unavailable" (fail-open) instead of delaying the user's request.
CATALOG_STATEMENT_TIMEOUT_MS = 5000

COMPANY_SYMBOLS_CSV = Path(__file__).resolve().parents[2] / "company_symbols.csv"

# Sources of "symbols this app knows". Kept deliberately wide (see the audit in
# the Part 2 brief: 21 symbols hold price rows while missing from
# static_stock_info/tasi_components, so a catalog built on those alone would 404
# real companies). Every source is optional.
_CATALOG_QUERIES: tuple = (
    "SELECT DISTINCT symbol FROM prices",
    "SELECT DISTINCT symbol FROM static_stock_info",
    "SELECT DISTINCT symbol FROM tasi_components",
)


def _normalise(symbol: Any) -> str:
    return str(symbol or "").strip().upper()


def _variants(symbol: Any) -> Set[str]:
    """Every form a legitimate caller may use for one company.

    Tadawul codes appear both zero-padded ("01010") and bare ("1010") across the
    sources and in user input, so membership is tested against both spellings.
    """
    raw = _normalise(symbol)
    out = {raw}
    stripped = raw.lstrip("0")
    if stripped:
        out.add(stripped)
    return out


def _read_csv_symbols(path: Path = COMPANY_SYMBOLS_CSV) -> Set[str]:
    """The static symbol list shipped with the backend, when it exists."""
    out: Set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                value = (row.get("Symbol") or row.get("symbol") or "").strip()
                if value:
                    out |= _variants(value)
    except OSError:
        pass
    except Exception as e:                            # noqa: BLE001 - optional source
        logger.debug("cache_guard: symbol CSV unreadable (%s)", type(e).__name__)
    return out


def _query_catalog(db) -> Set[str]:
    """Union of the DB symbol sources, or an EMPTY set when unavailable.

    One statement_timeout for the whole build, and every query is wrapped: a
    partial catalog is worse than none (it would 404 real symbols), so any
    failure aborts the build and the caller fails open.
    """
    collected: Set[str] = set()
    try:
        db.rollback()                    # clean transaction; SET LOCAL applies to it
        db.execute(text("SET LOCAL statement_timeout = :ms"),
                   {"ms": CATALOG_STATEMENT_TIMEOUT_MS})
        for sql in _CATALOG_QUERIES:
            collected |= {row[0] for row in db.execute(text(sql)).fetchall()
                          if row[0]}
        db.commit()
    except Exception as e:                            # noqa: BLE001 - fail open
        try:
            db.rollback()
        except Exception:                             # noqa: BLE001
            pass
        logger.warning("cache_guard: symbol catalog unavailable (%s) — "
                       "symbols are NOT validated until the next attempt",
                       type(e).__name__)
        return set()
    return {_normalise(s) for s in collected}


def known_symbols(db, *, max_age: float = IN_MEMORY_TTL_KNOWN_SYMBOLS) -> Set[str]:
    """The cached catalog (union of DB sources + the shipped CSV).

    Returns a set that may be EMPTY — and empty means "unknown catalog", which
    every caller must interpret as fail-open, never as "nothing exists".
    """
    now = time.monotonic()
    if _CATALOG["ready"] and (now - _CATALOG["loaded_at"]) < max_age:
        return _CATALOG["symbols"]
    symbols: Set[str] = set()
    if db is not None:
        symbols |= _query_catalog(db)
    symbols |= _read_csv_symbols()
    if symbols:
        # Only a NON-EMPTY catalog is memoised. Caching an empty one for an hour
        # would pin fail-open for the whole process after one transient failure.
        normalised = {s for s in (_normalise(x) for x in symbols) if s}
        _CATALOG["symbols"] = frozenset(
            normalised | {v for s in normalised for v in _variants(s)}
        )
        _CATALOG["loaded_at"] = now
        _CATALOG["ready"] = True
        logger.info("cache_guard: symbol catalog loaded symbols=%s", len(_CATALOG["symbols"]))
    return _CATALOG["symbols"] if _CATALOG["ready"] else set()


def reset_symbol_catalog() -> None:
    """Drop the in-process catalog (tests, and after a symbol-master import)."""
    _CATALOG["symbols"] = frozenset()
    _CATALOG["loaded_at"] = 0.0
    _CATALOG["ready"] = False


def is_known_symbol(db, symbol: Any) -> bool:
    """True when ``symbol`` is known OR the catalog could not be built.

    The second clause is the fail-open rule from the module docstring; it is
    what makes this safe to put in front of a production endpoint.
    """
    catalog = known_symbols(db)
    if not catalog:
        return True
    return bool(_variants(symbol) & catalog)


def guard_symbol(db, symbol: Any, *, field: str = "symbol") -> Any:
    """404 unless ``symbol`` names a company this app knows.

    The argument is returned UNCHANGED on purpose: matching happens case- and
    zero-padding-insensitively, but the route's own query keeps the exact string
    the caller sent. Normalising here would silently change which rows a
    case-sensitive ``WHERE symbol =`` compares against — a guard must reject
    junk, not redefine lookups.
    """
    cleaned = _normalise(symbol)
    if not cleaned:
        raise HTTPException(status_code=422, detail=f"{field} must not be empty")
    if not is_known_symbol(db, cleaned):
        raise HTTPException(status_code=404,
                            detail=f"Unknown {field} '{symbol}' — no such symbol")
    return symbol


def guard_pagination(limit: Optional[int] = None, offset: Optional[int] = None, *,
                     max_limit: int = MAX_QUERY_LIMIT,
                     max_offset: int = MAX_QUERY_OFFSET,
                     field: str = "limit") -> None:
    """422 on a parameter wide enough to mint an unbounded query and key space.

    The ceiling is a *backstop*, not the page size: FastAPI's own ``le=`` on each
    route stays, because it rejects before this module is even reached. This
    exists for the routes that carry no ceiling at all.
    """
    if limit is not None:
        if int(limit) < 0:
            raise HTTPException(status_code=422, detail=f"{field} must not be negative")
        if int(limit) > max_limit:
            raise HTTPException(status_code=422,
                                detail=f"{field} must be <= {max_limit}")
    if offset is not None:
        if int(offset) < 0:
            raise HTTPException(status_code=422, detail="offset must not be negative")
        if int(offset) > max_offset:
            raise HTTPException(status_code=422,
                                detail=f"offset must be <= {max_offset}")


def guard_date_range(start: Any = None, end: Any = None) -> None:
    """422 when a range is inverted or absurdly wide.

    ``start > end`` is a guaranteed-empty query that used to be re-run forever,
    and a 10 000-year window is a probe. Both are answered before the DB sees
    them. Values may be date/datetime/str; unparseable pairs are left to
    FastAPI's own validation.
    """
    if start in (None, "") or end in (None, ""):
        return
    try:
        from datetime import date, datetime

        def _as_date(value):
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()

        first, last = _as_date(start), _as_date(end)
    except (TypeError, ValueError):
        return                                    # not ours to parse; Pydantic will
    if first > last:
        raise HTTPException(status_code=422,
                            detail="start_date must not be after end_date")
    if (last - first).days > MAX_DATE_RANGE_DAYS:
        raise HTTPException(status_code=422,
                            detail=f"date range must be <= {MAX_DATE_RANGE_DAYS} days")


def guard_cache_key(key: Any) -> str:
    """422 for a cache key that could only have come from a crafted parameter.

    ``cache_read_through`` refuses such a key and serves from the DB (fail-open),
    which is right for a cache but wrong for a request the route should never
    have accepted: a 200 KB ``?symbol=`` would otherwise reach Postgres. Routes
    that interpolate user input into a key call this first, so the rejection
    happens before both the cache and the database.
    """
    problem = cache_key_problem(key)
    if problem:
        detail = (f"parameter produces an invalid cache key ({problem}, "
                  f"max length {CACHE_KEY_MAX_LENGTH})"
                  if problem != "auth_keyspace" else "reserved key namespace")
        raise HTTPException(status_code=422, detail=detail)
    return str(key)


def guard_query(*, db=None, symbol: Any = None, limit: Optional[int] = None,
                offset: Optional[int] = None, start: Any = None, end: Any = None,
                key: Any = None, symbol_field: str = "symbol") -> Any:
    """One call for the whole gate, in the order that costs the least.

    Cheap scalar checks first (limit/offset/key shape), then the catalog lookup,
    so a malformed request never spends a DB round trip to be rejected anyway.
    Returns the symbol it validated (unchanged), or None when none was given.
    """
    guard_pagination(limit, offset)
    if key is not None:
        guard_cache_key(key)
    guard_date_range(start, end)
    if symbol is not None:
        return guard_symbol(db, symbol, field=symbol_field) if db is not None \
            else _normalise(symbol)
    return None


def catalog_size() -> int:
    """How many symbols the in-process catalog currently holds (0 = unloaded)."""
    return len(_CATALOG["symbols"])
