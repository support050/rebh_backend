"""
Stock Screeners API Endpoints
روتر متخصص لكل نوع من أنواع الـ Stock Screeners
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import and_, desc, func, case, literal
from typing import List, Optional
from datetime import date, timedelta
import asyncio
import logging

from app.core.database import get_db, SessionLocal
from app.models.stock_indicators import StockIndicator
from app.models.rs_daily import RSDaily
from app.models.price import Price
from app.core.cache_helpers import (
    cache_read_through,
    make_screener_key,
)
from app.core.cache_config import (
    CACHE_TTL_SCREENERS,
    CACHE_TTL_HISTORICAL_SCREENER,
    CACHE_TTL_HISTORICAL_TREND,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/screeners", tags=["Stock Screeners"])


def safe_float(value):
    """تحويل آمن للأرقام"""
    return float(value) if value is not None else None


def safe_bool(value):
    """تحويل آمن للمنطقيات"""
    return bool(value) if value is not None else False


def screener_to_dict(ind: StockIndicator, rs_rating=None) -> dict:
    """تحويل بيانات المؤشر إلى قاموس"""
    base_dict = {
        'symbol': ind.symbol,
        'company_name': ind.company_name,
        'date': str(ind.date) if ind.date else None,

        # ============ Price & Moving Averages ============
        'close': safe_float(ind.close),
        'sma_10': safe_float(ind.sma_10),
        'sma_21': safe_float(ind.sma_21),
        'sma_50': safe_float(ind.sma_50),
        'sma_150': safe_float(ind.sma_150),
        'sma_200': safe_float(ind.sma_200),
        'sma_30w': safe_float(ind.sma_30w),
        'sma_40w': safe_float(ind.sma_40w),
        'ema10': safe_float(ind.ema10),
        'ema21': safe_float(ind.ema21),

        # ============ 52-Week Stats ============
        'fifty_two_week_high': safe_float(ind.fifty_two_week_high),
        'fifty_two_week_low': safe_float(ind.fifty_two_week_low),
        'percent_off_52w_high': safe_float(ind.percent_off_52w_high),
        'percent_off_52w_low': safe_float(ind.percent_off_52w_low),

        # ============ Technical Indicators ============
        'rsi_14': safe_float(ind.rsi_14),
        'cci': safe_float(ind.cci),
        'aroon_up': safe_float(ind.aroon_up),
        'aroon_down': safe_float(ind.aroon_down),
        'beta': safe_float(ind.beta),

        # ============ Historical 200MA ============
        'sma_200_1m_ago': safe_float(ind.sma_200_1m_ago),
        'sma_200_2m_ago': safe_float(ind.sma_200_2m_ago),
        'sma_200_3m_ago': safe_float(ind.sma_200_3m_ago),
        'sma_200_4m_ago': safe_float(ind.sma_200_4m_ago),
        'sma_200_5m_ago': safe_float(ind.sma_200_5m_ago),

        # ============ Boolean Conditions ============
        'sma50_gt_sma150': safe_bool(ind.sma50_gt_sma150),
        'sma50_gt_sma200': safe_bool(ind.sma50_gt_sma200),
        'sma150_gt_sma200': safe_bool(ind.sma150_gt_sma200),
        'sma200_gt_sma200_1m_ago': safe_bool(ind.sma200_gt_sma200_1m_ago),
        'price_gt_sma18': safe_bool(ind.price_gt_sma18),
        'ema10_gt_sma50': safe_bool(ind.ema10_gt_sma50),
        'ema10_gt_sma200': safe_bool(ind.ema10_gt_sma200),
        'ema21_gt_sma50': safe_bool(ind.ema21_gt_sma50),
        'ema21_gt_sma200': safe_bool(ind.ema21_gt_sma200),
        'sma200_gt_sma200_2m_ago': safe_bool(ind.sma200_gt_sma200_2m_ago),
        'sma200_gt_sma200_3m_ago': safe_bool(ind.sma200_gt_sma200_3m_ago),
        'sma200_gt_sma200_4m_ago': safe_bool(ind.sma200_gt_sma200_4m_ago),
        'sma200_gt_sma200_5m_ago': safe_bool(ind.sma200_gt_sma200_5m_ago),
    }

    if isinstance(rs_rating, dict):
        return {
            **base_dict,
            'rs_12m': rs_rating.get('rs_rating'),
            'rs_rating': rs_rating.get('rs_rating'),
            'rank_1m': rs_rating.get('rank_1m'),
            'rank_3m': rs_rating.get('rank_3m'),
            'rank_6m': rs_rating.get('rank_6m'),
            'rank_9m': rs_rating.get('rank_9m'),
            'rank_12m': rs_rating.get('rank_12m'),
        }
    else:
        return {
            **base_dict,
            'rs_12m': rs_rating,
            'rs_rating': rs_rating,
            'rank_1m': None,
            'rank_3m': None,
            'rank_6m': None,
            'rank_9m': None,
            'rank_12m': None,
        }


def get_latest_date(db: Session) -> date:
    """الحصول على آخر تاريخ متاح في stock_indicators"""
    return db.query(func.max(StockIndicator.date)).scalar()


def get_rs_map(db: Session, target_date) -> dict:
    """
    جلب معلومات الـ RS من rs_daily_v2 لكل سهم في تاريخ محدد.
    Returns: dict {symbol: dict}
    """
    rows = (
        db.query(RSDaily.symbol, RSDaily.rs_rating, RSDaily.rank_1m, RSDaily.rank_3m, RSDaily.rank_6m, RSDaily.rank_9m, RSDaily.rank_12m)
        .filter(RSDaily.date == target_date)
        .all()
    )
    return {
        row.symbol: {
            'rs_rating': row.rs_rating,
            'rank_1m': row.rank_1m,
            'rank_3m': row.rank_3m,
            'rank_6m': row.rank_6m,
            'rank_9m': row.rank_9m,
            'rank_12m': row.rank_12m,
        }
        for row in rows
    }


# ============ SCREENER 1: TREND - 1 MONTH ============
@router.get("/trend-1-month")
async def get_trend_1_month(
    db: Session = Depends(get_db),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    target_date: Optional[str] = Query(None)
):
    """
    🎯 Trend - 1 Month Screener
    Cached with 10-minute TTL.

    Criteria (matches reference screenshot exactly):
    ✅ 50 Day > 150 Day: Yes
    ✅ 50 Day > 200 Day: Yes
    ✅ 150 Day > 200 Day: Yes
    ✅ 200 Day > 200 Day 1 Month Ago: Yes
    ✅ RS 12M: > 69
    ✅ % Off 52 Wk Low: > 30.00%
    ✅ % Off 52 Wk High: > -25.00%
    ✅ Price Vs 50d SMA:  > 0.00%
    ✅ Price Vs 150d SMA: > 0.00%
    ✅ Price Vs 200d SMA: > 0.00%
    ✅ Price Vs 30w SMA:  > 0.00%
    ✅ Price Vs 40w SMA:  > 0.00%
    """
    cache_key = make_screener_key("trend-1-month", target_date, limit, offset)
    
    async def fetch_screener():
        if target_date:
            latest = target_date
        else:
            latest = get_latest_date(db)

        # Fetch RS map for the date
        rs_map = get_rs_map(db, latest)
        # Only keep symbols with RS > 69
        rs_symbols = {sym for sym, data in rs_map.items() if data is not None and data.get('rs_rating') is not None and data.get('rs_rating') > 69}

        query = db.query(StockIndicator).filter(StockIndicator.date == latest)

        query = query.filter(
            and_(
                StockIndicator.symbol.in_(rs_symbols),          # RS 12M > 69
                StockIndicator.sma_50 > StockIndicator.sma_150, # SMA50 > SMA150
                StockIndicator.sma_50 > StockIndicator.sma_200, # SMA50 > SMA200
                StockIndicator.sma_150 > StockIndicator.sma_200,# SMA150 > SMA200
                StockIndicator.sma_200 > StockIndicator.sma_200_1m_ago,  # 200 > 200 1M ago
                StockIndicator.percent_off_52w_low > 30.0,      # Off Low > 30%
                StockIndicator.percent_off_52w_high > -25.0,    # Off High > -25%
                StockIndicator.price_vs_sma_50_percent > 0.0,   # Price > 50d SMA
                StockIndicator.price_vs_sma_150_percent > 0.0,  # Price > 150d SMA
                StockIndicator.price_vs_sma_200_percent > 0.0,  # Price > 200d SMA
                StockIndicator.sma_30w.isnot(None),
                StockIndicator.close > StockIndicator.sma_30w,  # Price > 30w SMA
                StockIndicator.sma_40w.isnot(None),
                StockIndicator.close > StockIndicator.sma_40w,  # Price > 40w SMA
            )
        )

        total = query.count()
        results = query.order_by(StockIndicator.symbol).offset(offset).limit(limit).all()

        return {
            'screener': 'Trend - 1 Month',
            'data': [screener_to_dict(ind, rs_map.get(ind.symbol)) for ind in results],
            'total': total,
            'count': len(results),
        }
    
    result = await cache_read_through(cache_key, CACHE_TTL_SCREENERS, fetch_screener)
    return result


# ============ SCREENER 2: TREND - 2 MONTHS ============
@router.get("/trend-2-months")
async def get_trend_2_months(
    db: Session = Depends(get_db),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    target_date: Optional[str] = Query(None)
):
    """
    🎯 Trend - 2 Months Screener
    Cached with 10-minute TTL.
    """
    cache_key = make_screener_key("trend-2-months", target_date, limit, offset)
    
    async def fetch_screener():
        if target_date:
            latest = target_date
        else:
            latest = get_latest_date(db)

        rs_map = get_rs_map(db, latest)
        rs_symbols = {sym for sym, data in rs_map.items() if data is not None and data.get('rs_rating') is not None and data.get('rs_rating') > 69}

        query = db.query(StockIndicator).filter(StockIndicator.date == latest)

        query = query.filter(
            and_(
                StockIndicator.symbol.in_(rs_symbols),
                StockIndicator.sma_50 > StockIndicator.sma_150,
                StockIndicator.sma_50 > StockIndicator.sma_200,
                StockIndicator.sma_150 > StockIndicator.sma_200,
                StockIndicator.sma_200 > StockIndicator.sma_200_2m_ago,
                StockIndicator.sma_200_1m_ago > StockIndicator.sma_200_2m_ago,
                StockIndicator.percent_off_52w_high > -25.0,
                StockIndicator.percent_off_52w_low > 30.0,
                StockIndicator.price_vs_sma_50_percent > 0.0,
                StockIndicator.price_vs_sma_150_percent > 0.0,
                StockIndicator.price_vs_sma_200_percent > 0.0,
                StockIndicator.sma_30w.isnot(None),
                StockIndicator.close > StockIndicator.sma_30w,
                StockIndicator.sma_40w.isnot(None),
                StockIndicator.close > StockIndicator.sma_40w,
            )
        )

        total = query.count()
        results = query.order_by(StockIndicator.symbol).offset(offset).limit(limit).all()

        return {
            'screener': 'Trend - 2 Months',
            'data': [screener_to_dict(ind, rs_map.get(ind.symbol)) for ind in results],
            'total': total,
            'count': len(results),
        }
    
    result = await cache_read_through(cache_key, CACHE_TTL_SCREENERS, fetch_screener)
    return result


# ============ SCREENER 3: TREND - 4 MONTHS ============
@router.get("/trend-4-months")
async def get_trend_4_months(
    db: Session = Depends(get_db),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    target_date: Optional[str] = Query(None)
):
    """
    🎯 Trend - 4 Months Screener
    Cached with 10-minute TTL.
    """
    cache_key = make_screener_key("trend-4-months", target_date, limit, offset)
    
    async def fetch_screener():
        if target_date:
            latest = target_date
        else:
            latest = get_latest_date(db)

        rs_map = get_rs_map(db, latest)
        rs_symbols = {sym for sym, data in rs_map.items() if data is not None and data.get('rs_rating') is not None and data.get('rs_rating') > 69}

        query = db.query(StockIndicator).filter(StockIndicator.date == latest)

        query = query.filter(
            and_(
                StockIndicator.symbol.in_(rs_symbols),
                StockIndicator.sma_50 > StockIndicator.sma_150,
                StockIndicator.sma_50 > StockIndicator.sma_200,
                StockIndicator.sma_150 > StockIndicator.sma_200,
                StockIndicator.sma_200 > StockIndicator.sma_200_1m_ago,
                StockIndicator.sma_200 > StockIndicator.sma_200_2m_ago,
                StockIndicator.sma_200 > StockIndicator.sma_200_3m_ago,
                StockIndicator.sma_200 > StockIndicator.sma_200_4m_ago,
                StockIndicator.sma_200_1m_ago > StockIndicator.sma_200_2m_ago,
                StockIndicator.sma_200_2m_ago > StockIndicator.sma_200_3m_ago,
                StockIndicator.sma_200_3m_ago > StockIndicator.sma_200_4m_ago,
                StockIndicator.percent_off_52w_high > -25.0,
                StockIndicator.percent_off_52w_low > 30.0,
                StockIndicator.price_vs_sma_50_percent > 0.0,
                StockIndicator.price_vs_sma_150_percent > 0.0,
                StockIndicator.price_vs_sma_200_percent > 0.0,
                StockIndicator.sma_30w.isnot(None),
                StockIndicator.close > StockIndicator.sma_30w,
                StockIndicator.sma_40w.isnot(None),
                StockIndicator.close > StockIndicator.sma_40w,
            )
        )

        total = query.count()
        results = query.order_by(StockIndicator.symbol).offset(offset).limit(limit).all()

        return {
            'screener': 'Trend - 4 Months',
            'data': [screener_to_dict(ind, rs_map.get(ind.symbol)) for ind in results],
            'total': total,
            'count': len(results),
        }
    
    result = await cache_read_through(cache_key, CACHE_TTL_SCREENERS, fetch_screener)
    return result


# ============ SCREENER 4: TREND - 5 MONTHS ============
@router.get("/trend-5-months")
async def get_trend_5_months(
    db: Session = Depends(get_db),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    target_date: Optional[str] = Query(None)
):
    """
    🎯 Trend - 5 Months Screener
    Cached with 10-minute TTL.
    """
    cache_key = make_screener_key("trend-5-months", target_date, limit, offset)
    
    async def fetch_screener():
        if target_date:
            latest = target_date
        else:
            latest = get_latest_date(db)

        rs_map = get_rs_map(db, latest)
        rs_symbols = {sym for sym, data in rs_map.items() if data is not None and data.get('rs_rating') is not None and data.get('rs_rating') > 69}

        query = db.query(StockIndicator).filter(StockIndicator.date == latest)

        query = query.filter(
            and_(
                StockIndicator.symbol.in_(rs_symbols),
                StockIndicator.sma_50 > StockIndicator.sma_150,
                StockIndicator.sma_50 > StockIndicator.sma_200,
                StockIndicator.sma_150 > StockIndicator.sma_200,
                StockIndicator.sma_200 > StockIndicator.sma_200_5m_ago,
                StockIndicator.sma_200_1m_ago > StockIndicator.sma_200_2m_ago,
                StockIndicator.sma_200_2m_ago > StockIndicator.sma_200_3m_ago,
                StockIndicator.sma_200_3m_ago > StockIndicator.sma_200_4m_ago,
                StockIndicator.sma_200_4m_ago > StockIndicator.sma_200_5m_ago,
                StockIndicator.percent_off_52w_high > -25.0,
                StockIndicator.percent_off_52w_low > 30.0,
                StockIndicator.price_vs_sma_50_percent > 0.0,
                StockIndicator.price_vs_sma_150_percent > 0.0,
                StockIndicator.price_vs_sma_200_percent > 0.0,
                StockIndicator.sma_30w.isnot(None),
                StockIndicator.close > StockIndicator.sma_30w,
                StockIndicator.sma_40w.isnot(None),
                StockIndicator.close > StockIndicator.sma_40w,
            )
        )

        total = query.count()
        results = query.order_by(StockIndicator.symbol).offset(offset).limit(limit).all()

        return {
            'screener': 'Trend - 5 Months',
            'data': [screener_to_dict(ind, rs_map.get(ind.symbol)) for ind in results],
            'total': total,
            'count': len(results),
        }
    
    result = await cache_read_through(cache_key, CACHE_TTL_SCREENERS, fetch_screener)
    return result


# ============ SCREENER 5: TREND - 5 MONTHS WIDE ============
@router.get("/trend-5-months-wide")
async def get_trend_5_months_wide(
    db: Session = Depends(get_db),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    target_date: Optional[str] = Query(None)
):
    """
    🎯 Trend - 5 Months Wide Screener
    Cached with 10-minute TTL.
    """
    cache_key = make_screener_key("trend-5-months-wide", target_date, limit, offset)
    
    async def fetch_screener():
        if target_date:
            latest = target_date
        else:
            latest = get_latest_date(db)

        rs_map = get_rs_map(db, latest)

        query = db.query(StockIndicator).filter(StockIndicator.date == latest)

        query = query.filter(
            and_(
                StockIndicator.sma_50 > StockIndicator.sma_200,
                StockIndicator.sma_200 > StockIndicator.sma_200_5m_ago,
                StockIndicator.price_vs_sma_50_percent > 0.0,
                StockIndicator.price_vs_sma_150_percent > 0.0,
                StockIndicator.price_vs_sma_200_percent > 0.0,
                StockIndicator.sma_30w.isnot(None),
                StockIndicator.close > StockIndicator.sma_30w,
                StockIndicator.sma_40w.isnot(None),
                StockIndicator.close > StockIndicator.sma_40w,
            )
        )

        total = query.count()
        results = query.order_by(StockIndicator.symbol).offset(offset).limit(limit).all()

        return {
            'screener': 'Trend - 5 Months Wide',
            'data': [screener_to_dict(ind, rs_map.get(ind.symbol)) for ind in results],
            'total': total,
            'count': len(results),
        }
    
    result = await cache_read_through(cache_key, CACHE_TTL_SCREENERS, fetch_screener)
    return result


# ============ SCREENER 6: POWER PLAY ============
@router.get("/power-play")
async def get_power_play(
    db: Session = Depends(get_db),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    target_date: Optional[str] = Query(None)
):
    """
    🎯 Power Play Screener
    Cached with 10-minute TTL.

    Criteria (matches reference screenshot exactly):
    ✅ % Change 20d:  > -25.00%
    ✅ % Change 15d:  -15.00% to 5.00%
    ✅ % Change 126d: > 85.00%
    ✅ Price Vs 50d SMA:  > 0.00%
    ✅ Price Vs 200d SMA: > 0.00%
    """
    cache_key = make_screener_key("power-play", target_date, limit, offset)
    
    async def fetch_screener():
        if target_date:
            latest = target_date
        else:
            latest = get_latest_date(db)

        rs_map = get_rs_map(db, latest)

        query = db.query(StockIndicator).filter(StockIndicator.date == latest)

        # Power Play uses price_vs_sma percentages and percent_change columns
        # NULL guards ensure we only filter stocks that have calculated change data
        query = query.filter(
            and_(
                StockIndicator.price_vs_sma_50_percent.isnot(None),
                StockIndicator.price_vs_sma_200_percent.isnot(None),
                StockIndicator.percent_change_20d.isnot(None),
                StockIndicator.percent_change_15d.isnot(None),
                StockIndicator.percent_change_126d.isnot(None),
                StockIndicator.price_vs_sma_50_percent > 0.0,    # Price > 50d SMA
                StockIndicator.price_vs_sma_200_percent > 0.0,   # Price > 200d SMA
                StockIndicator.percent_change_20d > -25.0,        # % Change 20d > -25%
                StockIndicator.percent_change_15d >= -15.0,       # % Change 15d >= -15%
                StockIndicator.percent_change_15d <= 5.0,         # % Change 15d <= +5%
                StockIndicator.percent_change_126d > 85.0,        # % Change 126d > +85%
            )
        )

        total = query.count()
        results = query.order_by(StockIndicator.symbol).offset(offset).limit(limit).all()

        return {
            'screener': 'Power Play',
            'data': [screener_to_dict(ind, rs_map.get(ind.symbol)) for ind in results],
            'total': total,
            'count': len(results),
            'description': 'Power Play: أسهم أعلى من SMA 50 وSMA 200 مع شروط تغير السعر',
        }
    
    result = await cache_read_through(cache_key, CACHE_TTL_SCREENERS, fetch_screener)
    return result


# ============ HISTORICAL TREND COUNTS (Minervini Trend Chart) ============

HISTORICAL_TREND_CACHE_KEY = "screener:historical:trend_v4"
HISTORICAL_TREND_COMPUTING_KEY = "screener:historical:trend:computing"
HISTORICAL_TREND_FULL_LIMIT = 6000  # full history for chart — one Redis entry for all periods
# TTL is centralized as CACHE_TTL_HISTORICAL_TREND (24h — data updates once daily)


def _compute_historical_trend_sync(limit: int, *, verbose: bool = False) -> dict:
    """
    Heavy DB aggregation. Runs in a worker thread so it does not block
    the asyncio event loop (and other requests like /api/auth/me).
    """
    import time

    def _log(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    db = SessionLocal()
    try:
        t0 = time.time()
        _log(f"[historical-trend] Loading last {limit} trading dates...")
        date_rows = (
            db.query(StockIndicator.date)
            .distinct()
            .order_by(StockIndicator.date.desc())
            .limit(limit)
            .all()
        )
        if not date_rows:
            return {"title": "Minervini Trend", "series": [], "total_dates": 0}

        dates = sorted(r[0] for r in date_rows)
        recent_dates = (
            db.query(StockIndicator.date.label("d"))
            .distinct()
            .order_by(StockIndicator.date.desc())
            .limit(limit)
            .subquery()
        )
        on_recent = StockIndicator.date == recent_dates.c.d
        on_recent_rs = RSDaily.date == recent_dates.c.d
        _log(f"[historical-trend] Dates ready ({len(dates)} days, {time.time() - t0:.1f}s). Query 1/4: 5M wide...")

        wide_rows = (
            db.query(
                StockIndicator.date,
                func.count(StockIndicator.symbol).label("count"),
            )
            .join(recent_dates, on_recent)
            .filter(
                and_(
                    StockIndicator.sma_50 > StockIndicator.sma_200,
                    StockIndicator.sma_200 > StockIndicator.sma_200_5m_ago,
                    StockIndicator.price_vs_sma_50_percent > 0.0,
                    StockIndicator.price_vs_sma_150_percent > 0.0,
                    StockIndicator.price_vs_sma_200_percent > 0.0,
                    StockIndicator.sma_30w.isnot(None),
                    StockIndicator.close > StockIndicator.sma_30w,
                    StockIndicator.sma_40w.isnot(None),
                    StockIndicator.close > StockIndicator.sma_40w,
                ),
            )
            .group_by(StockIndicator.date)
            .order_by(StockIndicator.date)
            .all()
        )
        wide_map = {str(row.date): int(row.count) for row in wide_rows}
        _log(f"[historical-trend] Query 1/4 done ({time.time() - t0:.1f}s). Query 2/4: 1M trend (RS join)...")

        month1_rows = (
            db.query(
                StockIndicator.date,
                func.count(StockIndicator.symbol).label("count"),
            )
            .join(recent_dates, on_recent)
            .join(
                RSDaily,
                and_(
                    RSDaily.symbol == StockIndicator.symbol,
                    RSDaily.date == StockIndicator.date,
                ),
            )
            .filter(
                on_recent_rs,
                and_(
                    RSDaily.rs_rating > 69,
                    StockIndicator.sma_50 > StockIndicator.sma_150,
                    StockIndicator.sma_50 > StockIndicator.sma_200,
                    StockIndicator.sma_150 > StockIndicator.sma_200,
                    StockIndicator.sma_200 > StockIndicator.sma_200_1m_ago,
                    StockIndicator.percent_off_52w_low > 30.0,
                    StockIndicator.percent_off_52w_high > -25.0,
                    StockIndicator.price_vs_sma_50_percent > 0.0,
                    StockIndicator.price_vs_sma_150_percent > 0.0,
                    StockIndicator.price_vs_sma_200_percent > 0.0,
                    StockIndicator.sma_30w.isnot(None),
                    StockIndicator.close > StockIndicator.sma_30w,
                    StockIndicator.sma_40w.isnot(None),
                    StockIndicator.close > StockIndicator.sma_40w,
                ),
            )
            .group_by(StockIndicator.date)
            .order_by(StockIndicator.date)
            .all()
        )
        month1_map = {str(row.date): int(row.count) for row in month1_rows}
        _log(f"[historical-trend] Query 2/4 done ({time.time() - t0:.1f}s). Query 3/4: 4M trend (RS join)...")

        month4_rows = (
            db.query(
                StockIndicator.date,
                func.count(StockIndicator.symbol).label("count"),
            )
            .join(recent_dates, on_recent)
            .join(
                RSDaily,
                and_(
                    RSDaily.symbol == StockIndicator.symbol,
                    RSDaily.date == StockIndicator.date,
                ),
            )
            .filter(
                on_recent_rs,
                and_(
                    RSDaily.rs_rating > 69,
                    StockIndicator.sma_50 > StockIndicator.sma_150,
                    StockIndicator.sma_50 > StockIndicator.sma_200,
                    StockIndicator.sma_150 > StockIndicator.sma_200,
                    StockIndicator.sma_200 > StockIndicator.sma_200_1m_ago,
                    StockIndicator.sma_200 > StockIndicator.sma_200_2m_ago,
                    StockIndicator.sma_200 > StockIndicator.sma_200_3m_ago,
                    StockIndicator.sma_200 > StockIndicator.sma_200_4m_ago,
                    StockIndicator.sma_200_1m_ago > StockIndicator.sma_200_2m_ago,
                    StockIndicator.sma_200_2m_ago > StockIndicator.sma_200_3m_ago,
                    StockIndicator.sma_200_3m_ago > StockIndicator.sma_200_4m_ago,
                    StockIndicator.percent_off_52w_high > -25.0,
                    StockIndicator.percent_off_52w_low > 30.0,
                    StockIndicator.price_vs_sma_50_percent > 0.0,
                    StockIndicator.price_vs_sma_150_percent > 0.0,
                    StockIndicator.price_vs_sma_200_percent > 0.0,
                    StockIndicator.sma_30w.isnot(None),
                    StockIndicator.close > StockIndicator.sma_30w,
                    StockIndicator.sma_40w.isnot(None),
                    StockIndicator.close > StockIndicator.sma_40w,
                ),
            )
            .group_by(StockIndicator.date)
            .order_by(StockIndicator.date)
            .all()
        )
        month4_map = {str(row.date): int(row.count) for row in month4_rows}
        _log(f"[historical-trend] Query 3/4 done ({time.time() - t0:.1f}s). Query 4/4: Alrayan...")

        alrayan_rows = (
            db.query(
                StockIndicator.date,
                func.count(StockIndicator.symbol).label("count"),
            )
            .join(recent_dates, on_recent)
            .filter(StockIndicator.trend_signal == True)
            .group_by(StockIndicator.date)
            .order_by(StockIndicator.date)
            .all()
        )
        alrayan_map = {str(row.date): int(row.count) for row in alrayan_rows}

        series = [
            {
                "date": str(d),
                "trend_1m": month1_map.get(str(d), 0),
                "trend_4m": month4_map.get(str(d), 0),
                "trend_5m_wide": wide_map.get(str(d), 0),
                "alrayan": alrayan_map.get(str(d), 0),
            }
            for d in dates
        ]

        _log(f"[historical-trend] Complete: {len(series)} points in {time.time() - t0:.1f}s")

        return {
            "title": "Minervini Trend",
            "series": series,
            "total_dates": len(series),
        }
    finally:
        db.close()


@router.get("/historical-trend")
async def get_historical_trend(
    limit: int = Query(6000, ge=1, le=6000),
):
    """
    Historical Trend — reads pre-aggregated table `screener_daily_trend_counts`.
    Run scripts/backfill_screener_daily_trend.py once if the table is empty.
    """
    from app.services.minervini_cache import get_historical_trend_cached

    full = await get_historical_trend_cached(limit)
    if full is None or not full.get("series"):
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "title": "Minervini Trend",
                "series": [],
                "total_dates": 0,
                "message": (
                    "Historical trend data is not loaded yet. "
                    "Run: python scripts/backfill_screener_daily_trend.py"
                ),
            },
        )

    series = full.get("series") or []
    return {
        "status": "ready",
        "title": full.get("title", "Minervini Trend"),
        "series": series,
        "total_dates": len(series),
    }


# ============ HISTORICAL A/D RATING ============
@router.get("/historical-ad-rating")
async def get_historical_ad_rating(
    db: Session = Depends(get_db),
    limit: int = Query(5000, ge=1, le=5000),
    period: str = Query("ALL", description="5D, 1M, 6M, 1Y, 5Y, 10Y, ALL"),
):
    """
    📈 Historical A/D Rating
    Returns per-date stock counts AND percentages for A/D Rating 'A' and 'D'.
    Percentages normalize for market growth (total listed stocks).
    Cached for 1 hour.
    """
    cache_key = f"screener:historical:ad_rating_v3:limit:{limit}"

    async def fetch_historical():
        # Count A ratings
        a_rows = (
            db.query(
                RSDaily.date,
                func.count(RSDaily.symbol).label("count"),
            )
            .filter(RSDaily.acc_dis_rating.like('A%'))
            .group_by(RSDaily.date)
            .order_by(RSDaily.date)
            .all()
        )
        a_map = {str(row.date): int(row.count) for row in a_rows}

        # Count D ratings
        d_rows = (
            db.query(
                RSDaily.date,
                func.count(RSDaily.symbol).label("count"),
            )
            .filter(RSDaily.acc_dis_rating.like('D%'))
            .group_by(RSDaily.date)
            .order_by(RSDaily.date)
            .all()
        )
        d_map = {str(row.date): int(row.count) for row in d_rows}

        # Count total stocks with acc_dis_rating per date (for percentage)
        total_rows = (
            db.query(
                RSDaily.date,
                func.count(RSDaily.symbol).label("count"),
            )
            .filter(RSDaily.acc_dis_rating.isnot(None))
            .group_by(RSDaily.date)
            .order_by(RSDaily.date)
            .all()
        )
        total_map = {str(row.date): int(row.count) for row in total_rows}

        all_dates = sorted(set(a_map.keys()) | set(d_map.keys()))

        if limit and len(all_dates) > limit:
            all_dates = all_dates[-limit:]

        period_upper = period.upper()
        period_start = None
        if period_upper == "5D":
            period_start = date.today() - timedelta(days=7)
        elif period_upper == "1M":
            period_start = date.today() - timedelta(days=30)
        elif period_upper == "6M":
            period_start = date.today() - timedelta(days=180)
        elif period_upper == "1Y":
            period_start = date.today() - timedelta(days=365)
        elif period_upper == "5Y":
            period_start = date.today() - timedelta(days=365 * 5)
        elif period_upper == "10Y":
            period_start = date.today() - timedelta(days=365 * 10)

        if period_start:
            all_dates = [d for d in all_dates if date.fromisoformat(d[:10]) >= period_start]

        series = [
            {
                "date": d,
                "a_rating": a_map.get(d, 0),
                "d_rating": d_map.get(d, 0),
                "total_stocks": total_map.get(d, 1),
                "a_rating_pct": round(a_map.get(d, 0) / max(total_map.get(d, 1), 1) * 100, 2),
                "d_rating_pct": round(d_map.get(d, 0) / max(total_map.get(d, 1), 1) * 100, 2),
            }
            for d in all_dates
        ]

        return {
            "title": "A/D Rating History",
            "series": series,
            "total_dates": len(series),
        }

    result = await cache_read_through(cache_key, CACHE_TTL_HISTORICAL_SCREENER, fetch_historical)
    return result


# ============ SCREENER 11: NEW HIGHS ============
@router.get("/new-highs")
async def get_new_highs(
    period: str = Query("1-month", description="1-week, 1-month, 3-months, 6-months, 52-weeks"),
    db: Session = Depends(get_db),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    target_date: Optional[str] = Query(None)
):
    """
    🚀 New Highs Screener
    """
    cache_key = make_screener_key(f"new-highs-{period}", target_date, limit, offset)

    async def fetch_screener():
        if target_date:
            if isinstance(target_date, str):
                latest = date.fromisoformat(target_date)
            else:
                latest = target_date
        else:
            latest = get_latest_date(db)

        if not latest:
            return {"title": f"New Highs - {period}", "total_count": 0, "results": []}

        # Determine lookback days (calendar days approximation)
        p = period.lower()
        if p == "1-week":
            lookback_days = 7
        elif p == "1-month":
            lookback_days = 30
        elif p == "3-months":
            lookback_days = 90
        elif p == "6-months":
            lookback_days = 180
        elif p == "52-weeks":
            lookback_days = 365
        else:
            lookback_days = 30

        start_date = latest - timedelta(days=lookback_days)

        # Get max high for each symbol within the lookback period
        max_highs = (
            db.query(Price.symbol, func.max(Price.high).label('max_high'))
            .filter(Price.date >= start_date, Price.date <= latest)
            .group_by(Price.symbol)
            .subquery()
        )

        # Get today's prices
        today_prices = (
            db.query(Price.symbol, Price.high)
            .filter(Price.date == latest)
            .subquery()
        )

        # Join to find symbols where today's high >= max_high
        new_high_symbols_query = (
            db.query(today_prices.c.symbol)
            .join(max_highs, today_prices.c.symbol == max_highs.c.symbol)
            .filter(today_prices.c.high >= max_highs.c.max_high)
            .all()
        )
        new_high_symbols = {s[0] for s in new_high_symbols_query}

        if not new_high_symbols:
            return {"title": f"New Highs - {period}", "total_count": 0, "results": []}

        # Get RS map and indicators for these symbols
        rs_map = get_rs_map(db, latest)
        
        inds = (
            db.query(StockIndicator)
            .filter(StockIndicator.date == latest)
            .filter(StockIndicator.symbol.in_(new_high_symbols))
            .all()
        )

        results = []
        for ind in inds:
            rs_data = rs_map.get(ind.symbol)
            results.append(screener_to_dict(ind, rs_data))

        # Sort by RS rating descending
        results.sort(key=lambda x: (x.get('rs_rating') or 0), reverse=True)

        return {
            "title": f"New Highs - {period}",
            "total_count": len(results),
            "results": results[offset : offset + limit],
            "date": str(latest),
        }

    return await cache_read_through(cache_key, CACHE_TTL_SCREENERS, fetch_screener)
