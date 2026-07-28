# app/services/rs_line.py
"""
RS Line Service — يحسب المؤشرين من بيانات الـ DB مباشرة:
  1. TraderLion RS Line  (stock/benchmark ratio + new-high-before-price)
  2. RS MA Crossover LevelUp (EMA/SMA crossover on RS Line)

البيانات من:
  - جدول prices  → سعر إغلاق السهم
  - جدول market_pulse → سعر إغلاق TASI (البنشمارك)
"""

import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import asc
import logging

logger = logging.getLogger(__name__)


def _calc_ma(series: pd.Series, period: int, ma_type: str) -> pd.Series:
    """حساب Moving Average — EMA أو SMA"""
    if ma_type.upper() == "EMA":
        return series.ewm(span=period, adjust=False).mean()
    return series.rolling(window=period).mean()


def _normalize_symbol(symbol: str) -> str:
    """
    تحويل الرمز من صيغة Yahoo (.SR) لصيغة الـ DB
    مثال: '2222.SR' → '2222'
    """
    if symbol.endswith(".SR"):
        return symbol.replace(".SR", "")
    return symbol


def _fetch_stock_close(db: Session, symbol: str, start_date: str, end_date: str) -> pd.Series:
    """سحب بيانات Close للسهم من جدول prices"""
    from app.models.price import Price

    db_symbol = _normalize_symbol(symbol)
    logger.info(f"📊 جاري سحب بيانات {db_symbol} من DB")

    results = (
        db.query(Price.date, Price.close)
        .filter(Price.symbol == db_symbol)
        .filter(Price.date >= start_date)
        .filter(Price.date <= end_date)
        .order_by(asc(Price.date))
        .all()
    )

    if not results:
        raise ValueError(f"No price data for symbol {db_symbol} in database")

    dates = [r[0] for r in results]
    closes = [float(r[1]) for r in results]
    series = pd.Series(closes, index=pd.DatetimeIndex(dates), name=db_symbol)

    logger.info(f"✅ {db_symbol}: {len(series)} bar(s) from DB")
    return series


def _fetch_tasi_close(db: Session, start_date: str, end_date: str) -> pd.Series:
    """سحب بيانات Close لمؤشر TASI من جدول market_pulse"""
    from app.models.market_pulse import MarketPulse

    logger.info(f"📊 جاري سحب بيانات TASI من DB (market_pulse)")

    results = (
        db.query(MarketPulse.date, MarketPulse.close)
        .filter(MarketPulse.date >= start_date)
        .filter(MarketPulse.date <= end_date)
        .order_by(asc(MarketPulse.date))
        .all()
    )

    if not results:
        raise ValueError("No TASI data in market_pulse table")

    dates = [r[0] for r in results]
    closes = [float(r[1]) for r in results]
    series = pd.Series(closes, index=pd.DatetimeIndex(dates), name="TASI")

    logger.info(f"✅ TASI: {len(series)} bar(s) from DB")
    return series


def calculate_rs_line(
    db:           Session,
    symbol:       str,
    benchmark:    str = "^TASI.SR",
    start_date:   str = "2020-01-01",
    end_date:     Optional[str] = None,
    ma1_type:     str = "EMA",
    ma1_period:   int = 8,
    ma2_type:     str = "SMA",
    ma2_period:   int = 50,
    lookback:     int = 50,
    scale_factor: int = 3000,
) -> pd.DataFrame:
    """
    يحسب RS Line كاملة من بيانات الـ DB:
      RS Line = Stock Close / TASI Close × scale_factor
      MA1 (fast) on RS Line
      MA2 (slow) on RS Line
      Bull/Bear crossovers
      RS New High Before Price (RSNHBP)
    
    scale_factor:
      - 3000  → TraderLion RS Line — القيم ~7.61 (تطابق TradingView)
    """
    end = end_date or datetime.today().strftime("%Y-%m-%d")

    # سحب البيانات من الـ DB
    stock_close = _fetch_stock_close(db, symbol, start_date, end)
    bench_close = _fetch_tasi_close(db, start_date, end)

    # دمج البيانات مع الحفاظ على خط زمني السهم، وملء فراغات المؤشر (زي TradingView)
    df = pd.DataFrame({"stock": stock_close, "bench": bench_close})
    
    # 1. Forward fill للبنشمارك عشان لو المؤشر مقفول والسهم شغال ياخد آخر إغلاق
    df["bench"] = df["bench"].ffill()
    
    # 2. حذف الأيام اللي ملهاش بيانات خالص
    df = df.dropna()

    if df.empty:
        raise ValueError(f"No overlapping data between {symbol} and {benchmark}")

    # ── RS Line ──────────────────────────────────────
    # scale_factor=100 → RS MA Crossover | scale_factor=3000 → TraderLion (يطابق TradingView)
    df["rs_line"] = (df["stock"] / df["bench"]) * scale_factor

    # ── MAs على RS Line ─────────────────────────────
    df["ma1"] = _calc_ma(df["rs_line"], ma1_period, ma1_type)
    df["ma2"] = _calc_ma(df["rs_line"], ma2_period, ma2_type)

    # ── Crossovers (MA1 crosses MA2) ─────────────────
    df["cross_bull"] = (df["ma1"] > df["ma2"]) & (df["ma1"].shift(1) <= df["ma2"].shift(1))
    df["cross_bear"] = (df["ma1"] < df["ma2"]) & (df["ma1"].shift(1) >= df["ma2"].shift(1))

    # ── RS New High Before Price (Pink Dot) ──────────
    df["rs_new_high"]    = df["rs_line"] >= df["rs_line"].rolling(lookback).max().shift(1)
    df["price_new_high"] = df["stock"]   >= df["stock"].rolling(lookback).max().shift(1)
    df["rsnhbp"]         = df["rs_new_high"] & ~df["price_new_high"]

    # ── Direction & Zone ─────────────────────────────
    df["rs_up"]     = df["rs_line"] > df["rs_line"].shift(1)
    df["above_ma2"] = df["rs_line"] > df["ma2"]

    return df


def df_to_response(df: pd.DataFrame, symbol: str, benchmark: str) -> dict:
    """تحويل DataFrame إلى dict مطابق لـ RSLineResponse schema"""
    last  = df.iloc[-1]
    bulls = df[df["cross_bull"]]
    bears = df[df["cross_bear"]]

    signal_today = None
    if last["cross_bull"]:
        signal_today = "bullish_cross"
    elif last["cross_bear"]:
        signal_today = "bearish_cross"

    summary = {
        "last_date":       str(df.index[-1].date()),
        "rs_line":         round(float(last["rs_line"]), 6),
        "ma1":             round(float(last["ma1"]),     6),
        "ma2":             round(float(last["ma2"]),     6),
        "direction":       "up" if last["rs_up"] else "down",
        "position":        "above_ma" if last["above_ma2"] else "below_ma",
        "signal_today":    signal_today,
        "rsnhbp_today":    bool(last["rsnhbp"]),
        "last_bull_cross": str(bulls.index[-1].date()) if not bulls.empty else None,
        "last_bear_cross": str(bears.index[-1].date()) if not bears.empty else None,
    }

    data = []
    for dt, row in df.iterrows():
        data.append({
            "date":        str(dt.date()),
            "stock_close": round(float(row["stock"]),   4),
            "bench_close": round(float(row["bench"]),   2),
            "rs_line":     round(float(row["rs_line"]), 8),
            "ma1":         round(float(row["ma1"]), 8) if pd.notna(row["ma1"]) else None,
            "ma2":         round(float(row["ma2"]), 8) if pd.notna(row["ma2"]) else None,
            "cross_bull":  bool(row["cross_bull"]),
            "cross_bear":  bool(row["cross_bear"]),
            "rs_new_high": bool(row["rs_new_high"]),
            "rsnhbp":      bool(row["rsnhbp"]),
            "rs_up":       bool(row["rs_up"]),
            "above_ma2":   bool(row["above_ma2"]),
        })

    return {
        "symbol":     symbol,
        "benchmark":  benchmark,
        "summary":    summary,
        "data":       data,
        "total_bars": len(data),
    }
