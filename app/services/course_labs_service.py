"""
REBH Course Labs Engine
Specialized mathematical calculators implementing Abu Saad Mishal Al-Kharfashi course labs:
1. TASI Index Lab (Scenario P/E, Bond x 1.5, Gold/Silver/Bronze Tiers)
2. Beneish M-Score (Financial Statement Manipulation Detection, Cutoff: -1.78)
3. rNPV (Risk-Adjusted NPV for Biotech & Staged Projects with DiMasi Probabilities)
4. Cut-Cut System (Post-Crisis Transient Recovery Growth I/Y)
5. User-Based Valuation (Apps & Platform Monetization, Talabat / Jahez Model)
6. Terry Smith ROCE (EBIT / (Total Assets - Current Liabilities), Benchmark 32%)
7. Dilution & Buyback Effect Diagnostic
8. TVM / IRR Multi-Method Solver
"""
from typing import Dict, Any, List, Optional
import math


# --- 1. TASI INDEX LAB ---

def calculate_tasi_index_lab(
    current_pe: float = 13.6,
    bond_yield_pct: float = 4.75,
    scenarios: Optional[List[int]] = None
) -> Dict[str, Any]:
    """
    TASI Index Lab (SP-Vlu TASI Edition):
    - Fair P/E = 1 / (Bond Yield * 1.5)
    - Scenarios: 15, 17, 20, 25
    - Gold / Silver / Bronze Tiers
    """
    scenarios = scenarios or [15, 17, 20, 25]
    b = bond_yield_pct / 100.0
    fair_pe = round(1.0 / (b * 1.5), 2) if b > 0 else 14.0
    
    scenario_diffs = {
        f"pe_{p}": {
            "pe": p,
            "implied_change_pct": round(((p / current_pe) - 1.0) * 100.0, 1)
        }
        for p in scenarios
    }
    
    return {
        "current_pe": current_pe,
        "index_earnings_yield_pct": round((100.0 / current_pe), 2),
        "bond_yield_pct": bond_yield_pct,
        "required_index_yield_pct": round(bond_yield_pct * 1.5, 2),
        "fair_pe_bond_rule": fair_pe,
        "fair_vs_current_pct": round(((fair_pe / current_pe) - 1.0) * 100.0, 1),
        "scenarios": scenario_diffs,
        "tiers": {
            "golden_max_pe": 15,
            "golden_change_pct": round(((15 / current_pe) - 1.0) * 100.0, 1),
            "silver_max_pe": 20,
            "silver_change_pct": round(((20 / current_pe) - 1.0) * 100.0, 1),
            "bronze_max_pe": 25,
            "bronze_change_pct": round(((25 / current_pe) - 1.0) * 100.0, 1)
        }
    }


# --- 2. BENEISH M-SCORE ---

def calculate_beneish_m_score(
    dsri: float = 1.0,
    gmi: float = 1.0,
    aqi: float = 1.0,
    sgi: float = 1.0,
    depi: float = 1.0,
    sgai: float = 1.0,
    tata: float = 0.02,
    lvgi: float = 1.0
) -> Dict[str, Any]:
    """
    Beneish M-Score Formula (8-variable model):
    M = -4.84 + 0.920*DSRI + 0.528*GMI + 0.404*AQI + 0.892*SGI
        + 0.115*DEPI - 0.172*SGAI + 4.679*TATA - 0.327*LVGI
    Cutoff: > -1.78 indicates high probability of accounting manipulation.
    """
    m_score = (
        -4.84
        + (0.920 * dsri)
        + (0.528 * gmi)
        + (0.404 * aqi)
        + (0.892 * sgi)
        + (0.115 * depi)
        - (0.172 * sgai)
        + (4.679 * tata)
        - (0.327 * lvgi)
    )
    
    is_manipulator = m_score > -1.78
    return {
        "m_score": round(m_score, 3),
        "is_manipulation_risk": is_manipulator,
        "verdict_ar": "احتمالية تلاعب بالقوائم المالية ⚑" if is_manipulator else "قوائم سليمة إحصائياً (لا توجد إشارة تلاعب) ✓",
        "threshold": -1.78,
        "variables": {
            "dsri": dsri, "gmi": gmi, "aqi": aqi, "sgi": sgi,
            "depi": depi, "sgai": sgai, "tata": tata, "lvgi": lvgi
        }
    }


# --- 3. rNPV (RISK-ADJUSTED NPV FOR BIOTECH & STAGE-GATED ASSETS) ---

def calculate_rnpv(
    investment_m: float,
    cash_flow_annual_m: float,
    years: int = 3,
    probabilities_of_success_pct: Optional[List[float]] = None,
    discount_rate_pct: float = 10.0
) -> Dict[str, Any]:
    """
    rNPV (DiMasi Stage-Gate Methodology):
    rNPV = Sum( (CF_t * PoS_t) / (1 + r)^t ) - Investment
    Plain NPV = Sum( CF_t / (1 + r)^t ) - Investment
    """
    pos = probabilities_of_success_pct or [80.0, 70.0, 60.0]
    r = discount_rate_pct / 100.0
    
    plain_npv = -investment_m
    r_npv = -investment_m
    
    for t in range(1, years + 1):
        d = math.pow(1.0 + r, t)
        p = (pos[min(t - 1, len(pos) - 1)] / 100.0) if pos else 1.0
        plain_npv += cash_flow_annual_m / d
        r_npv += (cash_flow_annual_m * p) / d
        
    sign_flipped = (plain_npv > 0 and r_npv < 0)
    return {
        "investment_m": investment_m,
        "cash_flow_annual_m": cash_flow_annual_m,
        "discount_rate_pct": discount_rate_pct,
        "plain_npv_m": round(plain_npv, 2),
        "rnpv_m": round(r_npv, 2),
        "sign_flipped": sign_flipped,
        "lesson_note": "تغيرت الإشارة من ربح إلى خسارة بعد إدخال احتمالات النجاح — هذا جوهر درس التقييم الدوائي" if sign_flipped else "التقييم إيجابي حتى بعد خصم مخاطر المراحل"
    }


# --- 4. CUT-CUT SYSTEM (POST-CRISIS TRANSIENT GROWTH I/Y) ---

def calculate_cut_cut(
    peak_eps: float,
    current_eps: float,
    years_to_recover: int = 4
) -> Dict[str, Any]:
    """
    Cut-Cut Post-Crisis Formula:
    I/Y = (Peak_EPS / Current_EPS) ^ (1 / N) - 1
    Used when earnings collapse temporarily and are expected to recover to peak.
    """
    if current_eps <= 0 or peak_eps <= 0 or years_to_recover <= 0:
        return {"recovery_growth_pct": None, "error": "EPS values and years must be positive."}
        
    growth = math.pow(peak_eps / current_eps, 1.0 / years_to_recover) - 1.0
    return {
        "peak_eps": peak_eps,
        "current_eps": current_eps,
        "years_to_recover": years_to_recover,
        "recovery_growth_pct": round(growth * 100.0, 2),
        "rule_note": "معدل النمو التعويضي المؤقت يُستخدم كـ GS للمصفوفة ريثما تصدر أرباح 1-2 أرباع للتثبت"
    }


# --- 5. USER-BASED VALUATION (PLATFORMS & APPS) ---

def calculate_user_based_valuation(
    active_users_m: float,
    sar_per_user: Optional[float] = 500.0,
    annual_spend_per_user: Optional[float] = None,
    market_cap_m: Optional[float] = 20000.0
) -> Dict[str, Any]:
    """
    User-Based Valuation (Platform Model):
    Accepted Range: $100 - $200 per active user (≈ 375 - 750 SAR).
    """
    val = (active_users_m * annual_spend_per_user) if annual_spend_per_user else (active_users_m * (sar_per_user or 500.0))
    diff_pct = round(((val / market_cap_m) - 1.0) * 100.0, 1) if market_cap_m and market_cap_m > 0 else None
    
    return {
        "active_users_m": active_users_m,
        "implied_valuation_m": round(val, 2),
        "market_cap_m": market_cap_m,
        "valuation_vs_market_pct": diff_pct,
        "is_undervalued": bool(diff_pct and diff_pct > 0),
        "market_pays_per_user_sar": round(market_cap_m / active_users_m, 2) if active_users_m > 0 and market_cap_m else None
    }


# --- 6. TERRY SMITH ROCE & CAPITAL EFFICIENCY ---

def calculate_terry_smith_roce(
    operating_profit_ebit: float,
    total_assets: float,
    current_liabilities: float
) -> Dict[str, Any]:
    """
    Terry Smith Quality Formula:
    ROCE = EBIT / (Total Assets - Current Liabilities)
    Hurdle: > 32% (Index average is ~18%).
    """
    capital_employed = total_assets - current_liabilities
    if capital_employed <= 0:
        return {"roce_pct": None, "error": "Capital Employed must be positive."}
        
    roce = (operating_profit_ebit / capital_employed) * 100.0
    return {
        "ebit_m": operating_profit_ebit,
        "capital_employed_m": round(capital_employed, 2),
        "roce_pct": round(roce, 2),
        "is_terry_smith_grade": bool(roce >= 32.0),
        "benchmark_note": "ممتاز ومطابق لمعايير تيري سميث (≥32%)" if roce >= 32.0 else ("أعلى من متوسط السوق (≥18%)" if roce >= 18.0 else "كفاءة رأس مال منخفضة")
    }
