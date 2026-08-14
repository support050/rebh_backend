# backend/app/schemas/scraped_financials.py
"""
Pydantic schemas for scraped financial data API requests/responses.
"""
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any, List
from datetime import date, datetime
from enum import Enum


class PeriodTypeEnum(str, Enum):
    """Period type for financial reports."""
    ANNUALLY = "Annually"
    QUARTERLY = "Quarterly"


class ReportTypeEnum(str, Enum):
    """Report type for financial statements."""
    BALANCE_SHEET = "Balance_Sheet"
    INCOME_STATEMENT = "Income_Statement"
    CASH_FLOWS = "Cash_Flows"


# ==================== Company Schemas ====================

class CompanyBase(BaseModel):
    """Base schema for company data."""
    symbol: str = Field(..., description="Company symbol (e.g., '2222', '4020')")
    name_en: Optional[str] = Field(None, description="Company name in English")
    name_ar: Optional[str] = Field(None, description="Company name in Arabic")
    sector: Optional[str] = Field(None, description="Company sector")


class CompanyCreate(CompanyBase):
    """Schema for creating a new company."""
    pass


class CompanyUpdate(BaseModel):
    """Schema for updating a company."""
    name_en: Optional[str] = None
    name_ar: Optional[str] = None
    sector: Optional[str] = None
    last_scraped_at: Optional[datetime] = None


class CompanyResponse(CompanyBase):
    """Schema for company response."""
    id: int
    last_scraped_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


# ==================== Financial Report Schemas ====================

class FinancialReportBase(BaseModel):
    """Base schema for financial report."""
    company_symbol: str = Field(..., description="Company symbol")
    period_type: PeriodTypeEnum = Field(..., description="Period type: Annually or Quarterly")
    report_type: ReportTypeEnum = Field(..., description="Report type: Balance_Sheet, Income_Statement, or Cash_Flows")
    period_end_date: date = Field(..., description="Period end date (e.g., '2023-12-31')")
    metrics: Optional[Dict[str, Any]] = Field(None, description="Financial metrics as key-value pairs")


class FinancialReportCreate(FinancialReportBase):
    """Schema for creating a financial report."""
    raw_data: Optional[Dict[str, Any]] = Field(None, description="Raw scraped table data")


class FinancialReportResponse(FinancialReportBase):
    """Schema for financial report response."""
    id: int
    raw_data: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class FinancialReportListResponse(BaseModel):
    """Schema for list of financial reports."""
    reports: List[FinancialReportResponse]
    total: int
    symbol: str


# ==================== Ingest Schemas (from Scraper) ====================

class ReportIngestItem(BaseModel):
    """Single report item for ingestion from scraper."""
    report_type: ReportTypeEnum = Field(..., description="Type of report")
    period_type: PeriodTypeEnum = Field(..., description="Period type")
    period_end_date: date = Field(..., description="Period end date")
    metrics: Dict[str, Any] = Field(..., description="Financial metrics")
    raw_data: Optional[List[Dict[str, Any]]] = Field(None, description="Raw table rows")


class IngestRequest(BaseModel):
    """
    Schema for ingesting scraped data from Playwright script.
    Supports multiple reports for a single company in one request.
    """
    company_symbol: str = Field(..., description="Company symbol from Saudi Exchange")
    company_name_en: Optional[str] = Field(None, description="Company English name")
    company_name_ar: Optional[str] = Field(None, description="Company Arabic name")
    sector: Optional[str] = Field(None, description="Company sector")
    reports: List[ReportIngestItem] = Field(..., description="List of financial reports to ingest")
    
    @field_validator('company_symbol')
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        """Ensure symbol is not empty and trimmed."""
        if not v or not v.strip():
            raise ValueError('company_symbol cannot be empty')
        return v.strip()


class IngestResponse(BaseModel):
    """Response schema for ingest endpoint."""
    success: bool
    message: str
    company_symbol: str
    reports_processed: int
    reports_created: int
    reports_updated: int


class BulkIngestRequest(BaseModel):
    """Schema for bulk ingestion of multiple companies."""
    companies: List[IngestRequest] = Field(..., description="List of companies to ingest")


class BulkIngestResponse(BaseModel):
    """Response schema for bulk ingest."""
    success: bool
    message: str
    total_companies: int
    total_reports_processed: int
    failed_companies: List[str]


# ==================== Historical Financial Data Response ====================

class FinancialPeriodData(BaseModel):
    """Financial data for a specific period."""
    period_end_date: date
    period_type: PeriodTypeEnum
    metrics: Dict[str, Any]


class HistoricalFinancialsResponse(BaseModel):
    """
    Response for historical financial data.
    Organized by report type with all periods.
    """
    symbol: str
    company_name: Optional[str] = None
    balance_sheets: List[FinancialPeriodData] = []
    income_statements: List[FinancialPeriodData] = []
    cash_flows: List[FinancialPeriodData] = []
    
    class Config:
        from_attributes = True


# ==================== Transformed Data for Frontend ====================

class FinancialTableRow(BaseModel):
    """A single row in the financial table (metric name + values by period)."""
    metric_name: str
    values: Dict[str, Any]  # Key: period label (e.g., "2023-12-31"), Value: metric value


class FinancialTableResponse(BaseModel):
    """
    Financial data formatted as table for frontend.
    Years as columns, metrics as rows.
    """
    symbol: str
    report_type: ReportTypeEnum
    period_type: PeriodTypeEnum
    periods: List[str]  # Column headers (dates)
    rows: List[FinancialTableRow]  # Metric rows with values
