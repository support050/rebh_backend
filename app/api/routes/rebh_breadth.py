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
    GET /rebh/breadth/guru                (Guru Stock Scans leaderboard, standalone page)
    GET /rebh/breadth/ohlc                  (full: ohlc_dates + tasi_win + ohlc)
    GET /rebh/breadth/ohlc/{symbol}
    GET /rebh/breadth/meta
    GET /rebh/breadth/summary          (rule-based Arabic one-liner, CR3 §12)
    GET/PUT /rebh/breadth/prefs        (CR3 ⚙ Customize — user_prefs table)
    GET/PUT/DELETE /rebh/breadth/dashboards  (CR3 ★ Custom Dashboard — user_dashboards)
Big payloads (series/stocks/ohlc) are served gzipped (global GZipMiddleware),
cached in Redis through the shared `cache_read_through` helper (keys under
`rebh:breadth:*`, invalidated by the daily job's `_clear_breadth_cache()`), and
backed by a process-level memo keyed by trading_date — so the 8MB JSONB row is
parsed at most once per snapshot per worker even on a cold cache.
"""

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from typing import Optional

from app.api.deps import get_current_user
from app.core.cache_config import CACHE_TTL_BREADTH
from app.core.cache_helpers import cache_read_through
from app.core.database import get_db
from app.models.breadth import UserDashboard
from app.models.user import User
from app.models.user_prefs import UserPreference

router = APIRouter(prefix="/rebh/breadth", tags=["REBH Market Breadth"])

# EOD data: changes once per trading day. Modest TTL caps staleness within a
# day; the nightly breadth job explicitly clears rebh:breadth:* on each new
# snapshot (scheduler._clear_breadth_cache), so the TTL is only a safety net.
# Value lives in cache_config.CACHE_TTL_BREADTH (6 hours).

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
        "rebh:breadth:today", CACHE_TTL_BREADTH,
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

    return await cache_read_through(cache_key, CACHE_TTL_BREADTH, build)


@router.get("/events")
async def events(db: Session = Depends(get_db)):
    """Event marker date-lists per rule — same keys as api_samples/events.json."""
    return await cache_read_through(
        "rebh:breadth:events", CACHE_TTL_BREADTH,
        lambda: _latest_snapshot(db)["events"])


@router.get("/signals")
async def signals(db: Session = Depends(get_db)):
    """{signals_table, signal_stats} — same shape as api_samples/signals.json."""
    def build():
        D = _latest_snapshot(db)
        return {"signals_table": D["signals_table"], "signal_stats": D["signal_stats"]}
    return await cache_read_through("rebh:breadth:signals", CACHE_TTL_BREADTH, build)


@router.get("/sectors")
async def sectors(db: Session = Depends(get_db)):
    """{sectors, sector_perf, sector_pool} — same shape as api_samples/sectors.json."""
    def build():
        D = _latest_snapshot(db)
        return {"sectors": D["sectors"], "sector_perf": D["sector_perf"], "sector_pool": D["sector_pool"]}
    return await cache_read_through("rebh:breadth:sectors", CACHE_TTL_BREADTH, build)


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

    return await cache_read_through(cache_key, CACHE_TTL_BREADTH, build)


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

    return await cache_read_through(cache_key, CACHE_TTL_BREADTH, build)


@router.get("/guru")
async def guru(db: Session = Depends(get_db)):
    """Guru Stock Scans leaderboard — a self-contained payload for the standalone
    /guru-scans page: {date, columns:{scan:[symbols]}, rows:{sym:{name,chg_pct,rs,
    stage,count}}, counts}. Nine columns + the RS(1-99)/Stage(1-4) badges only; no
    earnings-countdown badge (Tadawul publishes no forward calendar). Served from the
    nightly breadth_data snapshot like every other board — never recomputed here.

    404 when the current snapshot predates the Guru column; 503 when the engine failed
    to build it, so the page shows the outage instead of an empty-but-confident board.
    """
    def build():
        g = _latest_snapshot(db).get("guru")
        if not g:
            raise HTTPException(status_code=404, detail="no guru payload in the current breadth snapshot")
        if "error" in g:
            raise HTTPException(status_code=503, detail=f"guru payload failed at source: {g['error']}")
        return g

    return await cache_read_through("rebh:breadth:guru", CACHE_TTL_BREADTH, build)


@router.get("/ohlc")
async def ohlc_all(db: Session = Depends(get_db)):
    """Full block {ohlc_dates, tasi_win, ohlc} — same shape as api_samples/ohlc.json."""
    def build():
        D = _latest_snapshot(db)
        return {"ohlc_dates": D["ohlc_dates"], "tasi_win": D["tasi_win"], "ohlc": D["ohlc"]}
    return await cache_read_through("rebh:breadth:ohlc", CACHE_TTL_BREADTH, build)


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

    return await cache_read_through(cache_key, CACHE_TTL_BREADTH, build)


@router.get("/meta")
async def meta(db: Session = Depends(get_db)):
    """{thresholds, verdicts, classic_q} — same shape as api_samples/meta.json."""
    def build():
        D = _latest_snapshot(db)
        return {"thresholds": D["thresholds"], "verdicts": D["verdicts"], "classic_q": D["classic_q"]}
    return await cache_read_through("rebh:breadth:meta", CACHE_TTL_BREADTH, build)


# ── CR3 §12 · Arabic daily read (summary:ar widget + /breadth/summary page) ──

_REGIME_AR = {
    "Washout": "تشبّع بيع تاريخي — أفضل سياق دخول",
    "Weak": "اتساع ضعيف بلا تشبّع بيع — الانتظار حتى Washout أو ثرست",
    "Neutral": "لا ميزة — حياد",
    "Healthy": "مشاركة صحية — الاتجاه مدعوم",
    "Strong": "مشاركة صحية — الاتجاه مدعوم",
}


@router.get("/summary")
async def summary(db: Session = Depends(get_db)):
    """Rule-based Arabic one-liner over today's snapshot — the reference
    `summary:ar` widget text (pro_dashboard_template.html) ported verbatim,
    computed server-side so the notification card and the /breadth/summary
    page share one source. Plain JSON, no recompute."""
    def build():
        t = _latest_snapshot(db)["today"]
        regime = t.get("regime") or ""
        ar = _REGIME_AR.get(regime, "")
        line = (f"<b>{t['date']}</b> · Breadth Score <b>{t['score']}</b> / 100 · "
                f"النظام <b>{regime}</b> منذ {t.get('regime_since', '')}<br>{ar}")
        return {"date": t["date"], "score": t["score"], "regime": regime,
                "regime_since": t.get("regime_since"), "ar": ar, "line": line}
    return await cache_read_through("rebh:breadth:summary", CACHE_TTL_BREADTH, build)


# ── CR3 §4 · user preferences (⚙ Customize drawer, persisted per account) ──

# Reference PREFS defaults (template line 193), kept key-for-key.
BREADTH_PREF_DEFAULTS = {
    "font": 1, "chart": 1, "lw": 1, "legend": True, "rscolor": True,
    "compact": False, "nav": 190, "up": "", "down": "", "range": 504,
    "hidden": [], "title": "TASI Market Breadth", "tvbars": 500,
}


def _prefs_blob(db: Session, user_id: int) -> tuple:
    """(row, full preferences dict) — creates the row lazily on first write."""
    pref = db.query(UserPreference).filter(UserPreference.user_id == user_id).first()
    blob = pref.preferences if pref else {}
    if not isinstance(blob, dict):
        blob = {}
    return pref, blob


@router.get("/prefs")
def get_prefs(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Breadth prefs for this user (namespaced under `breadth` inside the shared
    user_prefs JSON, so the watchlist/RS keys in the same row are untouched).
    Defaults are merged server-side so the client always gets a complete object."""
    _, blob = _prefs_blob(db, current_user.id)
    prefs = dict(BREADTH_PREF_DEFAULTS)
    stored = blob.get("breadth")
    if isinstance(stored, dict):
        prefs.update(stored)
    return {"preferences": prefs}


@router.put("/prefs")
def put_prefs(prefs: dict = Body(...,
                                 description="reference PREFS object (partial keys allowed)"),
              db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """Shallow-merge the submitted keys into the user's breadth prefs and store.
    The frontend debounces writes; the row is the single source across devices."""
    pref, blob = _prefs_blob(db, current_user.id)
    stored = blob.get("breadth")
    merged = dict(stored) if isinstance(stored, dict) else {}
    merged.update(prefs if isinstance(prefs, dict) else {})
    blob["breadth"] = merged
    if pref is None:
        pref = UserPreference(user_id=current_user.id)
        db.add(pref)
    pref.preferences = blob
    db.commit()
    out = dict(BREADTH_PREF_DEFAULTS)
    out.update(merged)
    return {"status": "success", "preferences": out}


# ── CR3 §2 · custom dashboards (★ Custom Dashboard, persisted per account) ──

class DashboardPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    items: list = Field(default_factory=list, description="[{w,c,h}] cdExport layout")
    is_default: bool = False


@router.get("/dashboards")
def list_dashboards(db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """All of this user's dashboards: {name, items, is_default, updated_at}."""
    rows = (db.query(UserDashboard)
            .filter(UserDashboard.user_id == current_user.id)
            .order_by(UserDashboard.name).all())
    return {"dashboards": [
        {"name": r.name, "items": r.items or [], "is_default": bool(r.is_default),
         "updated_at": r.updated_at.isoformat() if r.updated_at else None}
        for r in rows]}


@router.put("/dashboards")
def put_dashboard(payload: DashboardPayload, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Create or replace one dashboard (upsert on user_id+name). Setting
    is_default clears the flag on the user's other dashboards."""
    row = (db.query(UserDashboard)
           .filter(UserDashboard.user_id == current_user.id,
                   UserDashboard.name == payload.name).first())
    if row is None:
        row = UserDashboard(user_id=current_user.id, name=payload.name)
        db.add(row)
    row.items = payload.items
    if payload.is_default:
        (db.query(UserDashboard)
         .filter(UserDashboard.user_id == current_user.id,
                 UserDashboard.id != row.id)
         .update({"is_default": False}, synchronize_session=False))
    row.is_default = payload.is_default
    db.commit()
    db.refresh(row)
    return {"status": "success",
            "dashboard": {"name": row.name, "items": row.items or [],
                          "is_default": bool(row.is_default)}}


@router.delete("/dashboards/{name}")
def delete_dashboard(name: str, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """Delete one dashboard; 409 when it is the user's last one — the reference
    cdDelete keeps at least one tab alive (`Keep at least one dashboard`)."""
    q = db.query(UserDashboard).filter(UserDashboard.user_id == current_user.id)
    if q.count() <= 1:
        raise HTTPException(status_code=409, detail="Keep at least one dashboard")
    deleted = q.filter(UserDashboard.name == name).delete(synchronize_session=False)
    db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail=f"no dashboard '{name}'")
    return {"status": "success"}
