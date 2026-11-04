from typing import List, Optional
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from sqlalchemy import desc, func

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.economic_indicators import EconomicIndicator, TreasuryYieldCurve, SP500History, EurodollarFutures, CmeFedwatch, FredSeriesMetadata
from app.schemas.economic_indicators import EconomicIndicatorResponse, TreasuryYieldCurveResponse, SP500HistoryResponse, EurodollarFuturesResponse, CmeFedwatchResponse, FredSeriesMetadataResponse

router = APIRouter()

from fastapi.responses import JSONResponse
import json
import logging
import re
import threading

from app.core.security import verify_internal_key
from app.core.redis import redis_cache
from app.core.cache_config import (
    CACHE_TTL_ECONOMIC_INDICATORS,
    MAX_QUERY_LIMIT,
)
from app.core.cache_guard import guard_cache_key, guard_date_range
from app.core.cache_helpers import cache_read_through

# Configure logging
logger = logging.getLogger(__name__)


def _start_tracked(job_key: str, fn, *args, **kwargs) -> None:
    """Run a scrape in a daemon thread, recorded in job_runs under job_key.

    Before 2026-10-13 these endpoints fired bare threads: a scrape started
    from the Admin buttons never appeared in the Runbook, so jobs showed
    'Never recorded' / 'Untracked' even right after a successful run. The
    keys here are the same ones system_registry.py lists, so the Runbook and
    job_runs agree. track_job owns a short-lived session and never re-raises
    tracking failures; the try/except here still records a crashed scraper.
    """
    from app.services.job_tracking import track_job, with_result_handling

    def _body():
        try:
            with track_job(job_key, "manual") as run:
                with_result_handling(run, fn(*args, **kwargs))
        except Exception as e:
            logger.error(f"tracked scrape {job_key} crashed: {e}", exc_info=True)

    threading.Thread(target=_body, daemon=True).start()

def fetch_sp500_db(db: Session, start_date, end_date, limit):
    query = db.query(SP500History.trade_date, SP500History.close, SP500History.pe_ratio)
    if start_date:
        query = query.filter(SP500History.trade_date >= start_date)
    if end_date:
        query = query.filter(SP500History.trade_date <= end_date)
        
    query = query.order_by(desc(SP500History.trade_date)).limit(limit)
    return [{"trade_date": str(r.trade_date), "close": float(r.close) if r.close else None, "pe_ratio": float(r.pe_ratio) if r.pe_ratio else None} for r in query.all()]

@router.get("/SP500")
async def get_sp500_history(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = Query(500, le=MAX_QUERY_LIMIT, description="Max number of records"),
    db: Session = Depends(get_db)
):
    cache_key = f"economic:sp500:{limit}:{start_date}:{end_date}"
    # Part 2 / Phases 3+5. This endpoint built a Redis key straight from the
    # query string and had no ceiling on ``limit``, no single-flight (N readers
    # of one cold key meant N identical 25 000-row queries), no try/except around
    # the Redis read (a dead cache turned into a 500), and a falsy [] was never
    # served, so an empty table was re-queried on every hit. The ceiling is
    # MAX_QUERY_LIMIT rather than a smaller number because the live pages request
    # 25 000 rows; the key shape and TTL are unchanged.
    guard_date_range(start_date, end_date)
    guard_cache_key(cache_key)
    res = await cache_read_through(
        cache_key,
        CACHE_TTL_ECONOMIC_INDICATORS,
        lambda: run_in_threadpool(fetch_sp500_db, db, start_date, end_date, limit),
        cache_empty=True,
    )
    return JSONResponse(content=res)

@router.get("/scrape/SP500")
def trigger_scrape_sp500(_: bool = Depends(verify_internal_key)):
    from app.scrapers.sp500_scraper import scrape_sp500

    _start_tracked("sp500", scrape_sp500)
    return {"message": "Scraping started for S&P 500 in background"}

def fetch_yield_curve_db(db: Session, start_date, end_date, limit):
    query = db.query(TreasuryYieldCurve)
    if start_date:
        query = query.filter(TreasuryYieldCurve.report_date >= start_date)
    if end_date:
        query = query.filter(TreasuryYieldCurve.report_date <= end_date)
        
    query = query.order_by(desc(TreasuryYieldCurve.report_date)).limit(limit)
    res = []
    for r in query.all():
        res.append({
            "report_date": str(r.report_date),
            "month_1": float(r.month_1) if r.month_1 is not None else None,
            "month_1_5": float(r.month_1_5) if r.month_1_5 is not None else None,
            "month_2": float(r.month_2) if r.month_2 is not None else None,
            "month_3": float(r.month_3) if r.month_3 is not None else None,
            "month_4": float(r.month_4) if r.month_4 is not None else None,
            "month_6": float(r.month_6) if r.month_6 is not None else None,
            "year_1": float(r.year_1) if r.year_1 is not None else None,
            "year_2": float(r.year_2) if r.year_2 is not None else None,
            "year_3": float(r.year_3) if r.year_3 is not None else None,
            "year_5": float(r.year_5) if r.year_5 is not None else None,
            "year_7": float(r.year_7) if r.year_7 is not None else None,
            "year_10": float(r.year_10) if r.year_10 is not None else None,
            "year_20": float(r.year_20) if r.year_20 is not None else None,
            "year_30": float(r.year_30) if r.year_30 is not None else None
        })
    return res

@router.get("/yield-curve")
async def get_yield_curve(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = Query(500, le=MAX_QUERY_LIMIT, description="Max number of records"),
    db: Session = Depends(get_db)
):
    cache_key = f"economic:yieldcurve:{limit}:{start_date}:{end_date}"
    # Same gate as /SP500 above: ceiling on limit, validated range, single-flight
    # read-through, and an empty answer that stops re-hitting Postgres.
    guard_date_range(start_date, end_date)
    guard_cache_key(cache_key)
    res = await cache_read_through(
        cache_key,
        CACHE_TTL_ECONOMIC_INDICATORS,
        lambda: run_in_threadpool(fetch_yield_curve_db, db, start_date, end_date, limit),
        cache_empty=True,
    )
    return JSONResponse(content=res)

@router.get("/yield-curve/summary")
def get_yield_curve_summary(db: Session = Depends(get_db)):
    """One-call curve state for the three /bonds pages.

    Everything here is READ from treasury_yield_curves (written by the
    treasury_gov scraper) — nothing is fabricated. A maturity the source did
    not publish for the latest session comes back as None and the frontend
    shows an em-dash rather than a substitute.

    Inversion rules follow the two spreads FRED tracks (T10Y2Y, T10Y3M):
    spread < 0  -> inverted; 0 <= spread < 0.20 -> flat (conventional
    threshold band); otherwise normal. Null spreads stay null -> unknown.
    """
    latest = (db.query(TreasuryYieldCurve)
              .order_by(desc(TreasuryYieldCurve.report_date)).first())
    if not latest:
        raise HTTPException(status_code=404,
                            detail="No treasury data scraped yet "
                                   "(run scraper:treasury_gov)")

    def _f(v):
        return round(float(v), 4) if v is not None else None

    def _shape(spread):
        if spread is None:
            return "unknown"
        if spread < 0:
            return "inverted"
        if spread < 0.20:
            return "flat"
        return "normal"

    y = {
        "1Mo": _f(latest.month_1), "1.5Mo": _f(latest.month_1_5),
        "2Mo": _f(latest.month_2), "3Mo": _f(latest.month_3),
        "4Mo": _f(latest.month_4), "6Mo": _f(latest.month_6),
        "1Y": _f(latest.year_1),  "2Y": _f(latest.year_2),
        "3Y": _f(latest.year_3),  "5Y": _f(latest.year_5),
        "7Y": _f(latest.year_7),  "10Y": _f(latest.year_10),
        "20Y": _f(latest.year_20), "30Y": _f(latest.year_30),
    }
    spread_10y2y = (round(y["10Y"] - y["2Y"], 4)
                    if y["10Y"] is not None and y["2Y"] is not None else None)
    spread_10y3m = (round(y["10Y"] - y["3Mo"], 4)
                    if y["10Y"] is not None and y["3Mo"] is not None else None)

    total_rows = db.query(func.count(TreasuryYieldCurve.id)).scalar()

    return {
        "as_of": str(latest.report_date),
        # last (re)fetch of THIS row — the scraper stamps updated_at inside the
        # upsert; rows written before the column existed report None honestly.
        "last_updated": (latest.updated_at.isoformat()
                         if latest.updated_at else None),
        "rows_stored": total_rows,
        "curve": y,
        "spreads": {
            "10Y2Y": {"value": spread_10y2y, "shape": _shape(spread_10y2y),
                       "inverted": (spread_10y2y is not None and spread_10y2y < 0)},
            "10Y3M": {"value": spread_10y3m, "shape": _shape(spread_10y3m),
                       "inverted": (spread_10y3m is not None and spread_10y3m < 0)},
        },
        # overall curve shape takes the worse reading of the two benchmark
        # spreads, the way the press describes "the curve" inverting
        "curve_shape": _shape(min(
            [s for s in (spread_10y2y, spread_10y3m) if s is not None],
            default=None)) if any(s is not None for s in (spread_10y2y, spread_10y3m))
            else "unknown",
        "source": "U.S. Department of the Treasury — Daily Par Yield Curve Rates",
    }


@router.get("/scrape/yield-curve")
def trigger_scrape_yield_curve(_: bool = Depends(verify_internal_key)):
    from app.scrapers.treasury_gov_scraper import scrape_treasury_gov

    _start_tracked("treasury_gov", scrape_treasury_gov, mode="incremental")
    return {"message": "Scraping started for US Treasury Yield Curve (Treasury.gov) in background"}

@router.get("/scrape/treasury-gov")
def trigger_scrape_treasury_gov(mode: str = Query("incremental", description="incremental | backfill_recent | backfill"), _: bool = Depends(verify_internal_key)):
    from app.scrapers.treasury_gov_scraper import scrape_treasury_gov

    _start_tracked("treasury_gov", scrape_treasury_gov, mode)
    return {"message": f"Treasury.gov CSV scraper started (mode={mode}). This fetches all maturities and fills missing days."}

@router.post("/clear-cache", dependencies=[Depends(get_current_admin)])
async def clear_yield_cache():
    """Clear all cached economic indicator data from Redis.

    Admin-only POST: it was a public GET, so any crawler or third-party page
    could purge the whole `economic:*` namespace and force every reader to hit
    the DB at once (the CSRF middleware cannot protect a GET).
    """
    # SCAN + batched non-blocking UNLINK (was blocking KEYS + per-key DELETE).
    keys = await redis_cache.scan_iter("economic:*")
    count = await redis_cache.unlink(*keys) if keys else 0
    return {"message": f"Cleared {count} cached keys."}

# ──────────────────── Eurodollar Futures (Investing.com) ────────────────────

@router.get("/scrape/eurodollar-futures")
def trigger_scrape_eurodollar(force: bool = False,
                              _: bool = Depends(verify_internal_key)):
    """Background scrape, recorded as ``manual:eurodollar`` in job_runs.

    ``force`` defaults to False, and that is the whole fix for the card that
    said "failed" over healthy data: the scraper's own same-day guard returns
    ``already scraped today`` instead of re-hitting Investing.com, which answers
    a datacenter IP with a 403. Forcing it re-ran a scrape that could only fail
    and wrote that failure on top of a table already holding today's full ladder.
    Pass ``?force=true`` to re-pull anyway (e.g. after a Yahoo-fallback partial).
    """
    from app.scrapers.eurodollar_scraper import scrape_eurodollar_futures

    _start_tracked("manual:eurodollar", scrape_eurodollar_futures, save_db=True, force=force)
    return {"message": "Scraping started for Eurodollar Futures (Investing.com) in background"
                       + ("" if force else " — skipped automatically if today's snapshot is already stored")}

@router.get("/scrape/sp500-pe")
def trigger_scrape_sp500_pe(_: bool = Depends(verify_internal_key)):
    from app.scrapers.sp500_pe_scraper import scrape_sp500_pe

    _start_tracked("sp500_pe", scrape_sp500_pe)
    return {"message": "S&P 500 PE Ratio scraper started in background"}

# ──────────────────── CME FedWatch ────────────────────

@router.get("/scrape/cme-fedwatch")
def trigger_scrape_cmefedwatch(_: bool = Depends(verify_internal_key)):
    from app.scrapers.cmefedwatch_scraper import scrape_cme_fedwatch

    _start_tracked("manual:cme_fedwatch", scrape_cme_fedwatch, force=True)
    return {"message": "CME FedWatch scraper started in background"}


@router.get("/cme-fedwatch/latest", response_model=List[CmeFedwatchResponse])
def get_cme_fedwatch_latest(db: Session = Depends(get_db)):
    """Get the most recent snapshot of CME FedWatch probabilities."""
    from app.schemas.economic_indicators import CmeFedwatchResponse
    from app.models.economic_indicators import CmeFedwatch
    
    latest_date = db.query(CmeFedwatch.scrape_date).order_by(desc(CmeFedwatch.scrape_date)).first()
    if not latest_date:
        return []
    
    return db.query(
        CmeFedwatch.id,
        CmeFedwatch.scrape_date,
        CmeFedwatch.meeting_date,
        CmeFedwatch.rate_range,
        CmeFedwatch.probability
    ).filter(
        CmeFedwatch.scrape_date == latest_date[0]
    ).order_by(CmeFedwatch.meeting_date, CmeFedwatch.rate_range).all()

def _split_range(label: str):
    """'375-400' -> (375, 400)؛ None لو الشكل مش معروف."""
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", label or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


@router.get("/cme-fedwatch/dashboard")
def get_cme_fedwatch_dashboard(db: Session = Depends(get_db)):
    """Everything the two official QuikStrike screens need, from one call.

    "Current"  -> snapshot (Meeting Information + Ease/No Change/Hike) and the
                  Target Rate / Probability horizon table.
    "Probabilities" -> the Fed Fund Futures ladder plus the conditional matrix.

    The per-meeting Ease/No Change/Hike and contract/mid-price pairings are
    *derived* from the stored matrix and ladder rather than scraped per tab:
    the tool only renders the nearest meeting without a browser-side click, and
    the arithmetic is the same partition the tool itself uses (buckets below the
    current target = ease, equal = no change, above = hike).
    """
    from app.models.economic_indicators import (
        CmeFedwatchSnapshot, CmeFedwatchHorizon, CmeFedwatchFutures,
        CmeFedwatchMeeting, CmeFedwatchDotPlot,
    )

    latest = db.query(CmeFedwatch.scrape_date).order_by(desc(CmeFedwatch.scrape_date)).first()
    if not latest:
        raise HTTPException(status_code=404, detail="No CME FedWatch data scraped yet")
    d = latest[0]

    snap = (db.query(CmeFedwatchSnapshot)
            .filter(CmeFedwatchSnapshot.scrape_date == d).first())
    horizon_rows = (db.query(CmeFedwatchHorizon)
                    .filter(CmeFedwatchHorizon.scrape_date == d)
                    .order_by(CmeFedwatchHorizon.meeting_date,
                              CmeFedwatchHorizon.rate_range, CmeFedwatchHorizon.horizon).all())
    futures = (db.query(CmeFedwatchFutures)
               .filter(CmeFedwatchFutures.scrape_date == d)
               .order_by(CmeFedwatchFutures.contract_month).all())
    matrix = (db.query(CmeFedwatch)
              .filter(CmeFedwatch.scrape_date == d)
              .order_by(CmeFedwatch.meeting_date, CmeFedwatch.rate_range).all())
    # per-meeting scraped Current header (all nine tabs) — authoritative odds
    meeting_tbl = (db.query(CmeFedwatchMeeting)
                   .filter(CmeFedwatchMeeting.scrape_date == d).all())
    by_meeting = {str(m.meeting_date)[:10]: m for m in meeting_tbl}
    dot_rows = (db.query(CmeFedwatchDotPlot)
                .filter(CmeFedwatchDotPlot.scrape_date == d)
                .order_by(CmeFedwatchDotPlot.year_order,
                          desc(CmeFedwatchDotPlot.target_rate)).all())

    cur_lo, cur_hi = _split_range(snap.current_target_range if snap else None)

    # horizon rows -> one {columns, labels, rows} block PER meeting, keyed by
    # the ISO date string the matrix uses too. The scraper now stores every tab,
    # so switching a meeting shows that meeting's real Now/1D/1W/1M table.
    HCOL = ["now", "1d", "1w", "1m", "3m", "6m", "1y"]
    horizon_by_meeting: dict[str, dict] = {}
    for r in horizon_rows:
        md = str(r.meeting_date)[:10]
        blk = horizon_by_meeting.setdefault(md, {"columns": [], "labels": {}, "buckets": []})
        if r.horizon not in blk["columns"]:
            blk["columns"].append(r.horizon)
            blk["labels"][r.horizon] = r.horizon_label
        entry = next((b for b in blk["buckets"] if b["rate_range"] == r.rate_range), None)
        if entry is None:
            entry = {"rate_range": r.rate_range, "is_current": r.is_current, "values": {}}
            blk["buckets"].append(entry)
        entry["is_current"] = entry["is_current"] or r.is_current
        if r.probability is not None:
            entry["values"][r.horizon] = float(r.probability)
    for blk in horizon_by_meeting.values():
        blk["columns"].sort(key=lambda h: HCOL.index(h) if h in HCOL else 99)
        blk["buckets"].sort(key=lambda b: _split_range(b["rate_range"])[0] or 0)

    def _horizon_block(md: str) -> dict:
        blk = horizon_by_meeting.get(md)
        if not blk:
            return {"columns": [], "labels": {}, "rows": []}
        return {"columns": blk["columns"], "labels": blk["labels"], "rows": blk["buckets"]}

    # conditional matrix -> one entry per meeting, in date order
    meetings = []
    for m in matrix:
        entry = next((x for x in meetings if x["meeting_date"] == m.meeting_date), None)
        if entry is None:
            entry = {"meeting_date": m.meeting_date, "buckets": {}, "order": []}
            meetings.append(entry)
        p = float(m.probability) if m.probability is not None else 0.0
        entry["buckets"][m.rate_range] = p
        entry["order"].append(m.rate_range)

    ladder_by_month = {}
    for f in futures:
        if f.contract_month:
            ladder_by_month[f.contract_month.strftime("%Y-%m")] = f

    out_meetings = []
    for e in meetings:
        md = e["meeting_date"]
        md_str = str(md)[:10]
        try:
            key = date.fromisoformat(md_str).strftime("%Y-%m")
        except ValueError:
            key = None
        fut = ladder_by_month.get(key)
        scraped = by_meeting.get(md_str)

        # Ease/No Change/Hike: the tool's OWN printed aggregate for that meeting
        # (scraped tab) is authoritative; the derived partition is the fallback
        # for any meeting the replay could not reach.
        if scraped and scraped.prob_ease is not None:
            ease = float(scraped.prob_ease)
            no_change = float(scraped.prob_no_change) if scraped.prob_no_change is not None else 0.0
            hike = float(scraped.prob_hike) if scraped.prob_hike is not None else 0.0
        else:
            ease = no_change = hike = 0.0
            for rr, p in e["buckets"].items():
                lo, hi = _split_range(rr)
                if lo is None or cur_lo is None:
                    continue
                if hi <= cur_lo:
                    ease += p
                elif lo >= cur_hi:
                    hike += p
                else:
                    no_change += p

        out_meetings.append({
            "meeting_date": md_str,
            "meeting_label": (scraped.meeting_label if scraped else None),
            "buckets": e["buckets"],
            "rate_ranges": sorted(e["order"], key=lambda x: _split_range(x)[0] or 0),
            "prob_ease": round(ease, 1), "prob_no_change": round(no_change, 1),
            "prob_hike": round(hike, 1),
            "contract": (scraped.contract if scraped and scraped.contract else (fut.contract if fut else None)),
            "mid_price": (float(scraped.mid_price) if scraped and scraped.mid_price is not None
                          else (float(fut.price) if fut and fut.price is not None else None)),
            "expires": scraped.expires if scraped else None,
            "prior_volume": scraped.prior_volume if scraped else None,
            "prior_oi": scraped.prior_oi if scraped else None,
            "horizon": _horizon_block(md_str),
        })

    out_meetings.sort(key=lambda x: x["meeting_date"])

    # dot plot -> ordered year columns + one row per target rate (descending)
    dot_years: list[str] = []
    dot_by_rate: dict[float, dict] = {}
    for r in dot_rows:
        if r.year_label not in dot_years:
            dot_years.append(r.year_label)
        tr = float(r.target_rate)
        row = dot_by_rate.setdefault(tr, {"target_rate": tr, "is_median": False, "cells": {}})
        row["is_median"] = row["is_median"] or bool(r.is_median)
        row["cells"][r.year_label] = {
            "count": r.count,
            "bg_color": r.bg_color,
        }
    dotplot = {
        "years": dot_years,
        "rows": sorted(dot_by_rate.values(), key=lambda x: -x["target_rate"]),
    }

    # backward-compatible top-level "horizon" = the nearest meeting's block
    nearest_md = (out_meetings[0]["meeting_date"] if out_meetings
                  else (str(snap.meeting_date)[:10] if snap and snap.meeting_date else None))
    nearest_horizon = _horizon_block(nearest_md) if nearest_md else {
        "columns": [], "labels": {}, "rows": []}

    return {
        "scrape_date": d.isoformat(),
        "data_as_of": snap.data_as_of if snap else None,
        "current_target_range": snap.current_target_range if snap else None,
        "chart_title": snap.chart_title if snap else None,
        "chart_labels": snap.chart_labels if snap else None,
        "snapshot": None if not snap else {
            "meeting_label": snap.meeting_label, "contract": snap.contract,
            "expires": snap.expires, "mid_price": float(snap.mid_price) if snap.mid_price else None,
            "prior_volume": snap.prior_volume, "prior_oi": snap.prior_oi,
            "prob_ease": float(snap.prob_ease) if snap.prob_ease is not None else None,
            "prob_no_change": float(snap.prob_no_change) if snap.prob_no_change is not None else None,
            "prob_hike": float(snap.prob_hike) if snap.prob_hike is not None else None,
        },
        "horizon": nearest_horizon,
        "futures": [{
            "contract": f.contract, "price": float(f.price) if f.price is not None else None,
            "contract_month": f.contract_month.isoformat() if f.contract_month else None,
        } for f in futures],
        "meetings": out_meetings,
        "dotplot": dotplot,
        "all_rate_ranges": sorted({rr for m in out_meetings for rr in m["buckets"]},
                                  key=lambda x: _split_range(x)[0] or 0),
    }


@router.get("/scrape/{indicator_code}")
def trigger_scrape_specific(indicator_code: str, mode: str = Query("update", description="update or historical"), _: bool = Depends(verify_internal_key)):
    if indicator_code.upper() == "SP500_PE":
        from app.scrapers.gurufocus_scraper import scrape_gurufocus_indicator
        
        url = "https://www.gurufocus.com/economic_indicators/57/sp-500-pe-ratio"
        
        guru_mode = "incremental" if mode == "update" else "full"
        max_pages = 3 if guru_mode == "incremental" else None
        
        _start_tracked("manual:gurufocus", scrape_gurufocus_indicator, url, indicator_code.upper(), guru_mode, max_pages)
        return {"message": f"GuruFocus Scraping started for {indicator_code} in background (mode={guru_mode})"}
    
    if indicator_code.upper() == "SP500_EY":
        from app.scrapers.gurufocus_scraper import scrape_gurufocus_indicator
        
        url = "https://www.gurufocus.com/economic_indicators/151/sp-500-earnings-yield"
        
        # mode == "update" -> incremental (3 pages), mode == "historical" -> full (all pages)
        guru_mode = "incremental" if mode == "update" else "full"
        max_pages = 3 if guru_mode == "incremental" else None
        
        _start_tracked("manual:gurufocus", scrape_gurufocus_indicator, url, indicator_code.upper(), guru_mode, max_pages)
        return {"message": f"GuruFocus Scraping started for {indicator_code} in background (mode={guru_mode})"}
        
    from app.scrapers.fred_scraper import scrape_fred_indicator
    # tracked under the SAME fred:<CODE> key the cron and Run Now use, so an
    # Admin-button scrape shows up in the Runbook entry for that indicator
    _start_tracked(f"fred:{indicator_code.upper()}", scrape_fred_indicator, indicator_code.upper())
    return {"message": f"Scraping started for {indicator_code} in background"}

@router.get("/scrape")
def trigger_scrape_all(_: bool = Depends(verify_internal_key)):
    from app.scrapers.fred_scraper import scrape_all_fred

    # aggregate run of every FRED series — its own key so it never clutters
    # the per-indicator entries
    _start_tracked("fred:ALL", scrape_all_fred)
    return {"message": "Scraping started for all indicators in background"}

# ──────────────────── Eurodollar Futures ────────────────────

@router.get("/eurodollar-futures/latest", response_model=List[EurodollarFuturesResponse])
def get_eurodollar_futures_latest(db: Session = Depends(get_db)):
    """Get the most recent snapshot of all Eurodollar futures contracts."""
    latest_date = db.query(func.max(EurodollarFutures.scrape_date)).scalar()
    if not latest_date:
        return []
    return db.query(EurodollarFutures).filter(EurodollarFutures.scrape_date == latest_date).all()


@router.get("/eurodollar-futures/history/{contract}", response_model=List[EurodollarFuturesResponse])
def get_eurodollar_futures_history(
    contract: str,
    limit: int = Query(365, le=MAX_QUERY_LIMIT, description="Max number of daily records"),
    db: Session = Depends(get_db)
):
    """Get historical data for a specific Eurodollar futures contract."""
    rows = db.query(EurodollarFutures).filter(
        EurodollarFutures.contract == contract
    ).order_by(desc(EurodollarFutures.scrape_date)).limit(limit).all()
    return rows


@router.get("/eurodollar-futures/dates")
def get_eurodollar_futures_dates(db: Session = Depends(get_db)):
    """Get all available scrape dates for Eurodollar futures."""
    dates = db.query(EurodollarFutures.scrape_date).distinct().order_by(desc(EurodollarFutures.scrape_date)).all()
    return [d[0] for d in dates]


@router.get("/{indicator_code}/metadata", response_model=FredSeriesMetadataResponse)
def get_fred_series_metadata(
    indicator_code: str,
    db: Session = Depends(get_db)
):
    """Persisted FRED series metadata (units, frequency, seasonal adj, latest
    observation, last-updated stamp, next release date, source agency, FRED url).

    Read by the /economic-indicators pages so the card shows live, DB-backed
    values instead of the old hardcoded frontend metadata.
    """
    row = db.query(FredSeriesMetadata).filter(
        FredSeriesMetadata.indicator_code == indicator_code.upper()
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"No FRED metadata stored for {indicator_code}")
    return row


@router.get("/{indicator_code}", response_model=List[EconomicIndicatorResponse])
def get_economic_indicator(
    indicator_code: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = Query(500, description="Max number of records"),
    db: Session = Depends(get_db)
):
    query = db.query(EconomicIndicator).filter(EconomicIndicator.indicator_code == indicator_code.upper())
    
    if start_date:
        query = query.filter(EconomicIndicator.report_date >= start_date)
    if end_date:
        query = query.filter(EconomicIndicator.report_date <= end_date)
        
    # Order by newest first
    query = query.order_by(desc(EconomicIndicator.report_date)).limit(limit)
    
    return query.all()
