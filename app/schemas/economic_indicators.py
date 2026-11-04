from pydantic import BaseModel
from datetime import date, datetime
from typing import Optional

class EconomicIndicatorBase(BaseModel):
    report_date: date
    indicator_code: str
    value: Optional[float] = None
    yoy_pct: Optional[float] = None

class EconomicIndicatorCreate(EconomicIndicatorBase):
    pass

class EconomicIndicatorResponse(EconomicIndicatorBase):
    id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class FredSeriesMetadataResponse(BaseModel):
    """Persisted per-series FRED metadata (table fred_series_metadata)."""
    indicator_code: str
    series_id: Optional[str] = None
    title: Optional[str] = None
    units: Optional[str] = None
    frequency: Optional[str] = None
    seasonal_adjustment: Optional[str] = None
    source_agency: Optional[str] = None
    fred_url: Optional[str] = None
    latest_observation_date: Optional[date] = None
    latest_observation_value: Optional[float] = None
    last_updated_time: Optional[str] = None
    next_release_date: Optional[date] = None
    release_id: Optional[int] = None
    release_calendar_url: Optional[str] = None
    notes: Optional[str] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class TreasuryYieldCurveResponse(BaseModel):
    id: int
    report_date: date
    month_1: Optional[float] = None
    month_1_5: Optional[float] = None
    month_2: Optional[float] = None
    month_3: Optional[float] = None
    month_4: Optional[float] = None
    month_6: Optional[float] = None
    year_1: Optional[float] = None
    year_2: Optional[float] = None
    year_3: Optional[float] = None
    year_5: Optional[float] = None
    year_7: Optional[float] = None
    year_10: Optional[float] = None
    year_20: Optional[float] = None
    year_30: Optional[float] = None

    class Config:
        from_attributes = True

class SP500HistoryResponse(BaseModel):
    id: int
    trade_date: date
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: float
    volume: Optional[int] = None

    class Config:
        from_attributes = True


class EurodollarFuturesResponse(BaseModel):
    id: int
    scrape_date: date
    symbol: Optional[str] = None
    contract: str
    last_price: Optional[float]
    change: Optional[float]
    open_price: Optional[float]
    high: Optional[float]
    low: Optional[float]
    previous: Optional[float]
    volume: Optional[int]
    open_interest: Optional[int]
    updated_time: Optional[str]

    class Config:
        from_attributes = True


class CmeFedwatchResponse(BaseModel):
    id: int
    scrape_date: date
    meeting_date: str
    rate_range: str
    probability: float

    class Config:
        from_attributes = True

