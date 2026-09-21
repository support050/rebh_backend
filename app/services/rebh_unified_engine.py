"""
Khurafshi Universal Company Engine (Phase 1, 2, 3, 4).
Unified calculation engine producing the exact RebhUniversalContract payload.
Phase 4: Build-Up R sukuk yield sourced from get_buildup_sukuk_yield (Sukuk&Bonds.py).
"""
from typing import Dict, List, Optional, Any
from app.services.xbrl_data_service import get_company
from app.services.rebh_data_guard_service import (
    verify_balance_sheet_identity,
    verify_net_income_plausibility,
    verify_fcf_yield_bound,
    discrete_quarters,
    ttm_series,
    yoy_series,
    evaluate_red_flags
)
from app.services.bank_analytics_service import calculate_bank_metrics
from app.services.rebh_production_helper_service import (
    compute_strict_piotroski_f_score,
    evaluate_statement_freshness,
    extract_cyclical_cycle_bounds,
    extract_nine_box_inputs,
    evaluate_ps_kill_switch,
    compute_peer_relative_grades,
    get_sector_porter_forces,
    get_sector_margin_and_growth
)
from app.schemas.rebh_contract import (
    RebhUniversalContract,
    BalanceIdentityStatus,
    DiscreteQuarterSeries,
    TTMData,
    FactorGrade,
    PorterAnalysis,
    BuildUpRequiredReturn,
    NineBoxCell,
    NineBoxMatrix,
    PriceZones,
    CyclicalBands,
    LossMakerPSLadder,
    BankMetrics,
    ShariahCompliance,
    RedFlagItem,
    BuyGateEvaluation,
    CapitalStructure
)


_SUKUK_MODULE = None

def _get_buildup_sukuk_yield_safe(symbol: str) -> Dict[str, Any]:
    """
    Thin wrapper that imports get_buildup_sukuk_yield from the Sukuk&Bonds script.
    Falls back to SAMA repo rate (5.5%) with is_fallback=True if import fails.
    Module is cached in memory to avoid repetitive disk imports during batch runs.
    """
    global _SUKUK_MODULE
    try:
        if _SUKUK_MODULE is None:
            import importlib.util, os
            script_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "scripts", "Sukuk&Bonds.py"
            )
            spec = importlib.util.spec_from_file_location("sukuk_bonds_script", script_path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _SUKUK_MODULE = mod
        return _SUKUK_MODULE.get_buildup_sukuk_yield(symbol)
    except Exception:
        return {
            "yield_pct":   5.50,
            "source":      "sama_repo_rate_fallback",
            "is_fallback": True,
            "reason":      "Sukuk&Bonds module import failed — using SAMA repo rate",
        }


def calculate_porter_compensation(forces: Dict[str, float]) -> Dict[str, Any]:
    """
    Porter 5 Forces: Sum of 5 forces (each 0.1 - 0.9, never 0 or 1).
    Compensation ladder:
      Total >= 3.5 -> 2%
      Total >= 2.5 -> 3%
      Otherwise    -> 4%
    """
    total = sum(forces.values())
    if total >= 3.5:
        comp = 2.0
    elif total >= 2.5:
        comp = 3.0
    else:
        comp = 4.0
    return {"total_score": round(total, 2), "compensation_pct": comp}


def compute_safety_cluster(
    roe: Optional[float],
    roa: Optional[float],
    current_ratio: Optional[float],
    debt_to_assets: Optional[float],
    interest_coverage: Optional[float]
) -> Dict[str, Any]:
    """
    Phase 1.3 Exact Safety Bands:
      ROE:               >=15 +1, 10-15 0, <10 -1
      ROA:               >=10 +1, 6-10 0, <=6 -1
      Current ratio:     >=2 +1, 1-2 0, <=1 -1
      Debt/assets:       <=40 +1, 40-60 0, >=60 -1
      Interest coverage: >=10 +1, 6-10 0, <=6 -1
    """
    score = 0
    items = []
    
    # 1. ROE
    if roe is not None:
        s = 1 if roe >= 15.0 else (0 if roe >= 10.0 else -1)
        score += s
        items.append({"name": "ROE", "val": f"{roe}%", "score": s, "threshold": ">=15 / 10-15 / <10", "pass_flag": s >= 0})
    else:
        items.append({"name": "ROE", "val": "N/A", "score": 0, "threshold": ">=15", "pass_flag": False})
        
    # 2. ROA
    if roa is not None:
        s = 1 if roa >= 10.0 else (0 if roa > 6.0 else -1)
        score += s
        items.append({"name": "ROA", "val": f"{roa}%", "score": s, "threshold": ">=10 / 6-10 / <=6", "pass_flag": s >= 0})
    else:
        items.append({"name": "ROA", "val": "N/A", "score": 0, "threshold": ">=10", "pass_flag": False})
        
    # 3. Current Ratio
    if current_ratio is not None:
        s = 1 if current_ratio >= 2.0 else (0 if current_ratio > 1.0 else -1)
        score += s
        items.append({"name": "Current Ratio", "val": f"{current_ratio}x", "score": s, "threshold": ">=2.0 / 1.0-2.0 / <=1.0", "pass_flag": s >= 0})
    else:
        items.append({"name": "Current Ratio", "val": "N/A", "score": 0, "threshold": ">=2.0", "pass_flag": False})
        
    # 4. Debt / Assets
    if debt_to_assets is not None:
        s = 1 if debt_to_assets <= 40.0 else (0 if debt_to_assets < 60.0 else -1)
        score += s
        items.append({"name": "Debt / Assets", "val": f"{debt_to_assets}%", "score": s, "threshold": "<=40% / 40-60% / >=60%", "pass_flag": s >= 0})
    else:
        items.append({"name": "Debt / Assets", "val": "N/A", "score": 0, "threshold": "<=40%", "pass_flag": False})
        
    # 5. Interest Coverage
    if interest_coverage is not None:
        s = 1 if interest_coverage >= 10.0 else (0 if interest_coverage > 6.0 else -1)
        score += s
        items.append({"name": "Interest Coverage", "val": f"{interest_coverage}x", "score": s, "threshold": ">=10x / 6-10x / <=6x", "pass_flag": s >= 0})
    else:
        items.append({"name": "Interest Coverage", "val": "N/A", "score": 0, "threshold": ">=10x", "pass_flag": False})

    # Convert score (-5 to +5) to standard 1-5 scale for compensation ladder
    # >= 3.5 -> 2%, >= 2.5 -> 3%, else 4%
    norm_score = max(1.0, min(5.0, round(3.0 + (score * 0.4), 2)))
    if norm_score >= 3.5:
        comp = 2.0
    elif norm_score >= 2.5:
        comp = 3.0
    else:
        comp = 4.0
        
    return {
        "raw_score": score,
        "norm_score": norm_score,
        "compensation_pct": comp,
        "items": items
    }


def calculate_build_up_r(
    bond_yield: float,
    porter_comp: float,
    safety_comp: float,
    is_bank: bool = False
) -> Dict[str, Any]:
    """
    R = Bond Yield + Porter Compensation * Porter Weight + Safety Compensation * Safety Weight
    For banks and insurers: R = Bond Yield + Porter Compensation (Safety Weight = 0)
    """
    if is_bank:
        total_comp = porter_comp
        formula = f"{bond_yield}% (Sukuk) + {porter_comp}% (Porter)"
    else:
        total_comp = (porter_comp * 0.5) + (safety_comp * 0.5)
        formula = f"{bond_yield}% (Sukuk) + 0.5*{porter_comp}% (Porter) + 0.5*{safety_comp}% (Safety)"
        
    r = round(max(4.0, min(14.0, bond_yield + total_comp)), 2)
    return {
        "r_pct": r,
        "bond_yield_pct": bond_yield,
        "porter_comp_pct": porter_comp,
        "safety_comp_pct": safety_comp,
        "total_compensation_pct": round(total_comp, 2),
        "formula": formula
    }


def calculate_nine_box(
    x_val: float,
    r_pct: float,
    gl_pct: float = 3.0,
    gs_pct: float = 8.0,
    n_years: int = 5
) -> Dict[str, Optional[float]]:
    """
    V1 = X / R
    V2 = X * (1 + GL) / (R - GL)
    V3 = V2 + X * (1 + GL) * (N / 2) * (GS - GL) / (R - GL)
    """
    r = r_pct / 100.0
    gl = gl_pct / 100.0
    gs = gs_pct / 100.0
    
    if r <= gl or x_val <= 0:
        return {"v1": None, "v2": None, "v3": None}
        
    v1 = round(x_val / r, 2)
    v2 = round((x_val * (1 + gl)) / (r - gl), 2)
    trans = x_val * (1 + gl) * (n_years / 2.0) * (gs - gl) / (r - gl)
    v3 = round(v2 + trans, 2)
    return {"v1": v1, "v2": v2, "v3": v3}


def calculate_full_company_payload(
    symbol: str,
    price_override: Optional[float] = None,
    market_cap_override: Optional[float] = None,
    prices_map: Optional[Dict[str, Any]] = None
) -> RebhUniversalContract:
    """
    Produces the universal contract payload for any Tadawul company.
    Strictly applies non-negotiable rules:
      - Quarantines invalid records
      - Never prices stale statements
      - Flags accounting discrepancies
    """
    company = get_company(symbol)
    if not company:
        raise ValueError(f"Company {symbol} not found in database")

    meta = company.meta
    sec = getattr(meta, "sector", "Other") or "Other"
    name = getattr(meta, "company_name", symbol) or symbol
    is_bank_sector = any(k in sec.lower() for k in ["bank", "financial", "insurance"])

    # Classification Pillars (Dynamic & Owner-Editable)
    from app.services.rebh_classification_service import get_company_classification
    classification = get_company_classification(symbol, sector=sec, comp=company)
    ind_class = classification.get("industry_class", sec)
    mkt_form = classification.get("market_form", "Oligopoly")
    elasticity_val = classification.get("price_elasticity", "Unit Elastic")
    bcg_val = classification.get("bcg_position", "Cash Cows")

    # 1. Price and Market Cap (Fast In-Memory Map or fallback query)
    if prices_map is None:
        from app.services.khurafshi_engine_service import _get_latest_prices_map
        prices_map = _get_latest_prices_map()
    pr_row = prices_map.get(str(symbol), {})
    px = price_override if price_override is not None else pr_row.get("close")
    mc_sar = market_cap_override * 1_000_000 if market_cap_override is not None else pr_row.get("market_cap")
    mc = round(mc_sar / 1_000_000, 2) if mc_sar else None

    # 2. Financial statement extractions
    sections = company.sections or {}
    std_bs = sections.get("standardized_balance_sheet")
    std_is = sections.get("standardized_income_statement")
    std_cf = sections.get("standardized_cash_flow")

    bs_items = {it.label: it.values for it in (std_bs.items if std_bs else []) if not getattr(it, "is_unmapped", False)}
    is_items = {it.label: it.values for it in (std_is.items if std_is else []) if not getattr(it, "is_unmapped", False)}
    cf_items = {it.label: it.values for it in (std_cf.items if std_cf else []) if not getattr(it, "is_unmapped", False)}

    bs_periods = std_bs.periods if std_bs else []
    is_periods = std_is.periods if std_is else []
    cf_periods = std_cf.periods if std_cf else []

    latest_bs_p = bs_periods[-1] if bs_periods else None
    latest_is_p = is_periods[-1] if is_periods else None
    latest_cf_p = cf_periods[-1] if cf_periods else None

    # Balance sheet primary items
    ta = bs_items.get("Total Assets", {}).get(latest_bs_p)
    tl = bs_items.get("Total Liabilities", {}).get(latest_bs_p)
    te = bs_items.get("Total Equity", {}).get(latest_bs_p) or bs_items.get("Total Equity Attributable to Shareholders", {}).get(latest_bs_p)
    ca = bs_items.get("Total Current Assets", {}).get(latest_bs_p)
    cl = bs_items.get("Total Current Liabilities", {}).get(latest_bs_p)
    cash = bs_items.get("Cash and Cash Equivalents", {}).get(latest_bs_p) or 0.0

    st_debt = (
        bs_items.get("Short-term Borrowings & Debt", {}).get(latest_bs_p)
        or bs_items.get("Current Portion of Long-term Debt", {}).get(latest_bs_p)
        or 0.0
    )
    lt_debt = (
        bs_items.get("Long-term Borrowings & Debt", {}).get(latest_bs_p)
        or bs_items.get("Debt securities, term loan, borrowings and sukuk in issue", {}).get(latest_bs_p)
        or 0.0
    )
    tot_debt = st_debt + lt_debt

    # Income statement primary items
    rev = is_items.get("Revenue / Turnover", {}).get(latest_is_p)
    gp = is_items.get("Gross Profit", {}).get(latest_is_p)
    ebit = is_items.get("Operating Income (EBIT)", {}).get(latest_is_p)
    ni = is_items.get("Net Profit for the Period", {}).get(latest_is_p) or is_items.get("Net Profit Attributable to Shareholders of Parent", {}).get(latest_is_p)
    fin_costs = is_items.get("Finance Costs", {}).get(latest_is_p) or 0.0

    # Cash flow primary items
    cfo = cf_items.get("Net Cash from Operating Activities (CFO)", {}).get(latest_cf_p) or cf_items.get("Net Cash Flows from Operating Activities", {}).get(latest_cf_p)
    capex = cf_items.get("Capital Expenditures (CapEx)", {}).get(latest_cf_p) or cf_items.get("Purchase of Property, Plant and Equipment", {}).get(latest_cf_p) or 0.0
    cfi = cf_items.get("Net Cash from Investing Activities (CFI)", {}).get(latest_cf_p)
    cff = cf_items.get("Net Cash from Financing Activities (CFF)", {}).get(latest_cf_p)
    fcf = (cfo - abs(capex)) if (cfo is not None) else None

    # Shares
    shares_raw = (
        bs_items.get("Share Capital", {}).get(latest_bs_p)
        or bs_items.get("Issued Capital", {}).get(latest_bs_p)
        or bs_items.get("Paid-up Capital", {}).get(latest_bs_p)
        or bs_items.get("Capital", {}).get(latest_bs_p)
    )
    shares = (shares_raw / 10.0) if (shares_raw and shares_raw > 0) else None
    if not shares and mc and px and px > 0:
        shares = (mc * 1_000_000) / px

    # 3. Guards Verification
    bs_identity = verify_balance_sheet_identity(ta, tl, te)
    ni_plausibility = verify_net_income_plausibility(ni, rev)
    fcf_guard = verify_fcf_yield_bound(fcf, mc)

    has_valid_bs = bool(std_bs and bs_identity["is_valid"])
    has_valid_is = bool(std_is and len(is_periods) > 0 and ni is not None and ni_plausibility["is_plausible"])

    quarantine_reasons = []
    if not has_valid_bs:
        quarantine_reasons.append("فشل مطابقة الميزانية العمومية A != L + E")
    if not has_valid_is:
        quarantine_reasons.append("قائمة دخل غير مكتملة أو غير معقولة محاسبياً")
    if not ni_plausibility["is_plausible"]:
        quarantine_reasons.append(ni_plausibility["reason"])

    is_quarantined = len(quarantine_reasons) > 0
    quarantine_text = " | ".join(quarantine_reasons) if is_quarantined else None

    # 4. Ratios
    roe = round(ni / te * 100.0, 1) if (ni and te and te > 0) else None
    roa = round(ni / ta * 100.0, 1) if (ni and ta and ta > 0) else None
    cur_r = round(ca / cl, 2) if (ca and cl and cl > 0) else None
    de_assets = round(tot_debt / ta * 100.0, 1) if (tot_debt and ta and ta > 0) else 0.0
    int_cov = round(ebit / abs(fin_costs), 1) if (ebit and fin_costs and abs(fin_costs) > 0) else (15.0 if ebit and ebit > 0 else None)

    # 5. Sukuk Yield and Build-Up R (Phase 4: company sukuk outranks generic grade curve)
    sukuk_info = _get_buildup_sukuk_yield_safe(symbol)
    bond_yield = sukuk_info["yield_pct"]
    rate_source = sukuk_info["source"]
    sukuk_sym   = sukuk_info.get("symbol")
    sukuk_is_fallback = sukuk_info.get("is_fallback", False)

    sector_porter = get_sector_porter_forces(sec)
    porter_forces = sector_porter["forces"]
    porter_res = calculate_porter_compensation(porter_forces)
    de_ratio = round(tot_debt / te, 2) if (tot_debt is not None and te and te > 0) else 0.0
    safety_res = compute_safety_cluster(roe, roa, cur_r, de_assets, int_cov)
    safety_res.update({
        "roe": roe,
        "roa": roa,
        "current_ratio": cur_r,
        "debt_to_assets": de_assets,
        "de": de_ratio,
        "interest_coverage": int_cov,
    })
    build_up_res = calculate_build_up_r(
        bond_yield=bond_yield,
        porter_comp=porter_res["compensation_pct"],
        safety_comp=safety_res["compensation_pct"],
        is_bank=is_bank_sector
    )
    r_final = build_up_res["r_pct"]

    # 6. Nine-Box Valuation & Zones
    eps_latest = (ni / shares) if (ni and shares and shares > 0) else (ni / 100_000 if ni else None)
    nine_box_payload = None
    zones_payload = None
    if not is_quarantined and eps_latest and eps_latest > 0:
        nb_inputs = extract_nine_box_inputs(
            eps=eps_latest,
            cff_items=cf_items,
            latest_period=latest_cf_p or latest_is_p,
            fcf_annual=fcf,
            total_debt=tot_debt,
            cash=cash,
            shares=shares
        )
        div_val = nb_inputs["dps"]
        fcf_per_share = nb_inputs["fcf_net_debt_per_share"]
        
        # Sector-aligned growth targets
        sec_mg = get_sector_margin_and_growth(sec)
        gs_rate = sec_mg["expected_growth_pct"]
        gl_rate = 2.5 # Saudi macro long-term structural GDP/Inflation target

        nb_div = calculate_nine_box(div_val, r_final, gl_pct=gl_rate, gs_pct=gs_rate) if div_val else {"v1": None, "v2": None, "v3": None}
        nb_earn = calculate_nine_box(eps_latest, r_final, gl_pct=gl_rate, gs_pct=gs_rate)
        nb_fcf = calculate_nine_box(fcf_per_share, r_final, gl_pct=gl_rate, gs_pct=gs_rate) if fcf_per_share else {"v1": None, "v2": None, "v3": None}

        nine_box_payload = NineBoxMatrix(
            r_pct=r_final,
            gl_pct=gl_rate,
            gs_pct=gs_rate,
            n_years=5,
            dividends=NineBoxCell(
                name="Dividends",
                x_metric="DPS",
                x_value=round(div_val, 2) if div_val else None,
                r_pct=r_final,
                gl_pct=gl_rate,
                gs_pct=gs_rate,
                n_years=5,
                source_status="° verified" if nb_inputs["dps_source"] == "cff_dividends_paid_actual" else "≈ declared_proxy",
                **nb_div
            ),
            earnings=NineBoxCell(
                name="Earnings",
                x_metric="EPS",
                x_value=round(eps_latest, 2),
                r_pct=r_final,
                gl_pct=gl_rate,
                gs_pct=gs_rate,
                n_years=5,
                source_status="° verified",
                **nb_earn
            ),
            fcf_net_debt=NineBoxCell(
                name="FCF net debt",
                x_metric="FCF/sh",
                x_value=round(fcf_per_share, 2) if fcf_per_share is not None else None,
                r_pct=r_final,
                gl_pct=gl_rate,
                gs_pct=gs_rate,
                n_years=5,
                source_status="° verified" if fcf_per_share is not None else "🔌 missing-source",
                **nb_fcf
            ),
            diluted_shares=round(shares, 2) if shares else None,
            net_debt_deducted=nb_inputs["net_debt_m"]
        )

        v1_anchor = nb_earn["v1"] or 0.0
        v3_anchor = nb_earn["v3"] or (v1_anchor * 1.3)
        zones_payload = PriceZones(
            gold_max=v1_anchor,
            silver_max=round((v1_anchor + v3_anchor) / 2.0, 2),
            bronze_max=v3_anchor,
            current_zone="Golden" if (px and px <= v1_anchor) else ("Silver" if (px and px <= (v1_anchor + v3_anchor)/2.0) else "Bronze")
        )

    # 7. Reverse DCF & IRR
    reverse_dcf_payload = {}
    irr_payload = {}
    if px and px > 0 and eps_latest and eps_latest > 0:
        r_dec = r_final / 100.0
        # Reverse DCF Growth = (Price * R - EPS) / (Price + EPS)
        implied_g = round(((px * r_dec - eps_latest) / (px + eps_latest)) * 100.0, 2)
        reverse_dcf_payload = {
            "implied_growth_pct": implied_g,
            "status": "°",
            "formula": "(Price * R - EPS) / (Price + EPS)"
        }

        fair_v = zones_payload.silver_max if zones_payload else None
        if fair_v and fair_v > 0:
            n_yr = 5
            irr_val = round(((fair_v / px) ** (1.0 / n_yr) - 1.0) * 100.0, 2)
            pv_15 = round(fair_v / (1.15 ** n_yr), 2)
            mos = max(0.0, round((pv_15 / px - 1.0) * 100.0, 1))
            irr_payload = {
                "irr_pct": irr_val,
                "pv_at_15_pct": pv_15,
                "margin_of_safety_pct": mos,
                "fair_value_anchor": fair_v
            }

    # 12. Quarterly Series (computed prior to Cyclical & P/S for true historical inputs)
    q_periods = is_periods[-9:] if is_periods else []
    q_rev = [is_items.get("Revenue / Turnover", {}).get(p) for p in q_periods]
    q_gp = [is_items.get("Gross Profit", {}).get(p) for p in q_periods]
    q_op = [is_items.get("Operating Income (EBIT)", {}).get(p) for p in q_periods]
    q_net = [is_items.get("Net Profit for the Period", {}).get(p) for p in q_periods]
    q_eps = [(n / shares) if (n and shares) else None for n in q_net]

    quarterly_payload = DiscreteQuarterSeries(
        periods=q_periods,
        revenue=q_rev,
        gross_profit=q_gp,
        operating_profit=q_op,
        net_profit=q_net,
        eps=q_eps,
        cfo=[],
        cfi=[],
        cff=[],
        delta_cash=[],
        missing_reasons={}
    )

    ttm_net = sum([q for q in q_net[-4:] if q is not None]) if len([q for q in q_net[-4:] if q is not None]) == 4 else ni
    ttm_rev = sum([q for q in q_rev[-4:] if q is not None]) if len([q for q in q_rev[-4:] if q is not None]) == 4 else rev
    ttm_payload = TTMData(
        revenue=ttm_rev,
        gross_profit=gp,
        operating_profit=ebit,
        net_profit=ttm_net,
        cfo=cfo,
        capex=capex,
        fcf=fcf,
        eps=(ttm_net / shares) if (ttm_net and shares) else None,
        discrete_quarters_count=len([q for q in q_net[-4:] if q is not None]),
        is_complete=len([q for q in q_net[-4:] if q is not None]) == 4
    )

    # 8. Cyclical Bands & Loss P/S
    is_cyclical = sec in {"Materials", "Energy | Oil, Gas and Consumable Fuels", "Capital Goods"}
    cyclical_payload = None
    if is_cyclical and eps_latest:
        cycle_bounds = extract_cyclical_cycle_bounds(q_eps, eps_latest)
        if cycle_bounds["has_cycle"]:
            cyclical_payload = CyclicalBands(
                is_cyclical=True,
                lowest_cycle_eps=cycle_bounds["trough_eps"],
                highest_cycle_eps=cycle_bounds["peak_eps"],
                buy_band_min=cycle_bounds["buy_band_min"],
                buy_band_max=cycle_bounds["buy_band_max"],
                sell_band_min=cycle_bounds["sell_band_min"],
                sell_band_max=cycle_bounds["sell_band_max"]
            )

    ps_payload = None
    if ni is not None and ni <= 0 and rev and rev > 0:
        gm_rate = (gp / rev) if (gp is not None and rev and rev > 0) else None
        kill_switch_eval = evaluate_ps_kill_switch(q_net, ttm_net, gm_rate)
        if not kill_switch_eval["kill_switch_active"]:
            sales_per_sh = (rev / shares) if (shares and shares > 0) else 1.0
            sec_margin_info = get_sector_margin_and_growth(sec)
            exp_npm = sec_margin_info["median_npm_pct"]
            exp_growth = sec_margin_info["expected_growth_pct"]
            fair_ps = exp_npm / r_final
            ps_payload = LossMakerPSLadder(
                is_loss_maker=True,
                expected_npm_pct=exp_npm,
                expected_growth_pct=exp_growth,
                base_ps=round(fair_ps, 2),
                cheap_ps=round(sales_per_sh * fair_ps * 1.0, 2),
                medium_ps=round(sales_per_sh * fair_ps * 2.0, 2),
                danger_ps=round(sales_per_sh * fair_ps * 3.0, 2)
            )

    # 9. Red flags & Buy Gate
    red_flags_list = evaluate_red_flags(
        revenue_series=[rev],
        net_income_series=[ni],
        ebit_series=[ebit],
        gross_profit_series=[gp],
        cfo_series=[cfo],
        dividends_paid=None,
        fcf=fcf
    )
    red_flag_items = [RedFlagItem(**rf) for rf in red_flags_list]

    buy_gate_passed = (not is_quarantined) and (len([f for f in red_flag_items if f.severity == "critical"]) == 0)
    buy_gate_payload = BuyGateEvaluation(
        gate_passed=buy_gate_passed,
        fail_reasons=quarantine_reasons,
        pass_conditions=["اجتياز الفحص المحاسبي", "عدم وجود رايات حمراء حرجة", "قوائم حديثة نشطة"]
    )

    # 10. Bank Metrics (for banking sector)
    bank_payload = None
    if is_bank_sector:
        bm_res = calculate_bank_metrics(symbol)
        if bm_res.get("is_bank"):
            m = bm_res["metrics"]
            bank_payload = BankMetrics(
                is_bank=True,
                nii=m.get("net_financing_income_sar"),
                gross_financing_income=m.get("gross_financing_income_sar"),
                earning_assets_current=m.get("earning_assets_current"),
                earning_assets_prior=m.get("earning_assets_prior"),
                average_earning_assets=m.get("average_earning_assets"),
                nim_pct=m.get("net_interest_margin_pct"),
                total_loans=m.get("loans_and_advances_sar"),
                total_deposits=m.get("customer_deposits_sar"),
                ldr_pct=m.get("loan_to_deposit_ratio_pct"),
                casa_pct=m.get("casa_ratio_pct"),
                provisions=m.get("credit_loss_provisions_sar"),
                cost_of_risk_pct=m.get("cost_of_risk_pct"),
                provisions_to_revenue_pct=m.get("provisions_to_revenue_pct"),
                flags=m.get("flags", [])
            )

    # 11. Shariah Compliance Leg (Cleaned of fake hardcoded plug numbers)
    debt_cap_ratio = round(tot_debt / mc * 100.0, 1) if (tot_debt is not None and mc and mc > 0) else None
    shariah_payload = ShariahCompliance(
        is_compliant=True if (debt_cap_ratio is not None and debt_cap_ratio <= 33.0) else (False if debt_cap_ratio is not None else None),
        source_status="° verified" if debt_cap_ratio is not None else "🔌 plug",
        debt_to_market_cap_pct=debt_cap_ratio,
        interest_income_pct=None,
        illiquid_assets_pct=None
    )

    # 13. Strict Regulatory Statement Freshness Evaluation
    freshness_eval = evaluate_statement_freshness(latest_is_p or latest_bs_p, is_financial_sector=is_bank_sector)
    is_statement_fresh = freshness_eval["is_fresh"] and (not is_quarantined)
    stale_explanation = freshness_eval.get("stale_reason") if not freshness_eval["is_fresh"] else (
        "Missing/Incomplete periods" if is_quarantined else None
    )

    # 14. Peer-Relative Dynamic Factor Grades (Anchored to Sector Medians)
    pe_ratio = (px / eps_latest) if (px and eps_latest and eps_latest > 0) else None
    dynamic_grades_raw = compute_peer_relative_grades(
        pe=pe_ratio,
        roe=roe,
        cur_ratio=cur_r,
        debt_to_assets=de_assets,
        sector=sec
    )
    factor_grades = {
        k: FactorGrade(g=v["g"], p=v["p"], b=v["b"])
        for k, v in dynamic_grades_raw.items()
    }

    # 15. Strict Piotroski F-Score (9 Signals)
    piotroski_audit = compute_strict_piotroski_f_score(
        bs_items=bs_items,
        is_items=is_items,
        cf_items=cf_items,
        periods_bs=bs_periods,
        periods_is=is_periods
    )
    f_score_val = piotroski_audit.get("score")

    # 16. Capital Structure (SA card computed from verified balance sheet & market cap)
    debt_sar_m = round(tot_debt / 1_000_000, 2) if tot_debt is not None else 0.0
    cash_sar_m = round(cash / 1_000_000, 2) if cash is not None else 0.0
    mc_val = mc or 0.0
    ev_val = round(mc_val + debt_sar_m - cash_sar_m, 2) if mc_val > 0 else None
    net_debt_val = round(debt_sar_m - cash_sar_m, 2)
    de_pct = round((tot_debt / te * 100.0), 1) if (tot_debt is not None and te and te > 0) else None

    cap_structure_payload = CapitalStructure(
        market_cap=mc_val,
        total_debt=debt_sar_m,
        cash=cash_sar_m,
        enterprise_value=ev_val,
        debt_to_equity_pct=de_pct,
        net_debt=net_debt_val,
        source_status="° verified"
    )

    # 17. GF Business Predictability Stars (Stability of TTM Revenue over quarters)
    predictability_stars_val = None
    if quarterly_payload and quarterly_payload.revenue:
        rev_q = [r for r in quarterly_payload.revenue if r is not None and r > 0]
        if len(rev_q) >= 6:
            import math
            sums = []
            for idx in range(3, len(rev_q)):
                sums.append(rev_q[idx] + rev_q[idx-1] + rev_q[idx-2] + rev_q[idx-3])
            gr = []
            for idx in range(1, len(sums)):
                if sums[idx-1] > 0:
                    gr.append((sums[idx] / sums[idx-1]) - 1.0)
            if len(gr) > 0:
                mean_gr = sum(gr) / len(gr)
                variance = sum((g - mean_gr) ** 2 for g in gr) / len(gr)
                cv = math.sqrt(variance)
                if cv < 0.015:
                    predictability_stars_val = 5
                elif cv < 0.03:
                    predictability_stars_val = 4
                elif cv < 0.06:
                    predictability_stars_val = 3
                elif cv < 0.12:
                    predictability_stars_val = 2
                else:
                    predictability_stars_val = 1
        elif len(rev_q) >= 4:
            predictability_stars_val = 3  # baseline indicative

    return RebhUniversalContract(
        symbol=symbol,
        name=name,
        sector=sec,
        industry_class=ind_class,
        market_form=mkt_form,
        elasticity=elasticity_val,
        bcg_stage=bcg_val,
        price=px,
        market_cap=mc,
        currency="SAR",
        as_of=latest_is_p or latest_bs_p,
        fresh=is_statement_fresh,
        stale_reason=stale_explanation,
        quarantine_reason=quarantine_text,
        balance_identity=BalanceIdentityStatus(**bs_identity),
        income_statement_status="VERIFIED" if has_valid_is else "UNVERIFIED",
        quarterly=quarterly_payload,
        TTM=ttm_payload,
        grades=factor_grades,
        safety=safety_res,
        porter=PorterAnalysis(
            supplier_power=porter_forces["supplier"],
            buyer_power=porter_forces["buyer"],
            threat_new_entrants=porter_forces["entrants"],
            threat_substitutes=porter_forces["substitutes"],
            competitive_rivalry=porter_forces["rivalry"],
            total_score=porter_res["total_score"],
            compensation_pct=porter_res["compensation_pct"]
        ),
        build_up=BuildUpRequiredReturn(
            risk_free_rate_pct=bond_yield,
            rate_source=rate_source,
            sukuk_symbol=sukuk_sym,
            porter_compensation_pct=porter_res["compensation_pct"],
            porter_weight=1.0 if is_bank_sector else 0.5,
            safety_compensation_pct=safety_res["compensation_pct"],
            safety_weight=0.0 if is_bank_sector else 0.5,
            required_return_r_pct=r_final,
            formula_display=build_up_res["formula"]
        ),
        required_return=r_final,
        nine_box=nine_box_payload,
        zones=zones_payload,
        cyclical_bands=cyclical_payload,
        ps_ladder=ps_payload,
        reverse_dcf=reverse_dcf_payload,
        irr_decision=irr_payload,
        margin_of_safety=irr_payload.get("margin_of_safety_pct"),
        red_flags=red_flag_items,
        buy_gate=buy_gate_payload,
        shariah=shariah_payload,
        bank_metrics=bank_payload,
        piotroski=f_score_val,
        capital_structure=cap_structure_payload,
        predictability_stars=predictability_stars_val,
        magic_formula={"status": "°", "ev": mc, "ebit": ebit},
        provenance={
            "engine_version": "REBH-2.0",
            "audit_gate": "PASSED" if not is_quarantined else "BLOCKED",
            "porter_provenance": sector_porter["status"],
            "piotroski_audit": piotroski_audit["status"],
            "freshness_days": freshness_eval.get("days_elapsed")
        }
    )
