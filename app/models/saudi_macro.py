# backend/app/models/saudi_macro.py
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, UniqueConstraint
from sqlalchemy.sql import func
from app.core.database import Base


class SaudiEconomicIndicator(Base):
    """
    Saudi-Specific Macro Indicators & Time-Series Observations (Phase 5).
    Stores SAMA SAIBOR, GaStat GDP, Unemployment, Inflation, and Buffett Indicator with historical preservation.
    """
    __tablename__ = "saudi_economic_indicators"
    
    id = Column(Integer, primary_key=True, index=True)
    indicator_key = Column(String(50), nullable=False, index=True)  # e.g. saibor_3m, repo_rate, gdp_annual
    indicator_name = Column(String(255), nullable=True)
    value = Column(Float, nullable=True)
    raw_value = Column(String(100), nullable=True)
    unit = Column(String(50), nullable=True)  # %, SAR Million, Ratio
    period = Column(String(50), nullable=False)  # Observation period e.g. 2026-03 or 2025-Q4
    frequency = Column(String(50), default="Monthly")  # Daily, Monthly, Quarterly, Annual
    source = Column(String(100), nullable=False)  # SAMA, GaStat, KAPSARC
    source_url = Column(String(500), nullable=True)
    is_fallback = Column(Boolean, default=False, nullable=False)
    retrieved_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Composite uniqueness rule: indicator_key + period + source (preserves historical series)
    __table_args__ = (
        UniqueConstraint('indicator_key', 'period', 'source', name='uq_indicator_period_source'),
    )
