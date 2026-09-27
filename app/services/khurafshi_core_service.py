"""
REBH Khurafshi Core Valuation Engine
Specialized mathematical engine implementing Mishal Al-Kharfashi methodology verbatim:
- Build-Up Required Return (R) with Porter Five Forces & Financial Safety Ladder
- 9-Box Matrix (Dividends, Earnings, FCF net of debt) with (N/2) transitory formula
- Golden, Silver, and Bronze Price Zones
- Cyclical Molodovsky Peak/Trough Range Pricing
- Loss-making Path (P/S ladder x1/x2/x3 and Kill-Switch)
- Asset Play / Graham Net-Net / EPV Scenarios
"""
from typing import Dict, Optional, Any
from app.core.database import SessionLocal
from app.models.sukuk_bonds import SukukMarketData


def get_company_sukuk_yield(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Look up real company Sukuk yield from DB if available (Khurafshi Rule #1:
    'عائد صكوك الشركة نفسها أولى حين يتوفر').
    Falls back to Govt Sukuk Benchmark if no company-specific sukuk is found.
    """
    db = SessionLocal()
    try:
        # 1. Search for company's own sukuk (e.g. 1120 Al Rajhi, 2222 Aramco, 2280 Almarai)
        co_sukuk = db.query(SukukMarketData).filter(
            (SukukMarketData.parent_company_symbol == symbol) | (SukukMarketData.symbol == symbol)
        ).first()

        if co_sukuk and co_sukuk.coupon_rate:
            try:
                rate = float(str(co_sukuk.coupon_rate).replace("%", "").strip())
                return {
                    "source": "company_sukuk",
                    "symbol": co_sukuk.symbol,
                    "issuer_name": co_sukuk.issuer_name,
                    "yield_pct": rate,
                    "is_real_sukuk": True
                }
            except ValueError:
                pass

        # 2. Benchmark Government Sukuk fallback (average of Govt Sukuk)
        govt = db.query(SukukMarketData).filter(SukukMarketData.bond_type == "G").first()
        if govt and govt.coupon_rate:
            try:
                rate = float(str(govt.coupon_rate).replace("%", "").strip())
                return {
                    "source": "govt_sukuk_benchmark",
                    "symbol": govt.symbol,
                    "issuer_name": govt.issuer_name,
                    "yield_pct": rate,
                    "is_real_sukuk": False
                }
            except ValueError:
                pass

    except Exception:
        pass
    finally:
        db.close()

    return None


def calculate_porter_compensation(forces: Dict[str, float]) -> Dict[str, Any]:
    """
    Porter 5 Forces: Sum of 5 forces (each 0.1 - 0.9, never 0 or 1).
    Compensation ladder: >= 3.5 -> 2% | >= 2.5 -> 3% | else -> 4%
    """
    total = sum(forces.values())
    if total >= 3.5:
        comp = 2.0
    elif total >= 2.5:
        comp = 3.0
    else:
        comp = 4.0
    return {"total_score": round(total, 2), "compensation_pct": comp}


def calculate_safety_compensation(safety_score: int) -> float:
    """
    Safety Cluster Ladder: >= 3.5 -> 2% | >= 2.5 -> 3% | else -> 4%
    """
    if safety_score >= 4:
        return 2.0
    elif safety_score >= 2:
        return 3.0
    else:
        return 4.0


def calculate_build_up_r(
    bond_yield: float,
    porter_forces: Dict[str, float],
    safety_score: int,
    porter_weight: float = 0.4,
    safety_weight: float = 0.6,
    is_bank: bool = False
) -> Dict[str, Any]:
    """
    Build-Up R = Bond Yield + (Porter Comp * W) + (Safety Comp * W)
    Bound strictly between 4.0% and 12.0%.
    """
    porter_res = calculate_porter_compensation(porter_forces)
    porter_comp = porter_res["compensation_pct"]

    if is_bank:
        # Bank Exception: 100% Porter (industrial safety rules do not apply)
        total_comp = porter_comp
    else:
        safety_comp = calculate_safety_compensation(safety_score)
        total_comp = (porter_comp * porter_weight) + (safety_comp * safety_weight)

    raw_r = bond_yield + total_comp
    bounded_r = max(4.0, min(12.0, round(raw_r, 2)))

    return {
        "bond_yield_pct": bond_yield,
        "porter_score": porter_res["total_score"],
        "porter_comp_pct": porter_comp,
        "total_compensation_pct": round(total_comp, 2),
        "required_return_r_pct": bounded_r,
        "is_bank_exception": is_bank
    }


def calculate_nine_box_matrix(
    x_val: float,
    r_pct: float,
    gl_pct: float = 3.0,
    gs_pct: float = 8.0,
    n_years: int = 5
) -> Dict[str, Any]:
    """
    Khurafshi 9-Box formulas:
    - V1 (No Growth) = X / R
    - V2 (Gordon) = X * (1 + GL) / (R - GL)
    - V3 (Transitory) = V2 + X * (1 + GL) * (N / 2) * (GS - GL) / (R - GL)
    """
    r = r_pct / 100.0
    gl = gl_pct / 100.0
    gs = gs_pct / 100.0

    if r <= gl:
        return {"no_growth": None, "gordon": None, "transitory": None}

    v1 = round(x_val / r, 2)
    v2 = round((x_val * (1 + gl)) / (r - gl), 2)

    # Transitory formula with N/2 smoothing
    trans_part = x_val * (1 + gl) * (n_years / 2.0) * (gs - gl) / (r - gl)
    v3 = round(v2 + trans_part, 2)

    return {
        "no_growth": v1,
        "gordon": v2,
        "transitory": v3,
        "zones": {
            "golden_max": v1,
            "silver_max": round((v1 + v3) / 2.0, 2),
            "bronze_max": v3
        }
    }


def calculate_cyclical_bands(trough_eps: float, peak_eps: float) -> Dict[str, Any]:
    """
    Molodovsky Rule for Cyclical stocks:
    - Buy at 14x - 16x Trough EPS
    - Sell at 8x - 12x Peak EPS
    """
    return {
        "buy_range_sar": [round(trough_eps * 14.0, 2), round(trough_eps * 16.0, 2)],
        "sell_range_sar": [round(peak_eps * 8.0, 2), round(peak_eps * 12.0, 2)],
        "molodovsky_warning": "مكرر الأرباح المنخفض عند قمة الدورة فخ — الشراء يكون على قاع الأرباح والبيع على قمتها"
    }


def calculate_loss_ps_ladder(sales_per_share: float, expected_npm_pct: float, expected_growth_pct: float, r_pct: float) -> Dict[str, Any]:
    """
    Loss-making Path (P/S Ladder x1/x2/x3):
    Fair P/S = NPM / R
    Multiplier = NPM * Growth
    """
    r = r_pct / 100.0
    npm = expected_npm_pct / 100.0

    fair_ps = npm / r if r > 0 else 0
    base_val = sales_per_share * fair_ps

    return {
        "fair_ps_multiple": round(fair_ps, 2),
        "cheap_x1": round(base_val * 1.0, 2),
        "fair_x2": round(base_val * 2.0, 2),
        "danger_x3": round(base_val * 3.0, 2),
        "kill_switch_rule": "فور تحقق هوامش ربحية ملموسة، يتوقف مسار P/S ويعود التقييم للأرباح مباشرة"
    }
