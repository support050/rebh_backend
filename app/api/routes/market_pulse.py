"""
market_pulse.py
===============
FastAPI router for /api/market-pulse.

Reads are public (the TASI regime table is shown on the landing page and the
/market-pulse screen). Every write is admin-only: the rows are the single
source of truth for the index series that RS, breadth and the analytics
pipeline read from, and the daily scripts write them straight to the database
(``scripts/backfill_market_pulse.py``) — no legitimate caller uses these HTTP
mutations.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status, BackgroundTasks
from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.market_pulse import MarketPulse
from app.schemas.market_pulse import (
    MarketPulseCreate,
    MarketPulseRead,
    MarketPulseUpdate,
    MarketPulseAverages,
    MarketPulseStats,
    MarketPulseStats,
    OHLCVCreate,
)
from app.models.market_reports import HistoricalReport
from app.services.market_pulse_calc import (
    OHLCVInput, HistoryRow, compute_signals, build_record, get_calc_settings,
)

router = APIRouter()

# ── Allowlists ──────────────────────────────────────────────────────────────
SORTABLE_COLUMNS = frozenset({
    "date", "open", "high", "low", "close", "volume_traded",
    "change", "change_pct", "volume_change_pct",
    "ema_21", "sma_50", "sma_150", "sma_200",
    "atr", "atr_pct", "tr", "mv", "ftd_r",
    "distribution_days", "cluster",
    "year", "month",
})

ALLOWED_MARKET_PULSE = frozenset({
    "Confirmed uptrend", "Uptrend under pressure", "Market in correction",
})

ALLOWED_OUTLOOKS = frozenset({"FTD", "RD", "PRD", "DD", "SD"})


def _validate_sort_by(sort_by: str | None) -> str:
    if not sort_by:
        return "date"
    if sort_by not in SORTABLE_COLUMNS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid sort_by '{sort_by}'. Allowed: {sorted(SORTABLE_COLUMNS)}",
        )
    return sort_by


def _validate_market_pulse_status(value: str | None) -> str | None:
    if value is not None and value not in ALLOWED_MARKET_PULSE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid market_pulse_status. Allowed: {sorted(ALLOWED_MARKET_PULSE)}",
        )
    return value


def _validate_outlook(value: str | None) -> str | None:
    if value is not None and value not in ALLOWED_OUTLOOKS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid current_outlook. Allowed: {sorted(ALLOWED_OUTLOOKS)}",
        )
    return value


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _avg_query(db: Session, *filters) -> MarketPulseAverages:
    q = db.query(
        func.avg(MarketPulse.change).label("avg_change"),
        func.avg(MarketPulse.change_pct).label("avg_change_pct"),
        func.avg(MarketPulse.volume_change_pct).label("avg_volume_change_pct"),
        func.avg(MarketPulse.ema_21).label("avg_ema_21"),
        func.avg(MarketPulse.sma_50).label("avg_sma_50"),
        func.avg(MarketPulse.sma_150).label("avg_sma_150"),
        func.avg(MarketPulse.sma_200).label("avg_sma_200"),
        func.avg(MarketPulse.rd_count).label("avg_rd_count"),
        func.avg(MarketPulse.distribution_days).label("avg_distribution_days"),
        func.avg(MarketPulse.cluster).label("avg_cluster"),
        func.avg(MarketPulse.distribution_day_fall_of).label("avg_distribution_day_fall_of"),
        func.avg(MarketPulse.day_v_close_21).label("avg_day_v_close_21"),
        func.avg(MarketPulse.atr_pct).label("avg_atr_pct"),
        func.avg(MarketPulse.atr).label("avg_atr"),
        func.avg(MarketPulse.tr).label("avg_tr"),
        func.avg(MarketPulse.high_minus_low).label("avg_high_minus_low"),
        func.avg(MarketPulse.high_minus_prev_close).label("avg_high_minus_prev_close"),
        func.avg(MarketPulse.prev_close_minus_low).label("avg_prev_close_minus_low"),
        func.avg(MarketPulse.opn_close).label("avg_opn_close"),
        func.avg(MarketPulse.close_pct).label("avg_close_pct"),
        func.avg(MarketPulse.mv).label("avg_mv"),
        func.avg(MarketPulse.ftd_r).label("avg_ftd_r"),
    )
    if filters:
        q = q.filter(and_(*filters))
    return MarketPulseAverages(**q.one()._asdict())


def _filters(
    market_pulse_status: str | None,
    current_outlook: str | None,
    year: int | None,
    month: int | None = None,
) -> list:
    f = []
    if market_pulse_status:
        f.append(MarketPulse.market_pulse == market_pulse_status)
    if current_outlook:
        f.append(MarketPulse.current_outlook == current_outlook)
    if year:
        f.append(MarketPulse.year == year)
    if month:
        f.append(MarketPulse.month == month)
    return f


def _get_or_404(db: Session, record_id: int) -> MarketPulse:
    record = db.get(MarketPulse, record_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
    return record


# ─────────────────────────────────────────────────────────────────────────────
# RECALCULATE ALL
# ─────────────────────────────────────────────────────────────────────────────

def _parse_num(val: str | None) -> float | None:
    if not val: return None
    try: return float(val.replace(",", ""))
    except (ValueError, TypeError): return None

def _parse_int(val: str | None) -> int | None:
    if not val: return None
    try: return int(val.replace(",", "").split(".")[0])
    except (ValueError, TypeError): return None

def run_full_recalculation(db: Session):
    db.query(MarketPulse).delete()
    db.commit()

    reports = db.query(HistoricalReport).order_by(HistoricalReport.report_date.asc()).all()
    
    history_buffer = []
    inserted = 0
    calc_settings = get_calc_settings(db)

    for report in reports:
        o, h, lo, c, vol = _parse_num(report.open_price), _parse_num(report.high_price), _parse_num(report.low_price), _parse_num(report.close_price), _parse_num(report.volume_traded)
        if None in (o, h, lo, c, vol):
            continue

        today_in = OHLCVInput(
            date=report.report_date, open=o, high=h, low=lo, close=c, volume_traded=vol, # type: ignore
            value_traded=_parse_num(report.value_traded), no_of_trades=_parse_int(report.no_of_trades)
        )

        recent_history = list(reversed(history_buffer[-200:]))
        signals = compute_signals(today_in, recent_history, settings=calc_settings)
        record_dict = build_record(today_in, signals)
        
        db.add(MarketPulse(**record_dict))
        
        history_buffer.append(
            HistoryRow(
                close=record_dict["close"], volume_traded=record_dict["volume_traded"],
                high=record_dict["high"], low=record_dict["low"],
                ema_21=record_dict.get("ema_21"), atr=record_dict.get("atr"),
                rd_count=record_dict.get("rd_count"), ftd=record_dict.get("ftd"),
                dd_sd=record_dict.get("dd_sd"), current_outlook=record_dict.get("current_outlook"),
                change_pct=record_dict.get("change_pct"),
            )
        )

        inserted += 1
        if inserted % 500 == 0:
            db.commit()
            
    db.commit()

@router.post(
    "/recalculate-all",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Recalculate all market pulse data from historical reports in the background",
    dependencies=[Depends(get_current_admin)],
)
def recalculate_all_data(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    # We run this in the background because it might take a few seconds
    background_tasks.add_task(run_full_recalculation, db)
    return {"message": "Recalculation started in the background. Please wait a few seconds."}


# ─────────────────────────────────────────────────────────────────────────────
# INGEST  (daily scraper endpoint)
# ─────────────────────────────────────────────────────────────────────────────
@router.post(
    "/ingest",
    response_model=MarketPulseRead,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a new daily OHLCV bar and compute all signals",
    dependencies=[Depends(get_current_admin)],
)
def ingest_new_day(payload: OHLCVCreate, db: Session = Depends(get_db)):
    # Idempotency check
    if db.query(MarketPulse).filter(MarketPulse.date == payload.date).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Record for {payload.date} already exists. Use PATCH to update.",
        )

    raw_history = (
        db.query(MarketPulse)
        .order_by(MarketPulse.date.desc())
        .limit(200)
        .all()
    )

    history = [
        HistoryRow(
            close=float(r.close),
            volume_traded=float(r.volume_traded),
            high=float(r.high),
            low=float(r.low),
            ema_21=float(r.ema_21)       if r.ema_21       is not None else None,
            atr=float(r.atr)             if r.atr           is not None else None,
            rd_count=r.rd_count,
            ftd=r.ftd,
            dd_sd=r.dd_sd,
            current_outlook=r.current_outlook,
            change_pct=float(r.change_pct) if r.change_pct is not None else None,
        )
        for r in raw_history
    ]

    today_in = OHLCVInput(
        date=payload.date,
        open=float(payload.open),
        high=float(payload.high),
        low=float(payload.low),
        close=float(payload.close),
        volume_traded=float(payload.volume_traded),
        value_traded=float(payload.value_traded) if payload.value_traded else None,
        no_of_trades=payload.no_of_trades,
    )

    calc_settings = get_calc_settings(db)
    signals = compute_signals(today_in, history, settings=calc_settings)
    record  = MarketPulse(**build_record(today_in, signals))
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# ─────────────────────────────────────────────────────────────────────────────
# LIST
# ─────────────────────────────────────────────────────────────────────────────
@router.get(
    "/",
    response_model=list[MarketPulseRead],
)
def list_records(
    skip:                int           = Query(0,      ge=0, le=100_000),
    limit:               int           = Query(100_000,ge=1, le=100_000),
    market_pulse_status: Optional[str] = Query(None),
    current_outlook:     Optional[str] = Query(None),
    year:                Optional[int] = Query(None,   ge=1990, le=2100),
    month:               Optional[int] = Query(None,   ge=1,    le=12),
    sort_by:             Optional[str] = Query("date"),
    sort_dir:            str           = Query("desc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
):
    market_pulse_status = _validate_market_pulse_status(market_pulse_status)
    current_outlook     = _validate_outlook(current_outlook)
    col_name            = _validate_sort_by(sort_by)

    flt = _filters(market_pulse_status, current_outlook, year, month)
    q   = db.query(MarketPulse)
    if flt:
        q = q.filter(and_(*flt))

    sort_col = getattr(MarketPulse, col_name)
    q = q.order_by(sort_col.asc() if sort_dir == "asc" else sort_col.desc())
    return q.offset(skip).limit(limit).all()


# ─────────────────────────────────────────────────────────────────────────────
# AVERAGES
# ─────────────────────────────────────────────────────────────────────────────
@router.get(
    "/averages",
    response_model=MarketPulseAverages,
)
def get_averages(
    market_pulse_status: Optional[str] = Query(None),
    current_outlook:     Optional[str] = Query(None),
    year:                Optional[int] = Query(None, ge=1990, le=2100),
    db: Session = Depends(get_db),
):
    market_pulse_status = _validate_market_pulse_status(market_pulse_status)
    current_outlook     = _validate_outlook(current_outlook)
    flt = _filters(market_pulse_status, current_outlook, year)
    return _avg_query(db, *flt)


# ─────────────────────────────────────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────────────────────────────────────
@router.get(
    "/stats",
    response_model=MarketPulseStats,
)
def get_stats(
    market_pulse_status: Optional[str] = Query(None),
    current_outlook:     Optional[str] = Query(None),
    year:                Optional[int] = Query(None, ge=1990, le=2100),
    db: Session = Depends(get_db)
):
    market_pulse_status = _validate_market_pulse_status(market_pulse_status)
    current_outlook     = _validate_outlook(current_outlook)
    flt = _filters(market_pulse_status, current_outlook, year)

    q_total = db.query(func.count(MarketPulse.id))
    q_mp_dist = db.query(MarketPulse.market_pulse, func.count(MarketPulse.id)).filter(MarketPulse.market_pulse.isnot(None))
    q_co_dist = db.query(MarketPulse.current_outlook, func.count(MarketPulse.id)).filter(MarketPulse.current_outlook.isnot(None))

    if flt:
        q_total = q_total.filter(and_(*flt))
        q_mp_dist = q_mp_dist.filter(and_(*flt))
        q_co_dist = q_co_dist.filter(and_(*flt))

    total = q_total.scalar()
    mp_dist = dict(q_mp_dist.group_by(MarketPulse.market_pulse).all())
    co_dist = dict(q_co_dist.group_by(MarketPulse.current_outlook).all())

    return MarketPulseStats(
        total_records=total,
        averages=_avg_query(db, *flt),
        market_pulse_distribution=mp_dist,
        current_outlook_distribution=co_dist,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SINGLE RECORD
# ─────────────────────────────────────────────────────────────────────────────
@router.get(
    "/{record_id}",
    response_model=MarketPulseRead,
)
def get_record(record_id: int, db: Session = Depends(get_db)):
    return _get_or_404(db, record_id)


# ─────────────────────────────────────────────────────────────────────────────
# CREATE  (full manual insert)
# ─────────────────────────────────────────────────────────────────────────────
@router.post(
    "/",
    response_model=MarketPulseRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_admin)],
)
def create_record(payload: MarketPulseCreate, db: Session = Depends(get_db)):
    if db.query(MarketPulse).filter(MarketPulse.date == payload.date).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Record for {payload.date} already exists.",
        )
    record = MarketPulse(**payload.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# ─────────────────────────────────────────────────────────────────────────────
# UPDATE
# ─────────────────────────────────────────────────────────────────────────────
@router.patch(
    "/{record_id}",
    response_model=MarketPulseRead,
    dependencies=[Depends(get_current_admin)],
)
def update_record(record_id: int, payload: MarketPulseUpdate, db: Session = Depends(get_db)):
    record = _get_or_404(db, record_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(record, field, value)
    db.commit()
    db.refresh(record)
    return record


# ─────────────────────────────────────────────────────────────────────────────
# DELETE
# ─────────────────────────────────────────────────────────────────────────────
@router.delete(
    "/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(get_current_admin)],
)
def delete_record(record_id: int, db: Session = Depends(get_db)):
    record = _get_or_404(db, record_id)
    db.delete(record)
    db.commit()
