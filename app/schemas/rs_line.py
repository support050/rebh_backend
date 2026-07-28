# app/schemas/rs_line.py

from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from datetime import date


class RSLineRequest(BaseModel):
    """
    طلب حساب RS Line + MA Crossover لأي سهم سعودي
    """
    symbol:     str
    benchmark:  str = "^TASI.SR"
    start_date: str = "2022-01-01"
    end_date:   Optional[str] = None
    ma1_type:   Literal["EMA", "SMA"] = "EMA"
    ma1_period: int = Field(8,  ge=2, le=200)
    ma2_type:   Literal["EMA", "SMA"] = "SMA"
    ma2_period: int = Field(50, ge=2, le=200)
    lookback:   int = Field(50, ge=5,  le=250)
    scale_factor: int = Field(3000, ge=1, le=10000)


class RSPoint(BaseModel):
    date:        str
    stock_close: float
    bench_close: float
    rs_line:     float
    ma1:         Optional[float] = None
    ma2:         Optional[float] = None
    cross_bull:  bool = False
    cross_bear:  bool = False
    rs_new_high: bool = False
    rsnhbp:      bool = False
    rs_up:       bool = False
    above_ma2:   bool = False


class RSLineSummary(BaseModel):
    last_date:       str
    rs_line:         float
    ma1:             float
    ma2:             float
    direction:       Literal["up", "down"]
    position:        Literal["above_ma", "below_ma"]
    signal_today:    Optional[Literal["bullish_cross", "bearish_cross"]] = None
    rsnhbp_today:    bool = False
    last_bull_cross: Optional[str] = None
    last_bear_cross: Optional[str] = None


class RSLineResponse(BaseModel):
    symbol:     str
    benchmark:  str
    summary:    RSLineSummary
    data:       List[RSPoint]
    total_bars: int
