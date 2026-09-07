from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from datetime import date, datetime
from decimal import Decimal

class SubstantialShareholderResponse(BaseModel):
    id: int
    report_date: date
    symbol: Optional[str] = None
    normalized_symbol: Optional[str] = None
    company_name: Optional[str] = None
    shareholder_name: Optional[str] = None
    holding_percent_last_day: Optional[Decimal] = None
    holding_percent_previous_day: Optional[Decimal] = None
    change: Optional[Decimal] = None
    managed_by_authorized_trading_day: Optional[Decimal] = None
    managed_by_authorized_previous_day: Optional[Decimal] = None
    source_url: Optional[str] = None
    retrieval_timestamp: Optional[datetime] = None
    
    class Config:
        from_attributes = True

class NetShortPositionResponse(BaseModel):
    id: int
    report_date: date
    symbol: Optional[str] = None
    normalized_symbol: Optional[str] = None
    company: Optional[str] = None
    percent_over_outstanding: Optional[Decimal] = None
    percent_over_free_float: Optional[Decimal] = None
    ratio_over_avg_daily: Optional[Decimal] = None
    source_url: Optional[str] = None
    retrieval_timestamp: Optional[datetime] = None
    
    class Config:
        from_attributes = True

class ForeignHeadroomResponse(BaseModel):
    id: int
    report_date: date
    symbol: Optional[str] = None
    normalized_symbol: Optional[str] = None
    company: Optional[str] = None
    foreign_limit: Optional[Decimal] = None
    actual_foreign_ownership: Optional[Decimal] = None
    ownership_room: Optional[Decimal] = None
    source_url: Optional[str] = None
    retrieval_timestamp: Optional[datetime] = None
    
    class Config:
        from_attributes = True

class ShareBuybackResponse(BaseModel):
    id: int
    report_date: date
    symbol: Optional[str] = None
    normalized_symbol: Optional[str] = None
    company: Optional[str] = None
    shares_approved: Optional[Decimal] = None
    shares_purchased: Optional[Decimal] = None
    percent_of_capital: Optional[Decimal] = None
    data: Optional[Dict[str, Any]] = None
    source_url: Optional[str] = None
    retrieval_timestamp: Optional[datetime] = None
    
    class Config:
        from_attributes = True

class SBLPositionResponse(BaseModel):
    id: int
    report_date: date
    symbol: Optional[str] = None
    normalized_symbol: Optional[str] = None
    company: Optional[str] = None
    total_issued_shares: Optional[Decimal] = None
    lent_asset_quantity: Optional[Decimal] = None
    percent_of_lent_asset: Optional[Decimal] = None
    source_url: Optional[str] = None
    retrieval_timestamp: Optional[datetime] = None
    
    class Config:
        from_attributes = True

class HistoricalReportResponse(BaseModel):
    id: int
    report_date: date
    open_price: Optional[Decimal] = None
    high_price: Optional[Decimal] = None
    low_price: Optional[Decimal] = None
    close_price: Optional[Decimal] = None
    volume_traded: Optional[Decimal] = None
    value_traded: Optional[Decimal] = None
    no_of_trades: Optional[int] = None
    source_url: Optional[str] = None
    retrieval_timestamp: Optional[datetime] = None
    
    class Config:
        from_attributes = True

class QFIOwnershipFlowResponse(BaseModel):
    id: int
    report_date: date
    symbol: str
    normalized_symbol: str
    company_name: Optional[str] = None
    qfi_holding_percent: Optional[Decimal] = None
    total_foreign_percent: Optional[Decimal] = None
    net_inflow_shares: Optional[Decimal] = None
    net_inflow_sar: Optional[Decimal] = None
    source_url: Optional[str] = None
    retrieval_timestamp: Optional[datetime] = None

    class Config:
        from_attributes = True
