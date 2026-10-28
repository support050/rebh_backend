"""
Market Breadth API
Computes % of stocks above X-day Moving Average for each trading day.
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import case, func
from typing import Optional, List, Tuple
from datetime import date, timedelta
import pandas as pd
import urllib.parse

from app.core.database import get_db
from app.core.cache_config import CACHE_TTL_LEGACY_BREADTH
from app.core.cache_helpers import cache_read_through
from app.models.static_stock_info import StaticStockInfo
from app.models.price import Price
from app.models.rs_daily import RSDaily

router = APIRouter(prefix="/market-breadth", tags=["Market Breadth"])

# Both legacy cache families share the same 24h TTL (cache_config.CACHE_TTL_LEGACY_BREADTH).
AD_RATING_CACHE_TTL = CACHE_TTL_LEGACY_BREADTH
DASHBOARD_CACHE_TTL = CACHE_TTL_LEGACY_BREADTH
DEFAULT_CHART_LIMIT = 3000


def _period_min_date(period: str) -> Optional[date]:
    """Calendar cutoff for period filter (matches _apply_period_filter)."""
    today = date.today()
    period = period.upper()
    if period == "5D":
        return today - timedelta(days=7)
    if period == "1M":
        return today - timedelta(days=30)
    if period == "6M":
        return today - timedelta(days=180)
    if period == "1Y":
        return today - timedelta(days=365)
    if period == "5Y":
        return today - timedelta(days=365 * 5)
    if period == "10Y":
        return today - timedelta(days=365 * 10)
    return None


def _window_mean(values: list[float], window: int, index: int) -> float:
    if index < window - 1:
        return 0.0
    chunk = values[index - window + 1 : index + 1]
    return sum(chunk) / len(chunk) if chunk else 0.0


def _apply_period_filter(
    data_all: list,
    period: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> list:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date cannot be after end_date")

    filtered_data = data_all

    if start_date or end_date:
        if start_date:
            start_str = start_date.isoformat()
            filtered_data = [d for d in filtered_data if d.get('time', '') >= start_str]
        if end_date:
            end_str = end_date.isoformat()
            filtered_data = [d for d in filtered_data if d.get('time', '') <= end_str]
    else:
        target_date = _period_min_date(period)
        if target_date:
            target_str = target_date.isoformat()
            filtered_data = [d for d in filtered_data if d.get('time', '') >= target_str]

    # Return a new dict without date_obj to avoid mutating the cached original object
    return [{k: v for k, v in d.items() if k != 'date_obj'} for d in filtered_data]


def _compute_breadth_from_prices_df(df: pd.DataFrame) -> list:
    if df.empty:
        return []

    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values(by=['symbol', 'date'])

    for ma in [20, 50, 150, 200]:
        df[f'sma_{ma}'] = df.groupby('symbol')['close'].transform(lambda x: x.rolling(ma).mean())
        df[f'above_{ma}'] = (df['close'] > df[f'sma_{ma}']).astype(int)
        df[f'has_sma_{ma}'] = df[f'sma_{ma}'].notna().astype(int)

    grouped = df.groupby('date').agg(
        above_20=('above_20', 'sum'),
        has_sma_20=('has_sma_20', 'sum'),
        above_50=('above_50', 'sum'),
        has_sma_50=('has_sma_50', 'sum'),
        above_150=('above_150', 'sum'),
        has_sma_150=('has_sma_150', 'sum'),
        above_200=('above_200', 'sum'),
        has_sma_200=('has_sma_200', 'sum'),
    ).reset_index()

    for ma in [20, 50, 150, 200]:
        grouped[f'pct_above_{ma}'] = (
            grouped[f'above_{ma}'] / grouped[f'has_sma_{ma}'] * 100
        ).fillna(0).round(2)

    final_df = grouped[['date', 'pct_above_20', 'pct_above_50', 'pct_above_150', 'pct_above_200']]
    final_df = final_df[final_df['pct_above_20'] > 0]

    data_all = []
    for i, row in final_df.iterrows():
        subset = final_df.iloc[: i + 1]

        def get_sma(attr: str, n: int) -> float:
            if len(subset) < n:
                return 0.0
            vals = subset[attr].tail(n).dropna()
            return float(vals.mean()) if len(vals) else 0.0

        d = row['date'].date() if hasattr(row['date'], 'date') else row['date']
        data_all.append({
            'time': d.isoformat(),
            'date_obj': d,
            'total': 0,
            'pct_above_20': float(row['pct_above_20']),
            'pct_above_50': float(row['pct_above_50']),
            'pct_above_150': float(row['pct_above_150']),
            'pct_above_200': float(row['pct_above_200']),
            'ma50_20': get_sma('pct_above_20', 50),
            'ma200_20': get_sma('pct_above_20', 200),
            'ma50_50': get_sma('pct_above_50', 50),
            'ma200_50': get_sma('pct_above_50', 200),
            'ma50_150': get_sma('pct_above_150', 50),
            'ma200_150': get_sma('pct_above_150', 200),
            'ma50_200': get_sma('pct_above_200', 50),
            'ma200_200': get_sma('pct_above_200', 200),
        })

    return data_all


def _compute_filtered_breadth(
    db: Session,
    statuses: List[str],
    period: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> dict:
    symbol_rows = (
        db.query(StaticStockInfo.symbol)
        .filter(StaticStockInfo.approval_with_controls.in_(statuses))
        .all()
    )
    symbols = [str(r[0]) for r in symbol_rows]
    if not symbols:
        return {"count": 0, "data": []}

    rows = (
        db.query(Price.symbol, Price.date, Price.close)
        .filter(Price.symbol.in_(symbols))
        .order_by(Price.symbol, Price.date)
        .all()
    )
    if not rows:
        return {"count": 0, "data": []}

    df = pd.DataFrame(rows, columns=['symbol', 'date', 'close'])
    data_all = _compute_breadth_from_prices_df(df)
    filtered_data = _apply_period_filter(data_all, period, start_date, end_date)
    return {"count": len(filtered_data), "data": filtered_data}


def _load_percent_above_ma(
    db: Session,
    period: str,
    approval_with_controls: Optional[str],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> dict:
    if approval_with_controls:
        statuses = [s.strip() for s in approval_with_controls.split(',') if s.strip()]
        if statuses:
            return _compute_filtered_breadth(db, statuses, period, start_date, end_date)

    from app.models import market_breadth as mb

    results = db.query(mb.MarketBreadth).order_by(mb.MarketBreadth.date.asc()).all()
    pct20 = [float(row.pct_above_20 or 0) for row in results]
    pct50 = [float(row.pct_above_50 or 0) for row in results]
    pct150 = [
        float(row.pct_above_150) if getattr(row, "pct_above_150", None) is not None else 0.0
        for row in results
    ]
    pct200 = [float(row.pct_above_200 or 0) for row in results]

    data_all = []
    for i, row in enumerate(results):
        data_all.append({
            "time": row.date.isoformat(),
            "date_obj": row.date,
            "pct_above_20": pct20[i],
            "pct_above_50": pct50[i],
            "pct_above_150": pct150[i],
            "pct_above_200": pct200[i],
            "ma50_20": _window_mean(pct20, 50, i),
            "ma200_20": _window_mean(pct20, 200, i),
            "ma50_50": _window_mean(pct50, 50, i),
            "ma200_50": _window_mean(pct50, 200, i),
            "ma50_150": _window_mean(pct150, 50, i),
            "ma200_150": _window_mean(pct150, 200, i),
            "ma50_200": _window_mean(pct200, 50, i),
            "ma200_200": _window_mean(pct200, 200, i),
        })

    filtered_data = _apply_period_filter(data_all, period, start_date, end_date)
    return {"count": len(filtered_data), "data": filtered_data}


def _build_ad_rating_series(db: Session, limit: int, min_date: Optional[date] = None) -> list:
    """Aggregate A/D counts per date; scope to recent dates when limit/min_date set."""
    base = db.query(RSDaily.date).filter(RSDaily.acc_dis_rating.isnot(None))
    if min_date:
        base = base.filter(RSDaily.date >= min_date)

    if limit:
        date_rows = (
            base.distinct()
            .order_by(RSDaily.date.desc())
            .limit(limit)
            .all()
        )
        if not date_rows:
            return []
        dates = sorted(r[0] for r in date_rows)
        min_d, max_d = dates[0], dates[-1]
        q = db.query(RSDaily).filter(
            RSDaily.date >= min_d,
            RSDaily.date <= max_d,
            RSDaily.acc_dis_rating.isnot(None),
        )
    else:
        q = db.query(RSDaily).filter(RSDaily.acc_dis_rating.isnot(None))
        if min_date:
            q = q.filter(RSDaily.date >= min_date)

    rows = (
        q.with_entities(
            RSDaily.date,
            func.sum(case((RSDaily.acc_dis_rating.like("A%"), 1), else_=0)).label("a_rating"),
            func.sum(case((RSDaily.acc_dis_rating.like("D%"), 1), else_=0)).label("d_rating"),
            func.sum(case((RSDaily.acc_dis_rating.isnot(None), 1), else_=0)).label("total_stocks"),
        )
        .group_by(RSDaily.date)
        .order_by(RSDaily.date)
        .all()
    )

    series = []
    for row in rows:
        total = max(int(row.total_stocks or 0), 1)
        a = int(row.a_rating or 0)
        d = int(row.d_rating or 0)
        d_str = str(row.date)
        series.append({
            "time": d_str,
            "date_obj": row.date,
            "a_rating": a,
            "d_rating": d,
            "total_stocks": total,
            "a_rating_pct": round(a / total * 100, 2),
            "d_rating_pct": round(d / total * 100, 2),
        })

    if limit and len(series) > limit:
        series = series[-limit:]
    return series


async def _load_ad_rating(
    db: Session, 
    period: str, 
    limit: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> dict:
    min_date = _period_min_date(period)
    
    # We always cache ALL data for this limit to avoid multiple database scans
    cache_key = f"market_breadth:ad_rating:v2:limit:{limit}:period:ALL"

    async def fetch():
        return _build_ad_rating_series(db, limit, min_date=None)

    data_all = await cache_read_through(cache_key, AD_RATING_CACHE_TTL, fetch)
    
    filtered_data = _apply_period_filter(data_all, period, start_date, end_date)
    return {"count": len(filtered_data), "data": filtered_data}


def _load_screener_daily_bundle(
    db: Session, 
    period: str, 
    limit: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> Tuple[dict, dict]:
    """One DB read for Minervini trends + Alhussain (same table)."""
    from app.models.screener_daily_trend import ScreenerDailyTrend

    try:
        rows = (
            db.query(ScreenerDailyTrend)
            .order_by(ScreenerDailyTrend.date.desc())
            .limit(limit)
            .all()
        )
    except Exception as e:
        empty = {
            "count": 0,
            "data": [],
            "message": "Run scripts/add_screener_trend_columns.py",
        }
        print(f"Screener daily trend unavailable: {e}")
        return empty, {**empty, "message": str(e)}

    if not rows:
        empty = {
            "count": 0,
            "data": [],
            "message": "No screener trend data. Run scripts/backfill_screener_daily_trend.py",
        }
        return empty, {
            "count": 0,
            "data": [],
            "message": "No alhussain data yet. Run scripts/backfill_alhussain_daily.py",
        }

    rows = list(reversed(rows))
    trend_all = []
    alhussain_all = []
    for r in rows:
        d = r.date
        trend_all.append({
            "time": d.isoformat(),
            "date_obj": d,
            "trend_1m": int(r.trend_1m or 0),
            "trend_4m": int(r.trend_4m or 0),
            "trend_5m_wide": int(r.trend_5m_wide or 0),
            "alrayan": int(r.alrayan or 0),
        })
        alhussain_all.append({
            "time": d.isoformat(),
            "count": int(r.alhussain or 0),
            "date_obj": d,
        })

    trend_filtered = _apply_period_filter(trend_all, period, start_date, end_date)
    alh_filtered = _apply_period_filter(alhussain_all, period, start_date, end_date)
    return (
        {"count": len(trend_filtered), "data": trend_filtered},
        {"count": len(alh_filtered), "data": alh_filtered},
    )


def _dashboard_cache_key(
    period: str,
    ad_limit: int,
    screener_limit: int,
    approval_with_controls: Optional[str],
) -> str:
    shariah = (approval_with_controls or "").strip().lower()
    shariah_safe = urllib.parse.quote(shariah)
    return (
        f"market_breadth:dashboard:v2:"
        f"period:{period.upper()}:ad:{ad_limit}:scr:{screener_limit}:sh:{shariah_safe}"
    )


def _percent_above_ma_cache_key(
    period: str,
    approval_with_controls: Optional[str],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> str:
    shariah = (approval_with_controls or "").strip().lower()
    shariah_safe = urllib.parse.quote(shariah)
    start_safe = start_date.isoformat() if start_date else ""
    end_safe = end_date.isoformat() if end_date else ""
    return (
        f"market_breadth:percent_above_ma:v2:period:{period.upper()}:"
        f"sh:{shariah_safe}:start:{start_safe}:end:{end_safe}"
    )


def _screener_bundle_cache_key(period: str, limit: int, kind: str) -> str:
    return f"market_breadth:{kind}:v2:limit:{limit}:period:{period.upper()}"


async def _build_dashboard_payload(
    db: Session,
    period: str,
    ad_limit: int,
    screener_limit: int,
    approval_with_controls: Optional[str],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> dict:
    ma = _load_percent_above_ma(db, period, approval_with_controls, start_date, end_date)
    ad = await _load_ad_rating(db, period, ad_limit, start_date, end_date)
    screener_trend, alhussain = _load_screener_daily_bundle(db, period, screener_limit, start_date, end_date)
    return {
        "period": period.upper(),
        "ma_breadth": ma,
        "ad_rating": ad,
        "alhussain": alhussain,
        "screener_trend": screener_trend,
    }


@router.get("/dashboard")
async def get_market_breadth_dashboard(
    db: Session = Depends(get_db),
    period: str = Query("ALL", description="5D, 1M, 6M, 1Y, 5Y, 10Y, ALL"),
    ad_limit: int = Query(DEFAULT_CHART_LIMIT, ge=1, le=6000),
    alhussain_limit: int = Query(DEFAULT_CHART_LIMIT, ge=1, le=6000),
    screener_trend_limit: int = Query(DEFAULT_CHART_LIMIT, ge=1, le=6000),
    approval_with_controls: Optional[str] = Query(
        None,
        description="Comma-separated Shariah statuses (Arabic labels)",
    ),
    start_date: Optional[date] = Query(None, description="YYYY-MM-DD"),
    end_date: Optional[date] = Query(None, description="YYYY-MM-DD"),
):
    """
    Single request for all market-breadth charts (MA + A/D + Alhussain + Minervini trends).
    Avoids parallel proxy connections that can cause ECONNRESET on slow queries.
    """
    try:
        limit = min(ad_limit, alhussain_limit, screener_trend_limit)
        
        # We cache the data WITHOUT period or start_date/end_date filtering, then filter after
        cache_key = _dashboard_cache_key("ALL", limit, limit, approval_with_controls)

        async def fetch():
            # fetch without date filtering
            return await _build_dashboard_payload(
                db, "ALL", limit, limit, approval_with_controls
            )

        data = await cache_read_through(cache_key, DASHBOARD_CACHE_TTL, fetch)
        
        # If dates or period are provided, we need to filter the cached response
        if period != "ALL" or start_date or end_date:
            def filter_dataset(dataset):
                if not dataset or not isinstance(dataset, dict) or 'data' not in dataset:
                    return dataset
                
                filtered = _apply_period_filter(dataset['data'], period, start_date, end_date)
                return {"count": len(filtered), "data": filtered}

            return {
                "ma_breadth": filter_dataset(data.get('ma_breadth')),
                "ad_rating": filter_dataset(data.get('ad_rating')),
                "alhussain": filter_dataset(data.get('alhussain')),
                "screener_trend": filter_dataset(data.get('screener_trend'))
            }
            
        return data
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error loading market breadth dashboard: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/percent-above-ma")
async def get_percent_above_ma(
    db: Session = Depends(get_db),
    period: str = Query("ALL", description="5D, 1M, 6M, 1Y, 5Y, 10Y, ALL"),
    start_date: Optional[date] = Query(None, description="YYYY-MM-DD"),
    end_date: Optional[date] = Query(None, description="YYYY-MM-DD"),
    approval_with_controls: Optional[str] = Query(
        None,
        description="Comma-separated Shariah statuses (Arabic labels)",
    ),
):
    """
    Returns historical % of stocks above 20/50/150/200 day Moving Average.
    Uses market_breadth table for fast aggregated data.
    """
    try:
        # Always cache ALL internally to avoid multiple heavy scans
        cache_key = _percent_above_ma_cache_key("ALL", approval_with_controls, None, None)
        async def fetch():
            return _load_percent_above_ma(db, "ALL", approval_with_controls, None, None)
        data = await cache_read_through(cache_key, DASHBOARD_CACHE_TTL, fetch)
        
        if period != "ALL" or start_date or end_date:
            filtered = _apply_period_filter(data['data'], period, start_date, end_date)
            return {"count": len(filtered), "data": filtered}
        return data
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error computing market breadth: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/alhussain-count")
async def get_alhussain_count(
    db: Session = Depends(get_db),
    period: str = Query("ALL", description="5D, 1M, 6M, 1Y, 5Y, 10Y, ALL"),
    limit: int = Query(6000, ge=1, le=6000),
    start_date: Optional[date] = Query(None, description="YYYY-MM-DD"),
    end_date: Optional[date] = Query(None, description="YYYY-MM-DD"),
):
    """
    Historical daily count of stocks passing the Alhussain screener criteria.
    Reads pre-aggregated column from screener_daily_trend_counts.
    Run scripts/backfill_alhussain_daily.py once to populate history.
    """
    try:
        # Cache key always uses ALL to avoid multiple DB queries
        cache_key = _screener_bundle_cache_key("ALL", limit, "alhussain_count")
        
        async def fetch():
            _, alhussain = _load_screener_daily_bundle(db, "ALL", limit)
            return alhussain
            
        data = await cache_read_through(cache_key, DASHBOARD_CACHE_TTL, fetch)
        
        filtered_items = _apply_period_filter(data['data'], period, start_date, end_date)
        return {"count": len(filtered_items), "data": filtered_items}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error loading alhussain count: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ad-rating")
async def get_ad_rating_history(
    db: Session = Depends(get_db),
    period: str = Query("ALL", description="5D, 1M, 6M, 1Y, 5Y, 10Y, ALL"),
    limit: int = Query(5000, ge=1, le=5000),
    start_date: Optional[date] = Query(None, description="YYYY-MM-DD"),
    end_date: Optional[date] = Query(None, description="YYYY-MM-DD"),
):
    """
    Historical A/D Rating counts and percentages (A vs D grades).
    Reads from rs_daily.acc_dis_rating — cached, single grouped query.
    """
    try:
        return await _load_ad_rating(db, period, limit, start_date, end_date)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error loading A/D rating history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/screener-trend")
async def get_screener_trend(
    db: Session = Depends(get_db),
    period: str = Query("ALL", description="5D, 1M, 6M, 1Y, 5Y, 10Y, ALL"),
    limit: int = Query(6000, ge=1, le=6000),
    start_date: Optional[date] = Query(None, description="YYYY-MM-DD"),
    end_date: Optional[date] = Query(None, description="YYYY-MM-DD"),
):
    """
    Historical screener trend data (Minervini 1M/4M/5MW + Alrayan).
    Lightweight endpoint that reads only from screener_daily_trend_counts.
    """
    try:
        # Cache key always uses ALL to avoid multiple DB queries
        cache_key = _screener_bundle_cache_key("ALL", limit, "screener_trend")
        
        async def fetch():
            screener_trend, _ = _load_screener_daily_bundle(db, "ALL", limit)
            return screener_trend
            
        data = await cache_read_through(cache_key, DASHBOARD_CACHE_TTL, fetch)
        
        filtered_items = _apply_period_filter(data['data'], period, start_date, end_date)
        return {"count": len(filtered_items), "data": filtered_items}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error loading screener trend: {e}")
        raise HTTPException(status_code=500, detail=str(e))