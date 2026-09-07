# backend/app/models/sukuk_bonds.py
from sqlalchemy import Column, Integer, String, Numeric, Date, Boolean, DateTime, UniqueConstraint
from sqlalchemy.sql import func
from app.core.database import Base


class SukukMarketData(Base):
    """
    Sukuk and Debt Instruments Market Data Model (Phase 4).
    Stores corporate and government sukuk with numeric yields and dates.
    """
    __tablename__ = "sukuk_market_data"
    
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False, index=True)  # Sukuk Symbol (e.g. 5389)
    issuer_name = Column(String(255), nullable=True)
    parent_company_symbol = Column(String(20), nullable=True, index=True)  # Parent company (e.g. 1120)
    bond_type = Column(String(50), nullable=True)  # G = Govt, C = Corporate
    coupon_rate = Column(Numeric(10, 4), nullable=True)  # Stored as Numeric % (e.g. 5.2000)
    yield_to_maturity = Column(Numeric(10, 4), nullable=True)  # YTM %
    issue_date = Column(Date, nullable=True)
    maturity_date = Column(Date, nullable=True)
    outstanding_amount = Column(Numeric(20, 2), nullable=True)  # Amount in SAR
    currency = Column(String(10), default="SAR", nullable=False)
    sector_name = Column(String(255), nullable=True)
    source_url = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    as_of = Column(String(50), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Composite uniqueness constraint per issuance symbol and parent company
    __table_args__ = (
        UniqueConstraint('symbol', 'parent_company_symbol', name='uq_sukuk_symbol_parent'),
    )
