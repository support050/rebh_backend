# backend/app/models/market_reports.py
from sqlalchemy import Column, Integer, String, Date, DateTime, JSON, UniqueConstraint
from sqlalchemy.sql import func
from app.core.database import Base

class SubstantialShareholder(Base):
    __tablename__ = "substantial_shareholders"
    id = Column(Integer, primary_key=True, index=True)
    report_date = Column(Date, nullable=False, index=True)
    company_name = Column(String(255), nullable=True)
    shareholder_name = Column(String(255), nullable=True)
    holding_percent_last_day = Column(String(50), nullable=True)
    holding_percent_previous_day = Column(String(50), nullable=True)
    change = Column(String(50), nullable=True)
    managed_by_authorized_trading_day = Column(String(50), nullable=True)
    managed_by_authorized_previous_day = Column(String(50), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Add unique constraint to avoid duplicates per day
    __table_args__ = (UniqueConstraint('report_date', 'company_name', 'shareholder_name', name='uq_shareholder_date'),)

class NetShortPosition(Base):
    __tablename__ = "net_short_positions"
    id = Column(Integer, primary_key=True, index=True)
    report_date = Column(Date, nullable=False, index=True)
    symbol = Column(String(20), nullable=True)
    company = Column(String(255), nullable=True)
    percent_over_outstanding = Column(String(50), nullable=True)
    percent_over_free_float = Column(String(50), nullable=True)
    ratio_over_avg_daily = Column(String(50), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint('report_date', 'symbol', name='uq_short_position_date'),)

class ForeignHeadroom(Base):
    __tablename__ = "foreign_headrooms"
    id = Column(Integer, primary_key=True, index=True)
    report_date = Column(Date, nullable=False, index=True)
    symbol = Column(String(20), nullable=True)
    company = Column(String(255), nullable=True)
    foreign_limit = Column(String(50), nullable=True)
    actual_foreign_ownership = Column(String(50), nullable=True)
    ownership_room = Column(String(50), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint('report_date', 'symbol', name='uq_foreign_headroom_date'),)

class ShareBuyback(Base):
    __tablename__ = "share_buybacks"
    id = Column(Integer, primary_key=True, index=True)
    report_date = Column(Date, nullable=False, index=True)
    symbol = Column(String(20), nullable=True)
    company = Column(String(255), nullable=True)
    # Storing raw dynamic columns as json, since quarters update!
    data = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint('report_date', 'symbol', name='uq_buyback_date'),)

class SBLPosition(Base):
    __tablename__ = "sbl_positions"
    id = Column(Integer, primary_key=True, index=True)
    report_date = Column(Date, nullable=False, index=True)
    symbol = Column(String(20), nullable=True)
    company = Column(String(255), nullable=True)
    total_issued_shares = Column(String(50), nullable=True)
    lent_asset_quantity = Column(String(50), nullable=True)
    percent_of_lent_asset = Column(String(50), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint('report_date', 'symbol', name='uq_sbl_position_date'),)

class HistoricalReport(Base):
    __tablename__ = "historical_reports"
    id = Column(Integer, primary_key=True, index=True)
    report_date = Column(Date, nullable=False, index=True, unique=True)
    open_price = Column(String(50), nullable=True)
    high_price = Column(String(50), nullable=True)
    low_price = Column(String(50), nullable=True)
    close_price = Column(String(50), nullable=True)
    volume_traded = Column(String(50), nullable=True)
    value_traded = Column(String(50), nullable=True)
    no_of_trades = Column(String(50), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


