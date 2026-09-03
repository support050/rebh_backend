# backend/app/models/saudi_macro.py
from sqlalchemy import Column, Integer, String, Float, DateTime
from sqlalchemy.sql import func
from app.core.database import Base


class SaudiEconomicIndicator(Base):
    """Saudi-Specific Macro Indicators (SAMA SAIBOR, GaStat GDP, Saudi Buffett Indicator)"""
    __tablename__ = "saudi_economic_indicators"
    id = Column(Integer, primary_key=True, index=True)
    indicator_key = Column(String(50), nullable=False, unique=True, index=True)  # e.g. saibor_3m, saibor_12m, gdp_annual_m_sar, buffett_ratio
    indicator_name = Column(String(255), nullable=True)
    value = Column(Float, nullable=True)
    unit = Column(String(50), nullable=True)  # %, SAR Million, Ratio
    period = Column(String(50), nullable=True)  # e.g. 2024-Q4 or 2025-01
    source = Column(String(100), nullable=True)  # SAMA, GaStat, KAPSARC
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
