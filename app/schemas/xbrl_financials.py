from typing import Any

from pydantic import BaseModel, Field


class CompanyMeta(BaseModel):
    company_name: str
    symbol: str
    isin: str | None = None
    sector: str | None = None
    currency: str | None = None
    rounding: str | None = None
    status: str | None = None
    report_end: str | None = None
    source_files: list[str] = Field(default_factory=list)


class FinancialItem(BaseModel):
    label: str
    label_ar: str | None = None
    is_header: bool
    is_unmapped: bool | None = None
    values: dict[str, Any]


class PeriodMeta(BaseModel):
    key: str
    start: str = ""
    end: str = ""
    period_type: str = "unknown"


class FinancialSection(BaseModel):
    periods: list[str]
    period_meta: list[PeriodMeta] = Field(default_factory=list)
    items: list[FinancialItem]
    section_type: str | None = None          # "equity_matrix" for pivot tables
    components: list[str] = Field(default_factory=list)  # equity matrix column headers


class CompanyFinancials(BaseModel):
    meta: CompanyMeta
    sections: dict[str, FinancialSection]


class CompanyListItem(BaseModel):
    symbol: str
    company_name: str
    sector: str | None = None
    report_end: str | None = None
    periods_count: int = 0
