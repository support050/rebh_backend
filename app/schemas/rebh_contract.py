"""
Pydantic Schemas for REBH Universal Production Data Contract
Compliant with Phase 0.2 of REBH-AI-IMPLEMENTATION-PLAN.md.
"""
from typing import Dict, List, Optional, Any, Union
from pydantic import BaseModel, Field


class BalanceIdentityStatus(BaseModel):
    is_valid: bool = Field(..., description="True if Assets == Liabilities + Equity within tolerance")
    assets: Optional[float] = None
    liabilities: Optional[float] = None
    equity: Optional[float] = None
    discrepancy: Optional[float] = None
    tolerance_applied: Optional[float] = None
    reason: Optional[str] = None


class SafetyGradeDetail(BaseModel):
    name: str
    val: str
    score: int
    threshold: str
    pass_flag: bool


class FactorGrade(BaseModel):
    g: str = Field(..., description="Letter grade e.g. A+, B, C")
    p: int = Field(..., description="Percentile 0-100")
    b: str = Field(..., description="Baseline indicator (sector/market)")


class DiscreteQuarterSeries(BaseModel):
    periods: List[str] = Field(default_factory=list)
    revenue: List[Optional[float]] = Field(default_factory=list)
    gross_profit: List[Optional[float]] = Field(default_factory=list)
    operating_profit: List[Optional[float]] = Field(default_factory=list)
    net_profit: List[Optional[float]] = Field(default_factory=list)
    eps: List[Optional[float]] = Field(default_factory=list)
    cfo: List[Optional[float]] = Field(default_factory=list)
    cfi: List[Optional[float]] = Field(default_factory=list)
    cff: List[Optional[float]] = Field(default_factory=list)
    delta_cash: List[Optional[float]] = Field(default_factory=list)
    missing_reasons: Dict[str, str] = Field(default_factory=dict)


class TTMData(BaseModel):
    revenue: Optional[float] = None
    gross_profit: Optional[float] = None
    operating_profit: Optional[float] = None
    net_profit: Optional[float] = None
    ebit: Optional[float] = None
    ebitda: Optional[float] = None
    cfo: Optional[float] = None
    capex: Optional[float] = None
    fcf: Optional[float] = None
    eps: Optional[float] = None
    discrete_quarters_count: int = 0
    is_complete: bool = False


class PorterAnalysis(BaseModel):
    supplier_power: float = Field(..., ge=0.1, le=0.9)
    buyer_power: float = Field(..., ge=0.1, le=0.9)
    threat_new_entrants: float = Field(..., ge=0.1, le=0.9)
    threat_substitutes: float = Field(..., ge=0.1, le=0.9)
    competitive_rivalry: float = Field(..., ge=0.1, le=0.9)
    total_score: float
    compensation_pct: float


class BuildUpRequiredReturn(BaseModel):
    risk_free_rate_pct: float = Field(..., description="Government or corporate Sukuk yield")
    rate_source: str = Field(..., description="'company_sukuk' or 'govt_sukuk_benchmark'")
    sukuk_symbol: Optional[str] = None
    porter_compensation_pct: float
    porter_weight: float = 0.5
    safety_compensation_pct: float
    safety_weight: float = 0.5
    required_return_r_pct: float
    formula_display: str


class NineBoxCell(BaseModel):
    name: str
    x_metric: str
    x_value: Optional[float]
    r_pct: float
    gl_pct: float
    gs_pct: float
    n_years: int
    v1: Optional[float] = None
    v2: Optional[float] = None
    v3: Optional[float] = None
    source_status: str = "° verified"



class NineBoxMatrix(BaseModel):
    r_pct: float
    gl_pct: float
    gs_pct: float
    n_years: int
    dividends: NineBoxCell
    earnings: NineBoxCell
    fcf_net_debt: NineBoxCell
    diluted_shares: Optional[float] = None
    net_debt_deducted: Optional[float] = None
    debt_allocation_rule: str = "5-year amortized debt service allocation (20% net debt / shares)"



class PriceZones(BaseModel):
    gold_max: Optional[float] = None
    silver_max: Optional[float] = None
    bronze_max: Optional[float] = None
    current_zone: Optional[str] = None


class CyclicalBands(BaseModel):
    is_cyclical: bool = False
    lowest_cycle_eps: Optional[float] = None
    highest_cycle_eps: Optional[float] = None
    buy_band_min: Optional[float] = None
    buy_band_max: Optional[float] = None
    sell_band_min: Optional[float] = None
    sell_band_max: Optional[float] = None
    rule: str = "Lowest Clear-Cycle EPS * 14-16 to Buy, Highest * 8-12 to Sell"


class LossMakerPSLadder(BaseModel):
    is_loss_maker: bool = False
    expected_npm_pct: Optional[float] = None
    expected_growth_pct: Optional[float] = None
    base_ps: Optional[float] = None
    cheap_ps: Optional[float] = None
    medium_ps: Optional[float] = None
    danger_ps: Optional[float] = None


class BankMetrics(BaseModel):
    is_bank: bool = False
    nii: Optional[float] = None
    gross_financing_income: Optional[float] = None
    earning_assets_current: Optional[float] = None
    earning_assets_prior: Optional[float] = None
    average_earning_assets: Optional[float] = None
    nim_pct: Optional[float] = Field(None, description="NII / Average Earning Assets")
    total_loans: Optional[float] = None
    total_deposits: Optional[float] = None
    ldr_pct: Optional[float] = Field(None, description="Loans / Deposits (>=95% danger)")
    casa_pct: Optional[float] = Field(None, description="(Current + Savings) / Deposits")
    provisions: Optional[float] = None
    cost_of_risk_pct: Optional[float] = Field(None, description="Provisions / Loans")
    provisions_to_revenue_pct: Optional[float] = None
    flags: List[str] = Field(default_factory=list)


class ShariahCompliance(BaseModel):
    is_compliant: Optional[bool] = None
    source_status: str = Field(default="plug", description="'verified', 'estimate', or 'plug'")
    debt_to_market_cap_pct: Optional[float] = None
    interest_income_pct: Optional[float] = None
    illiquid_assets_pct: Optional[float] = None
    committee_disclaimer: str = "هذا الفحص كمي آلي ولا يغني عن اعتماد اللجان الشرعية المعتمدة"


class RedFlagItem(BaseModel):
    code: str
    severity: str = Field(..., description="'warning' or 'critical'")
    title_ar: str
    title_en: str
    detail: str
    status_symbol: str = "⚑"


class BuyGateEvaluation(BaseModel):
    gate_passed: bool
    fail_reasons: List[str] = Field(default_factory=list)
    pass_conditions: List[str] = Field(default_factory=list)


class CapitalStructure(BaseModel):
    market_cap: Optional[float] = None
    total_debt: Optional[float] = None
    cash: Optional[float] = None
    enterprise_value: Optional[float] = None
    debt_to_equity_pct: Optional[float] = None
    net_debt: Optional[float] = None
    source_status: str = "° verified"


class RebhUniversalContract(BaseModel):
    """
    Universal Company Engine Production Contract (Phase 0.2).
    Every output must declare status:
      ° = computed from verified data
      ≈ = declared estimate with source/assumption
      ⚑ = computed warning or audit flag
      🔌 = missing source or unimplemented feed
    """
    symbol: str
    name: str
    sector: str
    industry_class: str
    market_form: str = "TADAWUL_MAIN"
    elasticity: Optional[str] = None
    bcg_stage: Optional[str] = None
    price: Optional[float] = None
    market_cap: Optional[float] = None
    currency: str = "SAR"
    as_of: Optional[str] = None
    fresh: bool
    stale_reason: Optional[str] = None
    quarantine_reason: Optional[str] = None
    balance_identity: BalanceIdentityStatus
    income_statement_status: str
    quarterly: DiscreteQuarterSeries
    annual: Dict[str, Any] = Field(default_factory=dict)
    TTM: TTMData
    grades: Dict[str, FactorGrade] = Field(default_factory=dict)
    market_rank: Optional[int] = None
    sector_rank: Optional[int] = None
    predictability: Optional[float] = None
    predictability_stars: Optional[int] = None
    capital_structure: Optional[CapitalStructure] = None
    safety: Dict[str, Any] = Field(default_factory=dict)
    porter: Optional[PorterAnalysis] = None
    build_up: Optional[BuildUpRequiredReturn] = None
    required_return: Optional[float] = None
    selected_growth: Dict[str, Any] = Field(default_factory=dict)
    nine_box: Optional[NineBoxMatrix] = None
    zones: Optional[PriceZones] = None
    cyclical_bands: Optional[CyclicalBands] = None
    ps_ladder: Optional[LossMakerPSLadder] = None
    reverse_dcf: Dict[str, Any] = Field(default_factory=dict)
    irr_decision: Dict[str, Any] = Field(default_factory=dict)
    margin_of_safety: Optional[float] = None
    red_flags: List[RedFlagItem] = Field(default_factory=list)
    buy_gate: BuyGateEvaluation
    shariah: ShariahCompliance
    bank_metrics: Optional[BankMetrics] = None
    piotroski: Optional[int] = None
    altman: Optional[float] = None
    beneish: Optional[float] = None
    magic_formula: Dict[str, Any] = Field(default_factory=dict)
    owner_yield: Optional[float] = None
    ncav: Optional[float] = None
    netnet: bool = False
    estimate: Dict[str, Any] = Field(default_factory=dict)
    provenance: Dict[str, Any] = Field(default_factory=dict)
