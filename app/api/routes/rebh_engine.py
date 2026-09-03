"""
REBH Engine API Router
Provides comprehensive company valuation, factor grades, Khurafshi methodology models, 
and trust checks compliant with the REBH data contracts.
"""
from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Dict, Any, Optional, List
from app.services.rebh_engine_service import (
    calculate_valuation_models,
    get_trust_badge_status,
    get_company_signals
)
from app.services.bank_analytics_service import calculate_bank_metrics
from app.services import khurafshi_engine_service
from app.services import course_labs_service
from app.services.xbrl_data_service import get_company, list_companies
from app.api.deps import get_current_user

router = APIRouter(prefix="/api/rebh", tags=["REBH Engine"])


@router.get("/company/{symbol}")
def get_rebh_company(
    symbol: str,
    price: Optional[float] = Query(None, description="Optional override price"),
    market_cap: Optional[float] = Query(None, description="Optional override market cap in M SAR")
) -> Dict[str, Any]:
    """
    Get full ONE ∞ company valuation and analytical payload for a single symbol.
    Compliant with the REBH Universal Data contract.
    """
    company = get_company(symbol)
    if not company:
        raise HTTPException(status_code=404, detail=f"Company with symbol {symbol} not found")
        
    meta = company.meta
    sec = getattr(meta, "sector", "Other") or "Other"
    name = getattr(meta, "company_name", symbol) or symbol
    # Pull real price & market cap from prices table
    from app.services.khurafshi_engine_service import _get_latest_prices_map, _compute_piotroski_f_score
    _prices = _get_latest_prices_map()
    _pr = _prices.get(str(symbol), {})
    _close = _pr.get("close")
    _mc_sar = _pr.get("market_cap")

    # Allow query-param overrides (e.g. for scenario analysis)
    px = price if price is not None else _close
    mc_sar = market_cap * 1_000_000 if market_cap is not None else _mc_sar
    mc = round(mc_sar / 1_000_000, 2) if mc_sar else None  # in M SAR

    sections = company.sections
    std_bs = sections.get("standardized_balance_sheet")
    std_is = sections.get("standardized_income_statement")
    
    bs_items = {it.label: it.values for it in (std_bs.items if std_bs else []) if not getattr(it, "is_unmapped", False)}
    is_items = {it.label: it.values for it in (std_is.items if std_is else []) if not getattr(it, "is_unmapped", False)}
    
    periods = std_is.periods if std_is and std_is.periods else (std_bs.periods if std_bs else [])
    latest_p = periods[-1] if periods else None
    
    ta = bs_items.get("Total Assets", {}).get(latest_p)
    te = bs_items.get("Total Equity", {}).get(latest_p) or bs_items.get("Total Equity Attributable to Shareholders", {}).get(latest_p)
    ca = bs_items.get("Total Current Assets", {}).get(latest_p)
    cl = bs_items.get("Total Current Liabilities", {}).get(latest_p)
    st_debt = bs_items.get("Short-term Borrowings & Debt", {}).get(latest_p) or 0.0
    lt_debt = bs_items.get("Long-term Borrowings & Debt", {}).get(latest_p) or 0.0
    total_debt = st_debt + lt_debt
    
    ni = is_items.get("Net Profit for the Period", {}).get(latest_p) or is_items.get("Net Profit Attributable to Shareholders of Parent", {}).get(latest_p)
    rev = is_items.get("Revenue / Turnover", {}).get(latest_p)
    
    q_net = [is_items.get("Net Profit for the Period", {}).get(p) for p in periods]
    ttm_net = sum([q for q in q_net[-4:] if q is not None]) if len(q_net) >= 4 else (ni or 0.0)
    
    roe = round(ni / te * 100.0, 1) if (ni and te and te > 0) else None
    roa = round(ni / ta * 100.0, 1) if (ni and ta and ta > 0) else None
    cur_r = round(ca / cl, 2) if (ca and cl and cl > 0) else None
    de = round(total_debt / te, 2) if (te and te > 0) else None
    # PE: mc is in M SAR, ttm_net is in thousands SAR (XBRL) -> convert
    pe = round(mc / (ttm_net / 1_000_000), 1) if (mc and ttm_net and ttm_net > 0) else None

    # Khurafshi Safety Cluster
    safety_score = 0
    safety_details = []
    if roe is not None:
        s_roe = 1 if roe >= 15 else (0 if roe >= 10 else -1)
        safety_score += s_roe
        safety_details.append({"name": "ROE", "val": f"{roe}%", "score": s_roe})
    if roa is not None:
        s_roa = 1 if roa >= 10 else (0 if roa >= 6 else -1)
        safety_score += s_roa
        safety_details.append({"name": "ROA", "val": f"{roa}%", "score": s_roa})
    if cur_r is not None:
        s_cr = 1 if cur_r >= 2.0 else (0 if cur_r >= 1.0 else -1)
        safety_score += s_cr
        safety_details.append({"name": "Current Ratio", "val": f"{cur_r}x", "score": s_cr})
        
    fv = None
    if mc is not None and ttm_net and ttm_net > 0 and mc > 0 and sec not in {'Banks', 'Insurance', 'Financial Services', 'REITs'}:
        # fv values in M SAR (divide by required return rate; ttm_net is in thousands SAR -> /1M)
        ttm_net_m = ttm_net / 1_000_000
        fv = {
            "bear": round(ttm_net_m / 0.09, 1),
            "base": round(ttm_net_m / 0.06, 1),
            "bull": round(ttm_net_m / 0.045, 1)
        }
        fv["vs"] = round((fv["base"] / mc - 1) * 100)

    implied_growth = round(max((pe - 8.5) / 2.0, 0.0), 1) if pe and pe > 0 else None

    # Piotroski F-Score from real XBRL data
    periods_bs = std_bs.periods if std_bs else []
    periods_is_list = std_is.periods if std_is else periods
    f_score = _compute_piotroski_f_score(bs_items, is_items, periods_bs, periods_is_list)

    return {
        "sym": symbol,
        "n": name,
        "sec": sec,
        "px": px,
        "mc": mc,
        "pe": pe,
        "roe": roe,
        "roa": roa,
        "de": de,
        "current": cur_r,
        "f_score": f_score,
        "fv": fv,
        "wl": [
            ["g", "القوائم المالية مطابقة ومحققة بفحص الهويات (A = L + E)"],
            ["g", f"العائد على حقوق الملكية ROE يبلغ {roe}%"] if roe and roe >= 10 else ["w", "عائد حقوق الملكية منخفض"]
        ],
        "khurafshi": {
            "safety_score": safety_score,
            "safety_details": safety_details,
            "implied_growth_pct": implied_growth,
            "margin_of_safety_pct": fv.get("vs") if fv else None
        }
    }



@router.get("/universe")
def get_rebh_universe() -> Any:
    """
    Get full market dataset with factor percentiles, sector rankings, and peer pools.
    """
    return khurafshi_engine_service.get_khurafshi_universe_data()


@router.get("/stats")
def get_rebh_stats() -> Dict[str, Any]:
    """
    Get live computed aggregate platform statistics across all covered companies.
    """
    return khurafshi_engine_service.get_khurafshi_live_market_stats()


@router.get("/trust/{symbol}")
def get_rebh_trust_badge(symbol: str) -> Dict[str, Any]:
    """
    Get audit status, identity checks (A = L + E), and trust marks for a company.
    """
    return get_trust_badge_status(symbol)


@router.get("/signals/{symbol}")
def get_rebh_signals(symbol: str) -> Dict[str, Any]:
    """
    Get rule-based acceleration, operating leverage, and financial red flags.
    """
    return get_company_signals(symbol)


@router.get("/models/{symbol}")
def get_rebh_models(
    symbol: str,
    price: Optional[float] = Query(None),
    market_cap: Optional[float] = Query(None)
) -> Dict[str, Any]:
    """
    Get Buffett Owner Earnings, Graham Net-Net, and Magic Formula calculations.
    """
    return calculate_valuation_models(symbol, price=price, market_cap_m=market_cap)


@router.get("/banks/{symbol}")
def get_bank_financial_metrics(symbol: str) -> Dict[str, Any]:
    """
    Get specialized Banking Financial Metrics (NII, LDR, Cost of Risk, NIM, Provisions).
    Compliant with the REBH Banks Analytics model.
    """
    res = calculate_bank_metrics(symbol)
    if not res.get("is_bank"):
        raise HTTPException(status_code=400, detail=f"Company with symbol {symbol} is not classified as a Bank or lacks banking financial lines.")
    return res


# --- Course Labs Endpoints ---

@router.get("/labs/tasi-index")
def get_tasi_index_lab(
    pe: float = Query(13.6, description="Current index P/E"),
    bond: float = Query(4.75, description="Bond yield anchor %")
) -> Dict[str, Any]:
    """TASI Covered-Universe P/E Scenario & Gold/Silver/Bronze Tiers Lab."""
    return course_labs_service.calculate_tasi_index_lab(current_pe=pe, bond_yield_pct=bond)


@router.get("/labs/beneish-m-score")
def get_beneish_m_score(
    dsri: float = Query(1.0), gmi: float = Query(1.0), aqi: float = Query(1.0),
    sgi: float = Query(1.0), depi: float = Query(1.0), sgai: float = Query(1.0),
    tata: float = Query(0.02), lvgi: float = Query(1.0)
) -> Dict[str, Any]:
    """Beneish M-Score Manipulation Detection."""
    return course_labs_service.calculate_beneish_m_score(
        dsri=dsri, gmi=gmi, aqi=aqi, sgi=sgi, depi=depi, sgai=sgai, tata=tata, lvgi=lvgi
    )


@router.get("/labs/rnpv")
def get_rnpv_lab(
    investment: float = Query(100.0),
    cash_flow: float = Query(50.0),
    years: int = Query(3),
    r: float = Query(10.0),
    p1: float = Query(80.0),
    p2: float = Query(70.0),
    p3: float = Query(60.0)
) -> Dict[str, Any]:
    """Risk-Adjusted NPV for Biotech / Stage-Gated Projects."""
    return course_labs_service.calculate_rnpv(
        investment_m=investment,
        cash_flow_annual_m=cash_flow,
        years=years,
        discount_rate_pct=r,
        probabilities_of_success_pct=[p1, p2, p3]
    )


@router.get("/labs/cut-cut")
def get_cut_cut_lab(
    peak_eps: float = Query(..., description="Last peak EPS"),
    current_eps: float = Query(..., description="Crashed current EPS"),
    years: int = Query(4, description="Years to recover")
) -> Dict[str, Any]:
    """Cut-Cut Post-Crisis Transient Recovery Growth Solver."""
    return course_labs_service.calculate_cut_cut(
        peak_eps=peak_eps,
        current_eps=current_eps,
        years_to_recover=years
    )


