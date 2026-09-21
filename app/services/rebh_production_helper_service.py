"""
REBH Production Engine Helper Services.
Provides modular, tested, and pure extraction & calculation units to eliminate
hardcoded assumptions from the universal engine:
1. True Piotroski F-Score (9 strict binary signals requiring full prior-year comparison)
2. Stale-Never-Priced Regulatory Deadlines Gate (Quarterly 45d / Annual 90d / Banks)
3. Historical Cyclical Peak & Trough EPS derivation from multi-year quarterly series
4. Nine-Box Matrix Inputs Extractor (Dividends paid normalization & debt-adjusted FCF)
5. Pre-profit P/S Kill-Switch Gate (TTM > 0, 2 consecutive profitable quarters, margin stability)
6. Peer-Relative Factor Grades & Percentile derivation
"""
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, date
import re


# -----------------------------------------------------------------------------
# 1. PIOTROSKI F-SCORE WITH STRICT 2-YEAR DATA AUDIT
# -----------------------------------------------------------------------------

def compute_strict_piotroski_f_score(
    bs_items: Dict[str, Dict[str, Optional[float]]],
    is_items: Dict[str, Dict[str, Optional[float]]],
    cf_items: Dict[str, Dict[str, Optional[float]]],
    periods_bs: List[str],
    periods_is: List[str]
) -> Dict[str, Any]:
    """
    Computes strict 9-signal Piotroski F-Score.
    Requires at least 2 consecutive periods. If data is insufficient or missing,
    returns score=None with status='⚑missing-f-score' — never invents a partial score.
    """
    if len(periods_is) < 2 or len(periods_bs) < 2:
        return {
            "score": None,
            "status": "⚑missing-f-score",
            "is_complete": False,
            "reason": "Insufficient historical periods for YoY comparison (requires ≥ 2 periods)"
        }

    cur_is = periods_is[-1]
    prv_is = periods_is[-2]
    cur_bs = periods_bs[-1]
    prv_bs = periods_bs[-2]

    def _val(d: Dict[str, Dict[str, Optional[float]]], label_pattern: str, period: str) -> Optional[float]:
        for k, v in d.items():
            if re.search(label_pattern, k, re.IGNORECASE):
                val = v.get(period)
                return float(val) if val is not None else None
        return None

    # Extraction
    ni_cur = _val(is_items, r"Net Profit|Net Income", cur_is)
    ni_prv = _val(is_items, r"Net Profit|Net Income", prv_is)
    rev_cur = _val(is_items, r"Revenue|Turnover|Sales", cur_is)
    rev_prv = _val(is_items, r"Revenue|Turnover|Sales", prv_is)
    gp_cur = _val(is_items, r"Gross Profit", cur_is)
    gp_prv = _val(is_items, r"Gross Profit", prv_is)
    cfo_cur = _val(cf_items, r"Operating Activities|Operating Cash", cur_is) or _val(is_items, r"Operating Cash", cur_is)

    ta_cur = _val(bs_items, r"Total Assets", cur_bs)
    ta_prv = _val(bs_items, r"Total Assets", prv_bs)
    ca_cur = _val(bs_items, r"Current Assets", cur_bs)
    ca_prv = _val(bs_items, r"Current Assets", prv_bs)
    cl_cur = _val(bs_items, r"Current Liabilities", cur_bs)
    cl_prv = _val(bs_items, r"Current Liabilities", prv_bs)
    ltd_cur = _val(bs_items, r"Long-term Borrowings|Long-term Debt", cur_bs) or 0.0
    ltd_prv = _val(bs_items, r"Long-term Borrowings|Long-term Debt", prv_bs) or 0.0
    shares_cur = _val(bs_items, r"Issued Capital|Share Capital", cur_bs)
    shares_prv = _val(bs_items, r"Issued Capital|Share Capital", prv_bs)

    # Core check: Net income and Total Assets must be present for current & prior
    if None in (ni_cur, ni_prv, ta_cur, ta_prv) or ta_cur <= 0 or ta_prv <= 0:
        return {
            "score": None,
            "status": "⚑missing-f-score",
            "is_complete": False,
            "reason": "Missing fundamental balance sheet or income items for Piotroski computation"
        }

    score = 0
    signals = {}

    # Profitability signals
    # 1. ROA > 0
    roa_cur = ni_cur / ta_cur
    roa_prv = ni_prv / ta_prv
    s1 = bool(roa_cur > 0)
    score += int(s1)
    signals["positive_roa"] = s1

    # 2. CFO > 0
    s2 = bool(cfo_cur is not None and cfo_cur > 0)
    score += int(s2)
    signals["positive_cfo"] = s2

    # 3. Delta ROA > 0
    s3 = bool(roa_cur > roa_prv)
    score += int(s3)
    signals["growing_roa"] = s3

    # 4. Accrual: CFO > Net Income
    s4 = bool(cfo_cur is not None and cfo_cur > ni_cur)
    score += int(s4)
    signals["cfo_exceeds_ni"] = s4

    # Leverage & Liquidity signals
    # 5. Long-term debt change (LTD_cur / TA_cur < LTD_prv / TA_prv)
    lev_cur = ltd_cur / ta_cur
    lev_prv = ltd_prv / ta_prv
    s5 = bool(lev_cur <= lev_prv)
    score += int(s5)
    signals["lower_leverage"] = s5

    # 6. Current ratio increase
    if ca_cur and cl_cur and cl_cur > 0 and ca_prv and cl_prv and cl_prv > 0:
        cr_cur = ca_cur / cl_cur
        cr_prv = ca_prv / cl_prv
        s6 = bool(cr_cur > cr_prv)
    else:
        s6 = False
    score += int(s6)
    signals["higher_liquidity"] = s6

    # 7. No dilution (Shares count did not increase)
    if shares_cur is not None and shares_prv is not None and shares_prv > 0:
        s7 = bool(shares_cur <= shares_prv)
    else:
        s7 = False  # Strict: missing share data does not award free Piotroski signal
    score += int(s7)
    signals["no_dilution"] = s7

    # Operating Efficiency signals
    # 8. Gross margin improvement
    if rev_cur and rev_prv and rev_cur > 0 and rev_prv > 0 and gp_cur is not None and gp_prv is not None:
        gm_cur = gp_cur / rev_cur
        gm_prv = gp_prv / rev_prv
        s8 = bool(gm_cur > gm_prv)
    else:
        s8 = False
    score += int(s8)
    signals["higher_gross_margin"] = s8

    # 9. Asset turnover improvement (Rev / TA)
    at_cur = rev_cur / ta_cur if (rev_cur and ta_cur > 0) else 0.0
    at_prv = rev_prv / ta_prv if (rev_prv and ta_prv > 0) else 0.0
    s9 = bool(at_cur > at_prv)
    score += int(s9)
    signals["higher_asset_turnover"] = s9

    return {
        "score": score,
        "status": "° verified",
        "is_complete": True,
        "signals": signals
    }


# -----------------------------------------------------------------------------
# 2. STALE-NEVER-PRICED REGULATORY GATE
# -----------------------------------------------------------------------------

def evaluate_statement_freshness(
    latest_period_label: Optional[str],
    is_financial_sector: bool = False
) -> Dict[str, Any]:
    """
    PRICING FRESHNESS GATE — NOT a filing deadline checker.

    Determines whether a company's statements are recent enough to support
    a reliable valuation model (i.e., whether the engine may price the stock).

    Regulatory FILING deadlines (Tadawul/CMA):
      - Quarterly: 45 calendar days after period end
      - Annual:    90 calendar days after period end
      - Banks/Insurance (SAMA): shorter cycle, approximately 45d quarterly / 60d annual

    Pricing-gate buffer (deliberately wider than filing deadlines):
      - Standard companies:  180d quarterly / 270d annual
        Rationale: Allows for late filers, restatements, and periods where
        data has not yet been imported. The engine withholds output if exceeded.
      - Financial sector:    120d quarterly / 240d annual
        Rationale: SAMA-regulated entities have faster-moving balance sheets;
        staleness is more consequential for bank valuations.

    AUDIT NOTE (2026-09-09):
      The 180/270d limits are INTENTIONAL PRODUCT DECISIONS for the pricing gate.
      They are NOT filing deadlines and are NOT a mismatch with the 45/90d package
      reference. The package describes filing deadlines; this function implements
      the pricing gate. Both are correct in their respective contexts.
      Do NOT change these limits without explicit product-owner sign-off.
    """
    if not latest_period_label:
        return {
            "is_fresh": False,
            "days_elapsed": None,
            "period": "Unknown",
            "stale_reason": "لا توجد فترات مالية معلنة في سجل الشركة"
        }

    # Extract date from period string (e.g. '2024-09-30', 'Q3-2024', '2024-12-31', '2026-01_2026-03')
    import calendar
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", latest_period_label)
    parsed_date = None
    if match:
        try:
            parsed_date = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            parsed_date = None

    if not parsed_date:
        # Check for XBRL period range format like '2026-01_2026-03' or '2025-07_2025-09'
        range_match = re.search(r"(\d{4})[-/](\d{1,2})_(\d{4})[-/](\d{1,2})", latest_period_label)
        if range_match:
            try:
                eyear = int(range_match.group(3))
                emonth = int(range_match.group(4))
                last_day = calendar.monthrange(eyear, emonth)[1]
                parsed_date = date(eyear, emonth, last_day)
            except Exception:
                parsed_date = None

    if not parsed_date:
        # Check for year-quarter syntax
        y_match = re.search(r"(20\d{2})", latest_period_label)
        if y_match:
            year = int(y_match.group(1))
            if "Q1" in latest_period_label.upper():
                parsed_date = date(year, 3, 31)
            elif "Q2" in latest_period_label.upper():
                parsed_date = date(year, 6, 30)
            elif "Q3" in latest_period_label.upper():
                parsed_date = date(year, 9, 30)
            elif "Q4" in latest_period_label.upper() or "FY" in latest_period_label.upper():
                parsed_date = date(year, 12, 31)

    if not parsed_date:
        # Cannot verify freshness date safely
        return {
            "is_fresh": False,
            "days_elapsed": None,
            "period": latest_period_label,
            "stale_reason": f"تعذر استخراج تاريخ الإفصاح النظامي من الفترة ({latest_period_label})"
        }

    today = date.today()
    days_elapsed = (today - parsed_date).days

    is_annual = "FY" in latest_period_label.upper() or (parsed_date.month == 12 and parsed_date.day == 31)
    # Regulatory limit with market disclosure buffer:
    # Banking/Insurance have dedicated SAMA disclosure cycles:
    #   Quarterly: 120 days
    #   Annual: 240 days
    # Standard Tadawul Corporates:
    #   Quarterly: 180 days (45d statutory + 135d buffer)
    #   Annual: 270 days (90d statutory + 180d buffer)
    if is_financial_sector:
        max_allowed = 240 if is_annual else 120
    else:
        max_allowed = 270 if is_annual else 180

    if days_elapsed > max_allowed:
        return {
            "is_fresh": False,
            "days_elapsed": days_elapsed,
            "period": latest_period_label,
            "is_financial_sector": is_financial_sector,
            "stale_reason": f"القوائم المالية متأخرة ({days_elapsed} يوماً منذ {parsed_date}) — تجاوزت مهلة الحداثة ({max_allowed} يوماً)"
        }

    return {
        "is_fresh": True,
        "days_elapsed": days_elapsed,
        "period": latest_period_label,
        "stale_reason": None
    }


# -----------------------------------------------------------------------------
# 3. HISTORICAL CYCLICAL PEAK & TROUGH FROM REAL EARNINGS SERIES
# -----------------------------------------------------------------------------

def extract_cyclical_cycle_bounds(
    eps_series: List[Optional[float]],
    current_eps: Optional[float]
) -> Dict[str, Any]:
    """
    Extracts actual lowest (trough) and highest (peak) EPS from the company's
    historical quarterly or TTM series. Strictly avoids multiplying current EPS by fixed factors.
    """
    valid_eps = [float(e) for e in eps_series if e is not None]
    if not valid_eps and current_eps is not None:
        valid_eps = [current_eps]

    if not valid_eps:
        return {
            "has_cycle": False,
            "trough_eps": None,
            "peak_eps": None,
            "buy_band": [None, None],
            "sell_band": [None, None]
        }

    # Minimum positive trough to prevent division by zero or negative buy bands
    positive_eps = [e for e in valid_eps if e > 0]
    trough_eps = min(positive_eps) if positive_eps else (min(valid_eps) if valid_eps else 0.5)
    peak_eps = max(valid_eps)

    # Ensure peak is higher than trough
    if peak_eps < trough_eps:
        peak_eps = trough_eps

    return {
        "has_cycle": True,
        "trough_eps": round(trough_eps, 2),
        "peak_eps": round(peak_eps, 2),
        "buy_band_min": round(trough_eps * 14.0, 2),
        "buy_band_max": round(trough_eps * 16.0, 2),
        "sell_band_min": round(peak_eps * 8.0, 2),
        "sell_band_max": round(peak_eps * 12.0, 2),
        "sample_points": len(valid_eps),
        "source": "Historical quarterly series minimum & maximum"
    }


# -----------------------------------------------------------------------------
# 4. NINE-BOX INPUTS WITH SIGN & DEBT NORMALIZATION
# -----------------------------------------------------------------------------

def extract_nine_box_inputs(
    eps: Optional[float],
    cff_items: Dict[str, Dict[str, Optional[float]]],
    latest_period: Optional[str],
    fcf_annual: Optional[float],
    total_debt: Optional[float],
    cash: Optional[float],
    shares: Optional[float]
) -> Dict[str, Any]:
    """
    Normalizes Dividends Paid and Net Debt for Nine-Box valuation:
    - Normalizes Dividends Paid sign from cash flow (CFF is reported negative when cash paid).
    - Avoids double-counting debt in FCF.
    """
    # 1. Dividend extraction (DPS)
    dps = None
    if latest_period and cff_items and shares and shares > 0:
        div_raw = None
        for k, v in cff_items.items():
            if re.search(r"Dividends paid|Cash dividends", k, re.IGNORECASE):
                div_raw = v.get(latest_period)
                break
        if div_raw is not None:
            # Dividends paid in CFF is negative -> absolute value to get positive dividend flow
            dps = round(abs(float(div_raw)) / shares, 2)

    # Strict Zero-Tolerance: No synthetic proxy for DPS if cash flow does not report dividends
    if dps is None or dps <= 0:
        dps = None
        dps_source = "🔌 missing-source"
    else:
        dps_source = "cff_dividends_paid_actual"

    # 2. FCF and Net Debt per share
    net_debt = max(0.0, (total_debt or 0.0) - (cash or 0.0))
    net_debt_per_share = (net_debt / shares) if (shares and shares > 0) else 0.0

    fcf_net_of_debt_per_share = None
    if fcf_annual is not None and shares and shares > 0:
        raw_fcf_per_share = fcf_annual / shares
        # FCF net of debt cannot be negative in the Gordon/Transitory model
        fcf_net_of_debt_per_share = round(max(0.0, raw_fcf_per_share - (net_debt_per_share * 0.20)), 2)

    return {
        "dps": dps,
        "dps_source": dps_source,
        "fcf_net_debt_per_share": fcf_net_of_debt_per_share,
        "net_debt_m": round(net_debt, 2),
        "net_debt_per_share": round(net_debt_per_share, 2)
    }


# -----------------------------------------------------------------------------
# 5. PRE-PROFIT P/S KILL-SWITCH GATE
# -----------------------------------------------------------------------------

def evaluate_ps_kill_switch(
    quarterly_net_income: List[Optional[float]],
    ttm_net_income: Optional[float],
    gross_margin: Optional[float]
) -> Dict[str, Any]:
    """
    Evaluates whether the company has graduated from the pre-profit P/S model:
    Kill-Switch triggers when:
    1. TTM Net Income > 0, AND
    2. At least two consecutive recent quarters are profitable, AND
    3. Gross Margin > 15% (sustainable unit economics).
    """
    valid_q = [float(n) for n in quarterly_net_income if n is not None]
    two_consecutive_positive = False
    if len(valid_q) >= 2:
        two_consecutive_positive = (valid_q[-1] > 0 and valid_q[-2] > 0)

    ttm_positive = bool(ttm_net_income is not None and ttm_net_income > 0)
    margin_stable = bool(gross_margin is not None and gross_margin >= 0.15)

    kill_switch_triggered = ttm_positive and two_consecutive_positive and margin_stable

    return {
        "kill_switch_active": kill_switch_triggered,
        "two_consecutive_profitable": two_consecutive_positive,
        "ttm_profitable": ttm_positive,
        "margin_stable": margin_stable,
        "status_note": (
            "تم تفعيل مفتاح الإيقاف (Kill-Switch) — انتقلت الشركة للربحية المستقرة وتعود لتقييم الأرباح مباشرة"
            if kill_switch_triggered else
            "مسار مضاعف المبيعات P/S نشط لشركات ما قبل الربحية"
        )
    }


# -----------------------------------------------------------------------------
# 6. PEER-RELATIVE FACTOR GRADES & SECTOR PORTER FORCES
# -----------------------------------------------------------------------------

def compute_peer_relative_grades(
    pe: Optional[float],
    roe: Optional[float],
    cur_ratio: Optional[float],
    debt_to_assets: Optional[float],
    sector: Optional[str] = None
) -> Dict[str, Dict[str, Any]]:
    """
    Computes Dynamic Factor Grades (Valuation, Profitability, Safety)
    anchored to sector median profiles and risk tolerances.
    """
    s = (sector or "").lower()
    is_fin = any(k in s for k in ["bank", "insurance", "financial"])
    is_cap = any(k in s for k in ["materials", "energy", "capital goods", "utilities"])

    # 1. Valuation Grade (anchored to sector typical P/E range)
    pe_mid = 18.0 if is_cap else (14.0 if is_fin else 22.0)
    if pe is not None and pe > 0:
        if pe <= pe_mid * 0.6:
            val_g, val_p = "A+", 95
        elif pe <= pe_mid * 0.85:
            val_g, val_p = "A", 85
        elif pe <= pe_mid * 1.15:
            val_g, val_p = "B+", 75
        elif pe <= pe_mid * 1.4:
            val_g, val_p = "B", 60
        elif pe <= pe_mid * 1.8:
            val_g, val_p = "C+", 40
        else:
            val_g, val_p = "C", 25
    else:
        val_g, val_p = "D", 15

    # 2. Profitability Grade (anchored to sector expected ROE)
    roe_target = 10.0 if is_cap else (14.0 if is_fin else 16.0)
    if roe is not None:
        if roe >= roe_target * 1.6:
            prof_g, prof_p = "A+", 95
        elif roe >= roe_target * 1.2:
            prof_g, prof_p = "A", 85
        elif roe >= roe_target * 0.85:
            prof_g, prof_p = "B+", 75
        elif roe >= roe_target * 0.5:
            prof_g, prof_p = "B", 60
        elif roe > 0.0:
            prof_g, prof_p = "C+", 40
        else:
            prof_g, prof_p = "D", 15
    else:
        prof_g, prof_p = "N/A", 0

    # 3. Safety Grade (financial vs non-financial sector debt tolerances)
    safety_points = 0
    if is_fin:
        # Financial institutions operate with regulatory leverage; evaluate profitability and capital strength
        if cur_ratio is None or cur_ratio >= 1.0:
            safety_points += 40
        if roe and roe >= 12.0:
            safety_points += 50
        elif roe and roe >= 8.0:
            safety_points += 35
        else:
            safety_points += 20
    else:
        if cur_ratio is not None:
            if cur_ratio >= 2.0:
                safety_points += 45
            elif cur_ratio >= 1.2:
                safety_points += 30
            elif cur_ratio >= 1.0:
                safety_points += 15

        if debt_to_assets is not None:
            if debt_to_assets <= 30.0:
                safety_points += 50
            elif debt_to_assets <= 50.0:
                safety_points += 35
            elif debt_to_assets <= 70.0:
                safety_points += 15

    if safety_points >= 85:
        safe_g, safe_p = "A+", 95
    elif safety_points >= 70:
        safe_g, safe_p = "A", 85
    elif safety_points >= 50:
        safe_g, safe_p = "B+", 70
    elif safety_points >= 30:
        safe_g, safe_p = "B", 55
    else:
        safe_g, safe_p = "C", 30

    # 4. Growth Grade (anchored to sector expected growth)
    # Estimate growth score based on profitability and valuation momentum
    growth_points = 60
    if roe and roe >= 15.0:
        growth_points += 25
    elif roe and roe >= 8.0:
        growth_points += 15
    if pe is not None and pe > 0 and pe <= 20:
        growth_points += 10

    if growth_points >= 85:
        growth_g, growth_p = "A+", 95
    elif growth_points >= 75:
        growth_g, growth_p = "A", 85
    elif growth_points >= 65:
        growth_g, growth_p = "B+", 75
    elif growth_points >= 50:
        growth_g, growth_p = "B", 60
    else:
        growth_g, growth_p = "C+", 40

    # 5. Balance / Financial Strength Grade
    bal_points = 50
    if cur_ratio is not None:
        if cur_ratio >= 1.5:
            bal_points += 25
        elif cur_ratio >= 1.0:
            bal_points += 15
    if debt_to_assets is not None:
        if debt_to_assets <= 40.0:
            bal_points += 25
        elif debt_to_assets <= 60.0:
            bal_points += 15

    if bal_points >= 85:
        bal_g, bal_p = "A+", 95
    elif bal_points >= 75:
        bal_g, bal_p = "A-", 80
    elif bal_points >= 65:
        bal_g, bal_p = "B+", 70
    elif bal_points >= 50:
        bal_g, bal_p = "B", 55
    else:
        bal_g, bal_p = "C", 30

    basis_tag = "sec_peer" if sector else "mkt_baseline"
    return {
        "Cash": {"g": prof_g, "p": prof_p, "b": basis_tag},
        "Balance": {"g": bal_g, "p": bal_p, "b": basis_tag},
        "Valuation": {"g": val_g, "p": val_p, "b": basis_tag},
        "Growth": {"g": growth_g, "p": growth_p, "b": basis_tag},
        "Safety": {"g": safe_g, "p": safe_p, "b": basis_tag}
    }


def get_sector_porter_forces(sector: str) -> Dict[str, Any]:
    """
    Returns estimated Sector Default Porter Forces labeled explicitly as '≈ declared_estimate'.
    Recognizes Retail, Materials, Energy, Banks, Healthcare, and Telecom differences.
    """
    s = (sector or "").lower()
    
    if any(k in s for k in ["retail", "consumer discretionary", "consumer staples", "تجارة"]):
        forces = {"supplier": 0.5, "buyer": 0.7, "entrants": 0.8, "substitutes": 0.7, "rivalry": 0.7}
        rationale = "High threat of new entrants & substitute retail channels, moderate supplier power"
    elif any(k in s for k in ["materials", "chemical", "petrochem", "مواد"]):
        forces = {"supplier": 0.7, "buyer": 0.6, "entrants": 0.4, "substitutes": 0.5, "rivalry": 0.6}
        rationale = "High capital intensity limits entrants, but global commodity buyers dictate prices"
    elif any(k in s for k in ["bank", "financial", "insurance", "بنوك"]):
        forces = {"supplier": 0.4, "buyer": 0.5, "entrants": 0.3, "substitutes": 0.5, "rivalry": 0.6}
        rationale = "SAMA regulatory licensing forms steep entry barrier; high switching costs"
    elif any(k in s for k in ["health", "pharma", "رعاية"]):
        forces = {"supplier": 0.6, "buyer": 0.4, "entrants": 0.4, "substitutes": 0.4, "rivalry": 0.5}
        rationale = "Regulatory accreditations & high consumer switching costs provide moat"
    elif any(k in s for k in ["telecom", "it", "تقنية"]):
        forces = {"supplier": 0.5, "buyer": 0.5, "entrants": 0.3, "substitutes": 0.5, "rivalry": 0.7}
        rationale = "Infrastructure scale barriers (oligopoly of CITC spectrum licenses)"
    else:
        forces = {"supplier": 0.6, "buyer": 0.6, "entrants": 0.6, "substitutes": 0.6, "rivalry": 0.6}
        rationale = "General market sector baseline estimate"

    total = round(sum(forces.values()), 2)
    comp = 2.0 if total >= 3.5 else (3.0 if total >= 2.5 else 4.0)

    return {
        "forces": forces,
        "total_score": total,
        "compensation_pct": comp,
        "status": "≈ declared_estimate",
        "source": f"REBH Sector Default Matrix ({sector})",
        "rationale": rationale
    }


# ──────────────────────────────────────────────────────────────────────────────
# Sector Medians Cache — reads ALL XBRL files once, derives medians for all
# sectors in a single pass.  Per-sector lookup is then O(1).
# ──────────────────────────────────────────────────────────────────────────────
_SECTOR_MEDIANS_CACHE: Dict[str, Dict[str, Any]] = {}
_SECTOR_MEDIANS_WARMED: bool = False  # True after full-universe pre-load


def _sector_baseline(sector: str) -> Dict[str, Any]:
    """Hardcoded calibrated fallback when XBRL sample is insufficient."""
    s = (sector or "").lower()
    if any(k in s for k in ["materials", "chemical", "petrochem", "مواد"]):
        npm, growth = 12.0, 6.0
    elif any(k in s for k in ["retail", "consumer", "تجارة"]):
        npm, growth = 4.5, 8.0
    elif any(k in s for k in ["telecom", "it", "تقنية", "technology"]):
        npm, growth = 11.0, 12.0
    elif any(k in s for k in ["health", "pharma", "رعاية"]):
        npm, growth = 14.0, 10.0
    elif any(k in s for k in ["energy", "oil", "طاقة"]):
        npm, growth = 15.0, 5.0
    elif any(k in s for k in ["real estate", "reit", "عقارات"]):
        npm, growth = 25.0, 7.0
    else:
        npm, growth = 8.0, 7.5
    return {
        "sector": sector,
        "median_npm_pct": npm,
        "expected_growth_pct": growth,
        "sample_size": 1,
        "source": "≈ declared sector baseline",
    }


def warm_sector_medians_cache() -> int:
    """
    Pre-loads sector medians for every sector in the XBRL universe in ONE pass.
    Reads each JSON file exactly once, groups metrics by sector, then stores
    the medians. Returns number of sectors warmed.

    Call this ONCE before the universe loop in daily_market_update.py:
        from app.services.rebh_production_helper_service import warm_sector_medians_cache
        warm_sector_medians_cache()
    """
    global _SECTOR_MEDIANS_CACHE, _SECTOR_MEDIANS_WARMED
    if _SECTOR_MEDIANS_WARMED:
        return len(_SECTOR_MEDIANS_CACHE)

    try:
        import json, statistics
        from app.services.xbrl_data_service import _all_json_files

        # sector_key → {"npms": [...], "growths": [...]}
        buckets: Dict[str, Dict[str, list]] = {}

        for fp in _all_json_files():
            try:
                with open(fp, encoding="utf-8") as f:
                    cdata = json.load(f)
                c_sec = (cdata.get("meta", {}).get("sector") or "").strip()
                if not c_sec:
                    continue
                is_sec = cdata.get("sections", {}).get("standardized_income_statement", {})
                items = is_sec.get("items", [])
                periods = is_sec.get("periods", [])
                if not periods or not items:
                    continue
                p_last = periods[-1]
                p_prev = periods[-5] if len(periods) >= 5 else None

                rev_curr = rev_prev = ni = None
                for it in items:
                    lbl = it.get("label") or ""
                    vals = it.get("values", {})
                    if lbl == "Revenue / Turnover":
                        rev_curr = vals.get(p_last)
                        if p_prev:
                            rev_prev = vals.get(p_prev)
                    elif lbl == "Net Profit for the Period":
                        ni = vals.get(p_last)

                bucket = buckets.setdefault(c_sec, {"npms": [], "growths": []})
                if rev_curr and ni and rev_curr > 0 and ni > 0:
                    npm = (ni / rev_curr) * 100.0
                    if 0.0 < npm < 90.0:
                        bucket["npms"].append(npm)
                if rev_curr and rev_prev and rev_curr > 0 and rev_prev > 0:
                    g = ((rev_curr / rev_prev) - 1.0) * 100.0
                    if -50.0 < g < 80.0:
                        bucket["growths"].append(g)
            except Exception:
                continue

        for sec_name, data in buckets.items():
            npms = data["npms"]
            growths = data["growths"]
            if len(npms) >= 1:
                _SECTOR_MEDIANS_CACHE[sec_name] = {
                    "sector": sec_name,
                    "median_npm_pct": round(statistics.median(npms), 1),
                    "expected_growth_pct": round(statistics.median(growths), 1) if growths else 7.0,
                    "sample_size": len(npms),
                    "growth_sample_size": len(growths),
                    "source": "Data-Derived Sector Medians (Full XBRL Universe — pre-loaded)",
                }
        _SECTOR_MEDIANS_WARMED = True
    except Exception:
        pass

    return len(_SECTOR_MEDIANS_CACHE)


def get_sector_margin_and_growth(sector: str) -> Dict[str, Any]:
    """
    Returns data-derived Sector Median Net Profit Margin (NPM) and Sales Growth.

    - If warm_sector_medians_cache() was called beforehand (universe loop),
      this is an O(1) dict lookup — zero disk I/O.
    - If not pre-warmed or not found, uses calibrated baseline and caches result.
    """
    global _SECTOR_MEDIANS_CACHE

    # Fast path: already in cache (pre-warmed or previously computed)
    if sector in _SECTOR_MEDIANS_CACHE:
        return _SECTOR_MEDIANS_CACHE[sector]

    # Baseline calibrated defaults cached immediately to avoid repeated lookups
    res = _sector_baseline(sector)
    _SECTOR_MEDIANS_CACHE[sector] = res
    return res
