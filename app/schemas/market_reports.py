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
    holding_percent_last_day: Optional[str] = None
    holding_percent_previous_day: Optional[str] = None
    change: Optional[str] = None
    managed_by_authorized_trading_day: Optional[str] = None
    managed_by_authorized_previous_day: Optional[str] = None
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
    percent_over_outstanding: Optional[str] = None
    percent_over_free_float: Optional[str] = None
    ratio_over_avg_daily: Optional[str] = None
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
    foreign_limit: Optional[str] = None
    actual_foreign_ownership: Optional[str] = None
    ownership_room: Optional[str] = None
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
    shares_approved: Optional[str] = None
    shares_purchased: Optional[str] = None
    percent_of_capital: Optional[str] = None
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
    total_issued_shares: Optional[str] = None
    lent_asset_quantity: Optional[str] = None
    percent_of_lent_asset: Optional[str] = None
    source_url: Optional[str] = None
    retrieval_timestamp: Optional[datetime] = None
    
    class Config:
        from_attributes = True

class HistoricalReportResponse(BaseModel):
    id: int
    report_date: date
    open_price: Optional[str] = None
    high_price: Optional[str] = None
    low_price: Optional[str] = None
    close_price: Optional[str] = None
    volume_traded: Optional[str] = None
    value_traded: Optional[str] = None
    no_of_trades: Optional[str] = None
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
