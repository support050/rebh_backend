from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc, asc
from typing import List, Optional
from datetime import date, timedelta
import logging

import pandas as pd

from app.core.database import get_db
from app.models.price import Price
from app.models.market_pulse import MarketPulse
from app.schemas.price import PriceResponse, LatestPricesResponse
from app.core.cache_helpers import (
    cache_read_through,
    make_prices_history_key,
    make_prices_latest_key,
    make_prices_seasonality_key,
    make_prices_tasi_history_key
)
from app.core.cache_config import CACHE_TTL_PRICES_HISTORY, CACHE_TTL_PRICES_LATEST

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/prices", tags=["Prices"])

import csv
from pathlib import Path


@router.get("/history/{symbol}")
async def get_price_history(
    symbol: str,
    db: Session = Depends(get_db),
    limit: int = Query(10000, le=50000),
):
    """
    Get historical OHLCV + all oscillator data for a symbol.
    prices table: pure OHLCV
    stock_indicators: everything else (RSI, MCAs, SMA, 52w, etc.)
    Cached with 24-hour TTL (CACHE_TTL_PRICES_HISTORY).
    """
    cache_key = make_prices_history_key(symbol, limit)
    
    async def fetch_history():
        try:
            from app.models.stock_indicators import StockIndicator

            results = (
                db.query(Price, StockIndicator)
                .outerjoin(
                    StockIndicator,
                    (Price.symbol == StockIndicator.symbol) & (Price.date == StockIndicator.date)
                )
                .filter(Price.symbol == symbol)
                .order_by(asc(Price.date))  # ترتيب تصاعدي من الأقدم للأحدث
                .limit(limit)
                .all()
            )

            if not results:
                raise HTTPException(status_code=404, detail=f"No price history found for symbol {symbol}")

            def f(val):
                return float(val) if val is not None else None

            data = []
            for price, ind in results:
                data.append({
                    "time":   price.date.isoformat(),
                    "open":   f(price.open),
                    "high":   f(price.high),
                    "low":    f(price.low),
                    "close":  f(price.close),
                    "volume": int(price.volume_traded) if price.volume_traded is not None else 0,
                    # ── Standard SMAs (from stock_indicators) ──────────────────
                    "sma_10":  f(ind.sma_10)  if ind else None,
                    "sma_21":  f(ind.sma_21)  if ind else None,
                    "sma_50":  f(ind.sma_50)  if ind else None,
                    "sma_150": f(ind.sma_150) if ind else None,
                    "sma_200": f(ind.sma_200) if ind else None,
                    # ── PineScript-exact EMAs (from stock_indicators) ───────────
                    "ema_10":  f(ind.ema10) if ind else None,
                    "ema_21":  f(ind.ema21) if ind else None,
                    # ── Historical 200MA (from stock_indicators) ───────────────
                    "sma_200_1m_ago": f(ind.sma_200_1m_ago) if ind else None,
                    "sma_200_2m_ago": f(ind.sma_200_2m_ago) if ind else None,
                    "sma_200_3m_ago": f(ind.sma_200_3m_ago) if ind else None,
                    "sma_200_4m_ago": f(ind.sma_200_4m_ago) if ind else None,
                    "sma_200_5m_ago": f(ind.sma_200_5m_ago) if ind else None,
                    # ── Weekly SMAs (from stock_indicators) ────────────────────
                    "sma_30w": f(ind.sma_30w) if ind else None,
                    "sma_40w": f(ind.sma_40w) if ind else None,
                    # ── Additional SMAs (from stock_indicators) ────────────────
                    "sma_3": f(ind.sma3_rsi3) if ind else None,
                    "ema_20_sma3": f(ind.ema20_sma3) if ind else None,
                    # ── Price vs SMA % (from stock_indicators) ──────────────────
                    "price_vs_sma_10_percent": f(ind.price_vs_sma_10_percent) if ind else None,
                    "price_vs_sma_21_percent": f(ind.price_vs_sma_21_percent) if ind else None,
                    "price_vs_sma_50_percent": f(ind.price_vs_sma_50_percent) if ind else None,
                    "price_vs_sma_150_percent": f(ind.price_vs_sma_150_percent) if ind else None,
                    "price_vs_sma_200_percent": f(ind.price_vs_sma_200_percent) if ind else None,
                    # ── 52-Week & Volume Stats ──────────────────────────────────
                    "fifty_two_week_high": f(ind.fifty_two_week_high) if ind else None,
                    "fifty_two_week_low":  f(ind.fifty_two_week_low)  if ind else None,
                    "average_volume_50":   f(ind.average_volume_50)   if ind else None,
                    "vol_diff_50_percent": f(ind.vol_diff_50_percent) if ind else None,
                    "percent_off_52w_high": f(ind.percent_off_52w_high) if ind else None,
                    "percent_off_52w_low":  f(ind.percent_off_52w_low)  if ind else None,
                    # ── RSI Daily ──────────────────────────────────────────────
                    "rsi_14":    f(ind.rsi_14)    if ind else None,
                    "sma9_rsi":  f(ind.sma9_rsi)  if ind else None,
                    "wma45_rsi": f(ind.wma45_rsi) if ind else None,
                    # ── RSI Weekly ─────────────────────────────────────────────
                    "rsi_w":       f(ind.rsi_w)       if ind else None,
                    "sma9_rsi_w":  f(ind.sma9_rsi_w)  if ind else None,
                    "wma45_rsi_w": f(ind.wma45_rsi_w) if ind else None,
                    # ── CCI Daily ──────────────────────────────────────────────
                    "cci":       f(ind.cci)       if ind else None,
                    "cci_ema20": f(ind.cci_ema20) if ind else None,
                    # ── CCI Weekly ─────────────────────────────────────────────
                    "cci_w":       f(ind.cci_w)       if ind else None,
                    "cci_ema20_w": f(ind.cci_ema20_w) if ind else None,
                    # ── CFG Daily ──────────────────────────────────────────────
                    "cfg":       f(ind.cfg_daily) if ind else None,
                    "cfg_sma4":  f(ind.cfg_sma4)  if ind else None,
                    "cfg_ema45": f(ind.cfg_ema45) if ind else None,
                    # ── CFG Weekly ─────────────────────────────────────────────
                    "cfg_w":       f(ind.cfg_w)       if ind else None,
                    "cfg_sma4_w":  f(ind.cfg_sma4_w)  if ind else None,
                    "cfg_ema45_w": f(ind.cfg_ema45_w) if ind else None,
                    # ── THE.NUMBER Daily ───────────────────────────────────────
                    "the_number":    f(ind.the_number)    if ind else None,
                    "the_number_hl": f(ind.the_number_hl) if ind else None,
                    "the_number_ll": f(ind.the_number_ll) if ind else None,
                    # ── THE.NUMBER Weekly ──────────────────────────────────────
                    "the_number_w":    f(ind.the_number_w)    if ind else None,
                    "the_number_hl_w": f(ind.the_number_hl_w) if ind else None,
                    "the_number_ll_w": f(ind.the_number_ll_w) if ind else None,
                    # ── STAMP Daily ────────────────────────────────────────────
                    "stamp_s9rsi":   f(ind.stamp_s9rsi)   if ind else None,
                    "stamp_e45cfg":  f(ind.stamp_e45cfg)  if ind else None,
                    "stamp_e45rsi":  f(ind.stamp_e45rsi)  if ind else None,
                    "stamp_e20sma3": f(ind.stamp_e20sma3) if ind else None,
                    # ── STAMP Weekly ───────────────────────────────────────────
                    "stamp_s9rsi_w":  f(ind.stamp_s9rsi_w)  if ind else None,
                    "stamp_e45cfg_w": f(ind.stamp_e45cfg_w) if ind else None,
                    "stamp_e45rsi_w": f(ind.stamp_e45rsi_w) if ind else None,
                    "stamp_e20sma3_w": f(ind.stamp_e20sma3_w) if ind else None,
                    # ── Price MAs from indicators ──────────────────────────────
                    "sma4":  f(ind.sma4)  if ind else None,
                    "sma9":  f(ind.sma9_close) if ind else None,
                    "sma18": f(ind.sma18) if ind else None,
                    "wma45_close": f(ind.wma45_close) if ind else None,
                    # ── Weekly price MAs ─────────────────────────────────────
                    "sma4_w":  f(ind.sma4_w)  if ind else None,
                    "sma9_w":  f(ind.sma9_w)  if ind else None,
                    "sma18_w": f(ind.sma18_w) if ind else None,
                    "wma45_close_w": f(ind.wma45_close_w) if ind else None,
                    # ── Aroon ─────────────────────────────────────────────────
                    "aroon_up":   f(ind.aroon_up)   if ind else None,
                    "aroon_down": f(ind.aroon_down) if ind else None,
                    "aroon_up_w":   f(ind.aroon_up_w)   if ind else None,
                    "aroon_down_w": f(ind.aroon_down_w) if ind else None,
                })

            return {"symbol": symbol, "count": len(data), "data": data}

        except HTTPException:
            raise
        except Exception as e:
            print(f"Error fetching price history for {symbol}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return await cache_read_through(cache_key, CACHE_TTL_PRICES_HISTORY, fetch_history)


_MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@router.get("/seasonality/{symbol}")
async def get_price_seasonality(
    symbol: str,
    db: Session = Depends(get_db),
    years: int = Query(10, ge=2, le=20, description="Max calendar years in the year×month matrix"),
):
    """
    Calendar-month return seasonality computed from real closes in the prices
    table (indexed by idx_prices_symbol_date — single-symbol scan, ≤1 row/day).

    Method: take the last trading close of each calendar month, then
    month-over-month % return. Aggregated per calendar month across all
    history: mean, median, win rate (% positive months) and sample count.
    Also returns the newest `years` calendar years as a year×month matrix
    (null = month without a computed return, e.g. first month of data).
    Redis read-through cache, same TTL as /history.
    """
    cache_key = make_prices_seasonality_key(symbol, years)

    async def fetch_seasonality():
        try:
            rows = (
                db.query(Price.date, Price.close)
                .filter(Price.symbol == symbol)
                .order_by(asc(Price.date))
                .all()
            )
            if not rows:
                raise HTTPException(status_code=404, detail=f"No price history for symbol {symbol}")

            df = pd.DataFrame([(r[0], r[1]) for r in rows], columns=["date", "close"])
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df = df.dropna(subset=["close", "date"])
            df = df[df["close"] > 0]
            if df.empty:
                raise HTTPException(status_code=404, detail=f"No usable closes for symbol {symbol}")

            df = df.sort_values("date")
            df["year"] = df["date"].apply(lambda d: d.year)
            df["month"] = df["date"].apply(lambda d: d.month)
            # Last trading close of each calendar month
            monthly = df.groupby(["year", "month"], sort=True)["close"].last().reset_index()
            monthly["prev_close"] = monthly["close"].shift(1)
            monthly = monthly[monthly["prev_close"] > 0]
            monthly["ret_pct"] = (monthly["close"] / monthly["prev_close"] - 1.0) * 100.0

            # ── Per-calendar-month aggregates ─────────────────────────────
            months_out = []
            for m in range(1, 13):
                vals = monthly.loc[monthly["month"] == m, "ret_pct"]
                n = int(vals.count())
                if n == 0:
                    months_out.append({
                        "month": m, "name": _MONTH_NAMES[m - 1],
                        "mean": None, "median": None, "win_rate": None, "n": 0,
                    })
                    continue
                months_out.append({
                    "month": m, "name": _MONTH_NAMES[m - 1],
                    "mean": round(float(vals.mean()), 2),
                    "median": round(float(vals.median()), 2),
                    "win_rate": round(float((vals > 0).sum()) / n * 100.0, 1),
                    "n": n,
                })

            # ── Year × month matrix (newest `years` calendar years) ───────
            monthly["key"] = list(zip(monthly["year"], monthly["month"]))
            ret_map = {k: round(float(v), 2) for k, v in zip(monthly["key"], monthly["ret_pct"])}
            all_years = sorted({int(y) for y in monthly["year"]})
            recent_years = all_years[-years:]
            years_out = []
            for y in recent_years:
                cells = [ret_map.get((y, m)) for m in range(1, 13)]
                years_out.append({"year": y, "values": cells})

            return {
                "symbol": symbol,
                "first_date": df["date"].iloc[0].isoformat(),
                "last_date": df["date"].iloc[-1].isoformat(),
                "years_covered": len(all_years),
                "months": months_out,
                "years": years_out,
            }

        except HTTPException:
            raise
        except Exception as e:
            print(f"Error computing seasonality for {symbol}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return await cache_read_through(cache_key, CACHE_TTL_PRICES_HISTORY, fetch_seasonality)


@router.get("/tasi/history")
async def get_tasi_history(
    db: Session = Depends(get_db),
    days: int = Query(3650, ge=30, le=7300, description="Calendar days of TASI history (default ~10y)"),
):
    """
    True TASI index historical closes from the market_pulse table
    (columns date/close, date uniquely indexed) — the same benchmark source
    used by app/services/rs_line.py `_fetch_tasi_close()`.

    Returns the ascending daily series as { time: 'YYYY-MM-DD', close: number }
    so the frontend can compute real index returns (1M…5Y) and overlay the
    benchmark against individual symbols. Redis read-through, TTL = history.
    """
    cache_key = make_prices_tasi_history_key(days)

    async def fetch_tasi():
        try:
            min_date = date.today() - timedelta(days=days)
            rows = (
                db.query(MarketPulse.date, MarketPulse.close)
                .filter(MarketPulse.date >= min_date)
                .order_by(asc(MarketPulse.date))
                .all()
            )
            data = [
                {"time": r[0].isoformat(), "close": float(r[1]) if r[1] is not None else None}
                for r in rows
                if r[0] is not None
            ]
            if not data:
                raise HTTPException(status_code=404, detail="No TASI data in market_pulse table")
            return {
                "symbol": "TASI",
                "source": "market_pulse",
                "count": len(data),
                "first_date": data[0]["time"],
                "last_date": data[-1]["time"],
                "data": data,
            }
        except HTTPException:
            raise
        except Exception as e:
            print(f"Error fetching TASI history from market_pulse: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return await cache_read_through(cache_key, CACHE_TTL_PRICES_HISTORY, fetch_tasi)


@router.get("/latest", response_model=LatestPricesResponse)
async def get_latest_prices(
    db: Session = Depends(get_db),
    limit: int = Query(500, le=1000)
):
    """
    Get the latest prices for all stocks.
    Finds the most recent date in the prices table and returns all records for that date.
    Cached with 5-minute TTL.
    """
    cache_key = make_prices_latest_key(limit)
    
    async def fetch_latest():
        try:
            # 1. Get the atomic latest_ready_date from update_status
            from sqlalchemy import text as sa_text
            status_row = db.execute(sa_text("SELECT latest_ready_date, is_updating FROM update_status WHERE id = 1")).fetchone()
            
            if status_row and status_row[0]:
                # Always use latest_ready_date (points to last COMPLETE day)
                latest_date = status_row[0]
            elif status_row and status_row[1]:
                # is_updating=TRUE but no latest_ready_date → system just initialised, serve nothing
                return LatestPricesResponse(date=date.today(), count=0, data=[])
            else:
                # Fallback only if update_status row doesn't exist at all
                latest_date_row = db.query(Price.date).order_by(desc(Price.date)).first()
                if not latest_date_row:
                    return LatestPricesResponse(date=date.today(), count=0, data=[])
                latest_date = latest_date_row[0]
            
            # 2. Query data for that date
            results = db.query(Price).filter(Price.date == latest_date).limit(limit).all()

            # 3. Load TradingView Symbols Mapping
            tv_mapping = {}
            try:
                # Assuming company_symbols.csv is in backend/company_symbols.csv
                # Current file is backend/app/api/routes/prices.py
                csv_path = Path(__file__).resolve().parent.parent.parent.parent / "company_symbols.csv"
                
                if csv_path.exists():
                    with open(csv_path, 'r', encoding='utf-8-sig') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            # CSV Header: Symbol,Company,symbol on tradingView
                            sym = str(row.get('Symbol', '')).strip()
                            tv_sym = str(row.get('symbol on tradingView', '')).strip()
                            if sym and tv_sym:
                                tv_mapping[sym] = tv_sym
            except Exception as e:
                logger.warning(f"Error loading company_symbols.csv: {e}")

            # 4. Fetch Static Info and attach it
            from app.models.static_stock_info import StaticStockInfo
            static_rows = db.query(StaticStockInfo).all()
            static_map = {row.symbol: row for row in static_rows}
            
            for price in results:
                price.trading_view_symbol = tv_mapping.get(str(price.symbol))
                
                # Attach static info dynamically
                s_info = static_map.get(str(price.symbol))
                if s_info:
                    price.approval_with_controls = s_info.approval_with_controls
                    price.purge_amount = s_info.purge_amount
                    price.marginable_percent = s_info.marginable_percent
                else:
                    price.approval_with_controls = None
                    price.purge_amount = None
                    price.marginable_percent = None

            return LatestPricesResponse(
                date=latest_date,
                count=len(results),
                data=results
            )
        except Exception as e:
            logger.error(f"Error fetching latest prices: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    result = await cache_read_through(cache_key, CACHE_TTL_PRICES_LATEST, fetch_latest)
    return result
