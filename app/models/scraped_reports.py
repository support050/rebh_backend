# backend/app/models/scraped_reports.py
"""
SQLAlchemy models for scraped financial data from Saudi Exchange.
These models store data from the Playwright scraper integration.
"""
from sqlalchemy import Column, Integer, String, Date, DateTime, Enum, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from app.core.database import Base
import enum


class PeriodType(str, enum.Enum):
    """Period type enum for financial reports."""
    Annually = "Annually"
    Quarterly = "Quarterly"


class ReportType(str, enum.Enum):
    """Report type enum for financial reports."""
    Balance_Sheet = "Balance_Sheet"
    Income_Statement = "Income_Statement"
    Cash_Flows = "Cash_Flows"


class Company(Base):
    """
    Companies table - stores basic company information from Saudi Exchange.
    Symbol is unique identifier matching the Saudi Exchange symbol.
    """
    __tablename__ = "companies"
    
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    name_en = Column(String(255), nullable=True)
    name_ar = Column(String(255), nullable=True)
    sector = Column(String(255), nullable=True)
    last_scraped_at = Column(DateTime(timezone=True), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    def __repr__(self):
        return f"<Company(symbol='{self.symbol}', name_en='{self.name_en}')>"


class FinancialReport(Base):
    """
    Financial Reports table - stores scraped financial data.
    Uses JSONB for metrics because different companies/sectors have different line items.
    Unique constraint on (company_symbol, period_type, report_type, period_end_date)
    for UPSERT logic.
    """
    __tablename__ = "financial_reports"
    
    id = Column(Integer, primary_key=True, index=True)
    company_symbol = Column(String(20), nullable=False, index=True)
    period_type = Column(
        Enum(PeriodType, name='period_type_enum', create_type=True),
        nullable=False
    )
    report_type = Column(
        Enum(ReportType, name='report_type_enum', create_type=True),
        nullable=False
    )
    period_end_date = Column(Date, nullable=False)
    
    # JSONB for flexible metrics storage
    # Example: {"Total Assets": "1,234,567", "Current Assets": "987,654", ...}
    metrics = Column(JSONB, nullable=True)
    
    # Raw table data as scraped (for reference/debugging)
    raw_data = Column(JSONB, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Unique constraint for UPSERT logic
    __table_args__ = (
        UniqueConstraint(
            'company_symbol', 'period_type', 'report_type', 'period_end_date',
            name='uq_financial_report'
        ),
    )
    
    def __repr__(self):
        return f"<FinancialReport(symbol='{self.company_symbol}', type='{self.report_type}', period='{self.period_type}', date='{self.period_end_date}')>"
