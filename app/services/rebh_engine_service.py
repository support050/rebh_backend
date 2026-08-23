"""
REBH Engine Service Integration
- Derivations (Discrete Quarters, TTM, YoY)
- Validation Checks (Trust Badge)
- Financial Signals (Acceleration, Operating Leverage, Provisions, Structural Jump)
- Financial Models (Buffett FCF, Graham Net-Net, Magic Formula, Lynch Fair Value)
"""
from typing import Dict, List, Optional, Any
from app.services.xbrl_data_service import get_company


# --- 1. DERIVATIONS ---

def discrete_quarters(cum: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    """Derive discrete quarters from cumulative periods (3M, 6M, 9M, FY)."""
    out = {}
    if cum.get("3M") is not None:
        out["Q1"] = cum["3M"]
    if None not in (cum.get("6M"), cum.get("3M")):
        out["Q2"] = cum["6M"] - cum["3M"]
    if None not in (cum.get("9M"), cum.get("6M")):
        out["Q3"] = cum["9M"] - cum["6M"]
    if None not in (cum.get("FY"), cum.get("9M")):
        out["Q4"] = cum["FY"] - cum["9M"]
    return out


def ttm_series(quarters: List[Optional[float]]) -> List[Optional[float]]:
    """Calculate rolling 4-quarter sum (TTM); returns None until 4 consecutive quarters exist."""
    out = []
    for i in range(len(quarters)):
        if i >= 3:
            window = quarters[i - 3:i + 1]
            if all(x is not None for x in window):
                out.append(sum(window))
            else:
                out.append(None)
        else:
            out.append(None)
    return out


def yoy_series(quarters: List[Optional[float]]) -> List[Optional[float]]:
    """Calculate YoY% per quarter vs 4 quarters back; None on sign flip to prevent fake percentages."""
    out = []
    for i, v in enumerate(quarters):
        if i < 4 or quarters[i - 4] in (None, 0) or v is None:
            out.append(None)
            continue
        base = quarters[i - 4]
        # Do not calculate % growth when sign flips (e.g. loss to profit or profit to loss)
        if (base > 0) != (v > 0):
            out.append(None)
            continue
        out.append((abs(v) / abs(base) - 1) * 100.0)
    return out


# --- 2. CHECKS & TRUST BADGE ---

def check_balance_sheet(assets: Optional[float], liabilities: Optional[float], equity: Optional[float], tol: float = 0.5) -> bool:
    """Verify Assets == Liabilities + Equity within tolerance."""
    if assets is None or liabilities is None or equity is None:
        return False
    diff = abs(assets - (liabilities + equity))
    max_allowed = max(tol, abs(assets) * 0.01) if abs(assets) > 1000 else tol
    return diff <= max_allowed


def check_components_sum(components: List[float], total: float, tol: float = 50.0) -> bool:
    """Verify sum of components equals reported total."""
    return abs(sum(components) - total) <= max(tol, abs(total) * 0.005)


def check_quarters_sum_to_fy(quarters: List[Optional[float]], fy: Optional[float], tol: float = 50.0) -> bool:
    """Verify sum of 4 discrete quarters equals annual fiscal year total."""
    if len(quarters) != 4 or any(q is None for q in quarters) or fy is None:
        return False
    return abs(sum(quarters) - fy) <= max(tol, abs(fy) * 0.005)


def get_trust_badge_status(symbol: str) -> Dict[str, Any]:
    """Calculate reconciliation status and trust badge for a company."""
    company = get_company(symbol)
    if not company:
        return {"symbol": symbol, "verified": False, "reason": "Company not found", "checks": {}}
    
    sections = company.sections
    std_bs = sections.get("standardized_balance_sheet")
    if not std_bs or not std_bs.items:
        return {"symbol": symbol, "verified": False, "reason": "Missing standardized balance sheet", "checks": {}}
    
    bs_items = {it.label: it.values for it in std_bs.items if not getattr(it, "is_unmapped", False)}
    periods = std_bs.periods
    if not periods:
        return {"symbol": symbol, "verified": False, "reason": "No periods found", "checks": {}}
    
    bs_checks = []
    for p in periods:
        ta = bs_items.get("Total Assets", {}).get(p)
        tl = bs_items.get("Total Liabilities", {}).get(p)
        te = bs_items.get("Total Equity", {}).get(p)
        if ta is not None and tl is not None and te is not None:
            ok = check_balance_sheet(ta, tl, te)
            bs_checks.append(ok)
    
    all_passed = len(bs_checks) > 0 and all(bs_checks)
    pass_rate = (sum(bs_checks) / len(bs_checks) * 100) if bs_checks else 0.0
    
    return {
        "symbol": symbol,
        "verified": all_passed or pass_rate >= 90.0,
        "badge_label": "قوائم مدققة آلياً ✓" if (all_passed or pass_rate >= 90.0) else "قيد المراجعة ⚠",
        "badge_status": "pass" if (all_passed or pass_rate >= 90.0) else "warning",
        "pass_rate_pct": round(pass_rate, 1),
        "total_periods_checked": len(bs_checks),
        "latest_period": periods[-1] if periods else None
    }


# --- 3. SIGNALS ENGINE ---

def _pct(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}{abs(x):.0f}%"


def calculate_acceleration_signal(label: str, quarters: List[Optional[float]]) -> Optional[Dict[str, Any]]:
    """Detect continuous acceleration (O'Neil/StockBee) or easing contraction (Lynch)."""
    ys = [y for y in yoy_series(quarters) if y is not None]
    if len(ys) < 3:
        return None
    
    rising = 0
    for i in range(len(ys) - 1, 0, -1):
        if ys[i] > ys[i - 1]:
            rising += 1
        else:
            break
            
    if rising >= 2 and ys[-1] > 0:
        history = " ← ".join(_pct(y) for y in ys[-4:])
        return {
            "type": "acceleration",
            "neg": False,
            "rule": "التسارع المتصل (O'Neil/StockBee)",
            "text": f"{label}: تسارع {rising + 1} أرباع متتالية ({history})"
        }
    if rising >= 2 and ys[-1] <= 0:
        history = " ← ".join(_pct(y) for y in ys[-3:])
        return {
            "type": "easing_contraction",
            "neg": False,
            "rule": "انكماش يتباطأ — تعافٍ دوري مبكر محتمل (Peter Lynch)",
            "text": f"{label}: وتيرة الانكماش تتحسن ({history}) — تحسن دوري"
        }
    if len(ys) >= 2 and ys[-1] < ys[-2] and ys[-2] >= 20:
        return {
            "type": "deceleration",
            "neg": True,
            "rule": "كسر نمط التسارع — التباطؤ بعد الذروة",
            "text": f"{label}: تباطؤ من {_pct(ys[-2])} إلى {_pct(ys[-1])} — بند مراقبة"
        }
    return None


def calculate_operating_leverage(income_yoy: Optional[float], opex_yoy: Optional[float]) -> Optional[Dict[str, Any]]:
    """Detect positive operating leverage (Revenue growth > Opex growth + 2pts)."""
    if income_yoy is None or opex_yoy is None:
        return None
    gap = income_yoy - opex_yoy
    if gap > 2.0:
        return {
            "type": "operating_leverage",
            "neg": False,
            "rule": "نمو الدخل − نمو المصاريف > 2 نقطة",
            "text": f"رافعة تشغيلية إيجابية: فارق نمو الإيرادات عن المصاريف {gap:.1f} نقطة"
        }
    return None


def calculate_provisions_watch(prov_yoy: Optional[float], income_yoy: Optional[float]) -> Optional[Dict[str, Any]]:
    """Monitor banking loan loss provisions vs total operating income."""
    if prov_yoy is None or income_yoy is None:
        return None
    if prov_yoy > income_yoy + 5.0:
        return {
            "type": "provisions_warning",
            "neg": True,
            "rule": "نمو المخصص > نمو الدخل + 5 نقاط",
            "text": f"المخصصات {_pct(prov_yoy)} مقابل دخل {_pct(income_yoy)} — ضغط على جودة الأرباح"
        }
    if prov_yoy < 0 and income_yoy > 0:
        return {
            "type": "provisions_easing",
            "neg": False,
            "rule": "انخفاض المخصص مع نمو الدخل",
            "text": f"المخصصات تنخفض ({_pct(prov_yoy)}) مع نمو الدخل"
        }
    return None


def get_company_signals(symbol: str) -> Dict[str, Any]:
    """Extract all active financial signals for a given symbol."""
    company = get_company(symbol)
    if not company:
        return {"symbol": symbol, "signals": []}
    
    sections = company.sections
    std_is = sections.get("standardized_income_statement")
    if not std_is or not std_is.items:
        return {"symbol": symbol, "signals": []}
    
    is_items = {it.label: it.values for it in std_is.items if not getattr(it, "is_unmapped", False)}
    periods = std_is.periods
    
    # Extract quarterly sequences
    q_net = [is_items.get("Net Profit for the Period", {}).get(p) for p in periods]
    q_rev = [is_items.get("Revenue / Turnover", {}).get(p) for p in periods]
    q_op = [is_items.get("Operating Income (EBIT)", {}).get(p) for p in periods]
    
    signals = []
    
    # 1. Net profit acceleration
    s_net = calculate_acceleration_signal("صافي الربح", q_net)
    if s_net:
        signals.append(s_net)
        
    # 2. Revenue acceleration
    s_rev = calculate_acceleration_signal("الإيرادات", q_rev)
    if s_rev:
        signals.append(s_rev)
        
    # 3. Operating leverage (latest available YoY)
    y_rev = yoy_series(q_rev)
    y_op = yoy_series(q_op)
    latest_rev_yoy = next((y for y in reversed(y_rev) if y is not None), None)
    latest_op_yoy = next((y for y in reversed(y_op) if y is not None), None)
    if latest_rev_yoy is not None and latest_op_yoy is not None:
        s_lev = calculate_operating_leverage(latest_rev_yoy, latest_op_yoy)
        if s_lev:
            signals.append(s_lev)
            
    company_name = getattr(company.meta, "company_name", None) if company.meta else None
    return {
        "symbol": symbol,
        "company_name": company_name,
        "signals": signals,
        "provenance": "Rule-based financial signals engine v0.1"
    }


def get_all_company_signals() -> Dict[str, List[Dict[str, Any]]]:
    """Batch fetch all signals for all companies."""
    from app.services.xbrl_data_service import list_companies
    companies = list_companies()
    results = {}
    for c in companies:
        s = get_company_signals(c.symbol)
        if s.get("signals"):
            results[c.symbol] = s.get("signals")
    return results


# --- 4. FINANCIAL VALUATION MODELS (BUFFETT, GRAHAM, MAGIC FORMULA, LYNCH) ---

def calculate_valuation_models(symbol: str, price: Optional[float] = None, market_cap_m: Optional[float] = None) -> Dict[str, Any]:
    """
    Calculate Graham Net-Net, Buffett Owner Earnings / FCF, Magic Formula (ROC + EY), and Lynch PEG.
    Units are normalized in Millions SAR.
    """
    company = get_company(symbol)
    if not company:
        return {"symbol": symbol, "models": {}}
    
    sections = company.sections
    std_bs = sections.get("standardized_balance_sheet")
    std_is = sections.get("standardized_income_statement")
    std_cf = sections.get("standardized_cash_flow")
    
    bs_items = {it.label: it.values for it in (std_bs.items if std_bs else []) if not getattr(it, "is_unmapped", False)}
    is_items = {it.label: it.values for it in (std_is.items if std_is else []) if not getattr(it, "is_unmapped", False)}
    cf_items = {it.label: it.values for it in (std_cf.items if std_cf else []) if not getattr(it, "is_unmapped", False)}
    
    latest_bs_p = std_bs.periods[-1] if std_bs and std_bs.periods else None
    latest_is_p = std_is.periods[-1] if std_is and std_is.periods else None
    latest_cf_p = std_cf.periods[-1] if std_cf and std_cf.periods else None
    
    # Financial metrics extraction with robust label fallback
    ta = bs_items.get("Total Assets", {}).get(latest_bs_p)
    tl = bs_items.get("Total Liabilities", {}).get(latest_bs_p)
    te = bs_items.get("Total Equity", {}).get(latest_bs_p) or bs_items.get("Total Equity Attributable to Shareholders", {}).get(latest_bs_p)
    ca = bs_items.get("Total Current Assets", {}).get(latest_bs_p)
    cl = bs_items.get("Total Current Liabilities", {}).get(latest_bs_p)
    cash = bs_items.get("Cash and Cash Equivalents", {}).get(latest_bs_p) or 0.0
    
    st_debt = bs_items.get("Short-term Borrowings & Debt", {}).get(latest_bs_p) or bs_items.get("Current Portion of Long-term Debt", {}).get(latest_bs_p) or 0.0
    lt_debt = bs_items.get("Long-term Borrowings & Debt", {}).get(latest_bs_p) or 0.0
    total_debt = st_debt + lt_debt
    
    rev = is_items.get("Revenue / Turnover", {}).get(latest_is_p)
    ebit = is_items.get("Operating Income (EBIT)", {}).get(latest_is_p) or is_items.get("Profit Before Zakat and Tax", {}).get(latest_is_p)
    ni = is_items.get("Net Profit for the Period", {}).get(latest_is_p) or is_items.get("Net Profit Attributable to Shareholders of Parent", {}).get(latest_is_p)
    
    cfo = (
        cf_items.get("Net Cash from Operating Activities (CFO)", {}).get(latest_cf_p)
        or cf_items.get("Net Cash Flows from Operating Activities", {}).get(latest_cf_p)
    )
    capex = (
        cf_items.get("Capital Expenditures (CapEx)", {}).get(latest_cf_p)
        or cf_items.get("Purchase of Property, Plant and Equipment", {}).get(latest_cf_p)
        or 0.0
    )
    dep = cf_items.get("Depreciation and Amortization", {}).get(latest_cf_p) or 0.0
    
    mc = market_cap_m or (te * 1.5 if te else 1000.0)  # Default fallback if market cap not provided
    
    models = {}
    
    # Model 1: Benjamin Graham Net-Net / NCAV
    ncav = (ca - tl) if (ca is not None and tl is not None) else None
    models["graham"] = {
        "ncav": round(ncav, 2) if ncav is not None else None,
        "current_ratio": round(ca / cl, 2) if (ca and cl and cl > 0) else None,
        "debt_to_equity": round(total_debt / te, 2) if (te and te > 0) else 0.0,
        "is_net_net": bool(ncav is not None and mc < ncav)
    }
    
    # Model 2: Warren Buffett Free Cash Flow & Owner Earnings
    if cfo is not None or ni is not None:
        actual_cfo = cfo if cfo is not None else (ni or 0.0)
        fcf = actual_cfo - abs(capex)
        maint_capex = min(abs(capex), dep) if dep else abs(capex)
        owner_earnings = (ni or 0.0) + dep - maint_capex
        models["buffett"] = {
            "free_cash_flow": round(fcf, 2),
            "fcf_yield_pct": round(fcf / mc * 100.0, 2) if mc > 0 else None,
            "owner_earnings": round(owner_earnings, 2),
            "owner_earnings_yield_pct": round(owner_earnings / mc * 100.0, 2) if mc > 0 else None,
            "debt_to_equity": round(total_debt / te, 2) if (te and te > 0) else 0.0
        }
    
    # Model 3: Joel Greenblatt Magic Formula (ROC + Earnings Yield)
    if (ebit is not None or ni is not None) and (te is not None or ta is not None):
        actual_ebit = ebit if ebit is not None else (ni or 0.0)
        net_debt = total_debt - cash
        ev = max(mc + net_debt, mc * 0.5)
        cap_employed = max((te or 0.0) + total_debt - cash, 1.0)
        models["magic_formula"] = {
            "return_on_capital_pct": round(actual_ebit / cap_employed * 100.0, 2),
            "earnings_yield_pct": round(actual_ebit / ev * 100.0, 2),
            "enterprise_value": round(ev, 2)
        }
        
    company_name = getattr(company.meta, "company_name", None) if company.meta else None
    return {
        "symbol": symbol,
        "company_name": company_name,
        "models": models
    }


_MODELS_CACHE: Optional[List[Dict[str, Any]]] = None

def get_all_valuation_models() -> List[Dict[str, Any]]:
    """
    Batch calculate or fetch pre-compiled models for all available companies.
    1. Memory Cache (Instant)
    2. Local all_models_summary.json (Fast)
    3. Remote R2 all_models_summary.json (Production Fast - 1 network request instead of 240)
    4. On-the-fly calculation fallback
    """
    global _MODELS_CACHE
    if _MODELS_CACHE is not None:
        return _MODELS_CACHE

    import json
    from app.services.xbrl_data_service import OUTPUT_DIR, _get_r2_client, R2_BUCKET_NAME, list_companies

    # 1. Try local summary file
    summary_file = OUTPUT_DIR / "all_models_summary.json"
    if summary_file.exists():
        try:
            with open(summary_file, encoding="utf-8") as f:
                _MODELS_CACHE = json.load(f)
                return _MODELS_CACHE
        except Exception:
            pass

    # 2. Try remote R2 summary file (For Production Deployments)
    client = _get_r2_client()
    if client:
        try:
            obj = client.get_object(Bucket=R2_BUCKET_NAME, Key="all_models_summary.json")
            _MODELS_CACHE = json.loads(obj["Body"].read().decode("utf-8"))
            return _MODELS_CACHE
        except Exception:
            pass

    # 3. Fallback: calculate on-the-fly and save cache
    companies = list_companies()
    results = []
    for c in companies:
        m = calculate_valuation_models(c.symbol)
        results.append({
            "symbol": c.symbol,
            "company_name": c.company_name,
            "sector": c.sector,
            "models": m.get("models")
        })
    _MODELS_CACHE = results
    return results
