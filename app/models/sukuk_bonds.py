# backend/app/models/sukuk_bonds.py
from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func
from app.core.database import Base


class SukukMarketData(Base):
    __tablename__ = "sukuk_market_data"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False, unique=True, index=True)  # Sukuk Symbol (e.g. 5389)
    issuer_name = Column(String(255), nullable=True)
    parent_company_symbol = Column(String(20), nullable=True, index=True)  # Parent company (e.g. 1120)
    bond_type = Column(String(50), nullable=True)  # G = Govt, C = Corporate
    coupon_rate = Column(String(50), nullable=True)  # Yield / Coupon % (e.g. 5.20)
    maturity_date = Column(String(50), nullable=True)
    outstanding_amount = Column(String(100), nullable=True)
    sector_name = Column(String(255), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
