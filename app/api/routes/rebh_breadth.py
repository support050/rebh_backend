"""
REBH Market Breadth API — serves the engine's DATA snapshot from Postgres.

Source of truth: table `breadth_data` (one JSONB row per trading date, written by
the nightly breadth_job; rolling 14 days). NOTHING here recomputes indicators and
nothing reads parquet — routes only slice the stored DATA object, so responses
match the client's api_samples/* files key-for-key by construction (D4/🅰️).

Endpoints (registered under /api by main.py, auth via protected_dependencies):
    GET /rebh/breadth/today
    GET /rebh/breadth/series?keys=a,b&from=YYYY-MM-DD&to=YYYY-MM-DD
    GET /rebh/breadth/events
    GET /rebh/breadth/signals
    GET /rebh/breadth/sectors
    GET /rebh/breadth/stocks?group=Banks
    GET /rebh/breadth/stockbee?date=YYYY-MM-DD&key=up_x
    GET /rebh/breadth/ohlc                  (full: ohlc_dates + tasi_win + ohlc)
    GET /rebh/breadth/ohlc/{symbol}
    GET /rebh/breadth/meta
Big payloads (series/stocks/ohlc) are served gzipped (global GZipMiddleware),
cached in Redis through the shared `cache_read_through` helper (keys under
`rebh:breadth:*`, invalidated by the daily job's `_clear_breadth_cache()`), and
backed by a process-level memo keyed by trading_date — so the 8MB JSONB row is
parsed at most once per snapshot per worker even on a cold cache.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session
from typing import Optional

from app.core.cache_helpers import cache_read_through
from app.core.database import get_db

router = APIRouter(prefix="/rebh/breadth", tags=["REBH Market Breadth"])

# EOD data: changes once per trading day. Modest TTL caps staleness within a
# day; the nightly breadth job explicitly clears rebh:breadth:* on each new
# snapshot (scheduler._clear_breadth_cache), so the TTL is only a safety net.
BREADTH_CACHE_TTL = 21600  # 6 hours

# process-level memo: {"trading_date": date|None, "data": dict|None}
_MEMO: dict = {"trading_date": None, "data": None}


def _latest_snapshot(db: Session) -> dict:
    """Newest DATA snapshot; re-parses only when a newer trading_date landed."""
    row = db.execute(text(
        "SELECT trading_date, data FROM breadth_data ORDER BY trading_date DESC LIMIT 1"
    )).fetchone()
    if row is None:
        raise HTTPException(status_code=404,
                            detail="No breadth snapshot yet — daily job has not run")
    if _MEMO["trading_date"] != row[0]:
        _MEMO["trading_date"] = row[0]
        _MEMO["data"] = row[1]                      # psycopg2 JSONB -> python dict
    return _MEMO["data"]


@router.get("/today")
async def today(db: Session = Depends(get_db)):
    """18 headline fields — same keys as api_samples/today.json."""
    return await cache_read_through(
        "rebh:breadth:today", BREADTH_CACHE_TTL,
        lambda: _latest_snapshot(db)["today"])


@router.get("/series")
async def series(
    keys: Optional[str] = Query(None, description="comma-separated series keys; default all 125"),
    from_: Optional[str] = Query(None, alias="from", description="YYYY-MM-DD inclusive"),
    to: Optional[str] = Query(None, description="YYYY-MM-DD inclusive"),
    db: Session = Depends(get_db),
):
    """{dates:[...], series:{key:[...]}} — same shape as api_samples/series.json."""
    cache_key = f"rebh:breadth:series:{keys or 'all'}:{from_ or '-'}:{to or '-'}"

    def build():
        D = _latest_snapshot(db)
        dates, all_series = D["dates"], D["series"]
        if keys:
            wanted = [k.strip() for k in keys.split(",") if k.strip()]
            missing = [k for k in wanted if k not in all_series]
            if missing:
                raise HTTPException(status_code=400, detail=f"unknown series keys: {missing[:10]}")
        else:
            wanted = list(all_series)
        lo = next((i for i, d in enumerate(dates) if not from_ or d >= from_), 0)
        hi = len(dates)
        if to:
            hi = next((i for i in range(len(dates) - 1, -1, -1) if dates[i] <= to) + 1, len(dates))
        return {"dates": dates[lo:hi], "series": {k: all_series[k][lo:hi] for k in wanted}}

    return await cache_read_through(cache_key, BREADTH_CACHE_TTL, build)


@router.get("/events")
async def events(db: Session = Depends(get_db)):
    """Event marker date-lists per rule — same keys as api_samples/events.json."""
    return await cache_read_through(
        "rebh:breadth:events", BREADTH_CACHE_TTL,
        lambda: _latest_snapshot(db)["events"])


@router.get("/signals")
async def signals(db: Session = Depends(get_db)):
    """{signals_table, signal_stats} — same shape as api_samples/signals.json."""
    def build():
        D = _latest_snapshot(db)
        return {"signals_table": D["signals_table"], "signal_stats": D["signal_stats"]}
    return await cache_read_through("rebh:breadth:signals", BREADTH_CACHE_TTL, build)


@router.get("/sectors")
async def sectors(db: Session = Depends(get_db)):
    """{sectors, sector_perf, sector_pool} — same shape as api_samples/sectors.json."""
    def build():
        D = _latest_snapshot(db)
        return {"sectors": D["sectors"], "sector_perf": D["sector_perf"], "sector_pool": D["sector_pool"]}
    return await cache_read_through("rebh:breadth:sectors", BREADTH_CACHE_TTL, build)


@router.get("/stocks")
async def stocks(
    group: Optional[str] = Query(None, description="industry group filter, e.g. Banks"),
    db: Session = Depends(get_db),
):
    """{stocks, leaders, nh_vol_list, names} — same shape as api_samples/stocks.json."""
    cache_key = f"rebh:breadth:stocks:{group or 'all'}"

    def build():
        D = _latest_snapshot(db)
        if group:
            if group not in D["stocks"]:
                raise HTTPException(status_code=404, detail=f"unknown group '{group}'")
            return {"stocks": {group: D["stocks"][group]}, "names": D["names"]}
        return {"stocks": D["stocks"], "leaders": D["leaders"],
                "nh_vol_list": D["nh_vol_list"], "names": D["names"]}

    return await cache_read_through(cache_key, BREADTH_CACHE_TTL, build)


@router.get("/stockbee")
async def stockbee(
    date: Optional[str] = Query(None, description="YYYY-MM-DD; default all stored days"),
    key: Optional[str] = Query(None, description="column key, e.g. up_x"),
    db: Session = Depends(get_db),
):
    """{date: {key: [symbols]}} — same shape as api_samples/stockbee_names.json."""
    cache_key = f"rebh:breadth:stockbee:{date or 'all'}:{key or 'all'}"

    def build():
        lists = _latest_snapshot(db)["sb_lists"]
        if date:
            if date not in lists:
                raise HTTPException(status_code=404, detail=f"no stockbee list for {date}")
            out = {date: lists[date]}
        else:
            out = lists
        if key:
            out = {d: {k: v for k, v in cols.items() if k == key} for d, cols in out.items()}
        return out

    return await cache_read_through(cache_key, BREADTH_CACHE_TTL, build)


@router.get("/ohlc")
async def ohlc_all(db: Session = Depends(get_db)):
    """Full block {ohlc_dates, tasi_win, ohlc} — same shape as api_samples/ohlc.json."""
    def build():
        D = _latest_snapshot(db)
        return {"ohlc_dates": D["ohlc_dates"], "tasi_win": D["tasi_win"], "ohlc": D["ohlc"]}
    return await cache_read_through("rebh:breadth:ohlc", BREADTH_CACHE_TTL, build)


@router.get("/ohlc/{symbol}")
async def ohlc_symbol(symbol: str, db: Session = Depends(get_db)):
    """One symbol's adjusted OHLC windows + MAs + TASI reference for RS line."""
    cache_key = f"rebh:breadth:ohlc:{symbol.strip()}"

    def build():
        D = _latest_snapshot(db)
        sym = symbol.strip().lstrip("0") or symbol.strip()   # tolerate 01010-style input
        data = D["ohlc"].get(sym) or D["ohlc"].get(symbol.strip())
        if data is None:
            raise HTTPException(status_code=404, detail=f"no ohlc for symbol '{symbol}'")
        return {"symbol": sym, "dates": D["ohlc_dates"], "tasi": D["tasi_win"], "data": data}

    return await cache_read_through(cache_key, BREADTH_CACHE_TTL, build)


@router.get("/meta")
async def meta(db: Session = Depends(get_db)):
    """{thresholds, verdicts, classic_q} — same shape as api_samples/meta.json."""
    def build():
        D = _latest_snapshot(db)
        return {"thresholds": D["thresholds"], "verdicts": D["verdicts"], "classic_q": D["classic_q"]}
    return await cache_read_through("rebh:breadth:meta", BREADTH_CACHE_TTL, build)
