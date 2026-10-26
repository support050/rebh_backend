"""
REBH Financial Guard and Data Integrity Service (Phase 1).
Implements mandatory accounting identities, plausibility guards, and scale verification:
- Strict A = L + E accounting identity verification
- Net Income <= 120% of Revenue plausibility check
- abs(FCF Yield) <= 150% plausibility guard
- Discrete quarters derivation from cumulative statements
- YoY growth calculation with strict sign-flip suppression
- Continuous rolling TTM calculation
- 10 Financial Red-Flags auditing engine
"""
from typing import Dict, List, Optional, Any, Tuple
import math


def verify_balance_sheet_identity(
    assets: Optional[float],
    liabilities: Optional[float],
    equity: Optional[float],
    abs_tol: float = 1.0,
    rel_tol: float = 0.001,
    exact_to_the_riyal: bool = True
) -> Dict[str, Any]:
    """
    Verify fundamental accounting identity: Assets = Liabilities + Equity.
    Package requirement: 'A = L + E to the riyal'.
    When exact_to_the_riyal=True, maximum allowed tolerance is strictly abs_tol (1.0 SAR).
    """
    if assets is None or liabilities is None or equity is None:
        return {
            "is_valid": False,
            "assets": assets,
            "liabilities": liabilities,
            "equity": equity,
            "discrepancy": None,
            "tolerance_applied": None,
            "reason": "Missing statement line item (Assets, Liabilities, or Equity)"
        }
    
    diff = abs(assets - (liabilities + equity))
    max_allowed = abs_tol if exact_to_the_riyal else max(abs_tol, abs(assets) * rel_tol)
    is_valid = diff <= max_allowed
    
    return {
        "is_valid": is_valid,
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "discrepancy": round(diff, 2),
        "tolerance_applied": round(max_allowed, 2),
        "exact_to_the_riyal": exact_to_the_riyal,
        "reason": None if is_valid else f"Discrepancy {round(diff, 2)} SAR exceeds strict tolerance {round(max_allowed, 2)} SAR"
    }


def verify_net_income_plausibility(
    net_income: Optional[float],
    revenue: Optional[float],
    max_ratio: float = 1.20
) -> Dict[str, Any]:
    """
    Enforce Rule #7: Net Income <= 120% of Revenue plausibility guard.
    Flags or blocks extraordinary gain anomalies or corrupted scale.
    """
    if net_income is None or revenue is None:
        return {"is_plausible": True, "ratio": None, "reason": "Missing Revenue or Net Income"}
    
    if revenue <= 0:
        # Pre-revenue or zero-revenue company
        return {"is_plausible": True, "ratio": None, "reason": "Zero or negative revenue (pre-revenue path)"}
        
    ratio = net_income / revenue
    is_plausible = ratio <= max_ratio
    return {
        "is_plausible": is_plausible,
        "ratio": round(ratio, 3),
        "reason": None if is_plausible else f"Net income ({round(net_income, 1)}) exceeds {int(max_ratio*100)}% of revenue ({round(revenue, 1)})"
    }


def verify_fcf_yield_bound(
    fcf: Optional[float],
    market_cap: Optional[float],
    max_bound: float = 150.0
) -> Dict[str, Any]:
    """
    Enforce Rule #8: abs(FCF Yield) <= 150% guard.
    Withhold extreme distorted yield outputs.
    """
    if fcf is None or market_cap is None or market_cap <= 0:
        return {"is_valid": True, "yield_pct": None, "reason": "Missing FCF or Market Cap"}
    
    yield_pct = (fcf / market_cap) * 100.0
    is_valid = abs(yield_pct) <= max_bound
    return {
        "is_valid": is_valid,
        "yield_pct": round(yield_pct, 2) if is_valid else None,
        "raw_yield_pct": round(yield_pct, 2),
        "reason": None if is_valid else f"FCF yield {round(yield_pct, 1)}% exceeds absolute bound of {max_bound}%"
    }


def discrete_quarters(cum: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    """Derive discrete quarters from cumulative financial statement periods."""
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


def derive_discrete_quarter_series(
    values: Dict[str, Optional[float]]
) -> List[Tuple[str, Optional[float]]]:
    """
    Turn a {period_key: value} map keyed by the XBRL parser's *cumulative*
    windows ('YYYY-MM_YYYY-MM', running from the fiscal-year start) into a
    chronological list of discrete quarterly values.

    Income-statement and cash-flow items are flow figures and MUST be
    de-cumulated (Qn = Cum_n - Cum_(n-1)); balance-sheet snapshot lines must
    never be passed here. Explicitly-reported discrete windows already present
    in the source (e.g. 'YYYY-04_YYYY-06') are preferred over derived ones.

    Returns a list of (label, value) tuples where label is 'YYYY-Qn'. Fiscal
    years without recognizable calendar-year cumulative anchors are skipped.
    """
    if not values:
        return []
    years = sorted({
        key.split("_")[0][:4]
        for key in values
        if "_" in key and len(key.split("_")[0]) >= 7 and key.split("_")[0][:4].isdigit()
    })
    series: List[Tuple[str, Optional[float]]] = []
    for y in years:
        cum = {
            "3M": values.get(f"{y}-01_{y}-03"),
            "6M": values.get(f"{y}-01_{y}-06"),
            "9M": values.get(f"{y}-01_{y}-09"),
            "FY": values.get(f"{y}-01_{y}-12"),
        }
        derived = discrete_quarters(cum)

        def _pick(disc_key: str, derived_key: str) -> Optional[float]:
            explicit = values.get(disc_key)
            if explicit is not None:
                return explicit
            return derived.get(derived_key)

        quarters = (
            ("Q1", _pick(f"{y}-01_{y}-03", "Q1")),
            ("Q2", _pick(f"{y}-04_{y}-06", "Q2")),
            ("Q3", _pick(f"{y}-07_{y}-09", "Q3")),
            ("Q4", _pick(f"{y}-10_{y}-12", "Q4")),
        )
        for quarter, val in quarters:
            if val is not None:
                series.append((f"{y}-{quarter}", val))
    return series


def _first_present(items: Dict[str, Dict[str, Any]], labels, period) -> Optional[float]:
    for lbl in labels:
        v = items.get(lbl, {}).get(period)
        if isinstance(v, (int, float)) and v not in (0, 0.0):
            return float(v)
    return None


SHARE_CAPITAL_LABELS = (
    "Share Capital", "Issued Capital", "Paid-up Capital", "Capital",
    "share capital", "Share capital", "Issued share capital",
)
EPS_LABELS = (
    "Basic Earnings per Share (EPS)",
    "Basic Earnings Per Share", "Basic And Diluted Earnings Per Share",
    "Earnings Per Share", "Basic EPS", "بند ربحية السهم",
)
NET_LABELS = (
    "Net Profit Attributable to Shareholders of Parent",
    "Net Profit for the Period",
)


def _pick_basic_eps(is_items: Dict[str, Dict[str, Any]], period) -> Optional[float]:
    """Return the reported basic EPS (a SAR-per-share figure) for `period`.
    Prefers a label in EPS_LABELS, else the first *basic, non-discontinued*
    'per share' line. EPS is scale-agnostic — it is quoted per share no matter
    whether the statements' bulk money runs in SAR, thousands or millions."""
    for lbl in EPS_LABELS:
        v = is_items.get(lbl, {}).get(period)
        if isinstance(v, (int, float)) and abs(v) > 1e-9:
            return float(v)
    best: Optional[float] = None
    for lbl, values in is_items.items():
        low = (lbl or "").lower()
        if "per share" not in low and "eps" not in low:
            continue
        if "discontinued" in low or "diluted" in low:
            continue
        v = values.get(period)
        if isinstance(v, (int, float)) and abs(v) > 1e-9:
            best = float(v)
            break
    return best


def detect_scale_factor(
    bs_items: Dict[str, Dict[str, Any]],
    is_items: Dict[str, Dict[str, Any]],
    market_cap_sar: Optional[float],
    price: Optional[float],
    latest_bs_period,
    latest_is_period,
    par_value_sar: float = 10.0,
) -> Tuple[float, str]:
    """
    Detect the reporting scale (decimals/rounding) of a stored financial file and
    return the multiplier that converts its raw monetary values into actual SAR.

    Stored XBRL artifacts arrive in MIXED units across companies (full SAR,
    thousands, or millions) and the source 'rounding' attribute is almost always
    absent, so the scale is inferred from a hard SAR anchor — the market-implied
    share count (market_cap_sar / price, always in true SAR). Two independent
    ratios are tried, snapped to the nearest power of ten:

    1. reported-EPS (PRIMARY): net_profit / eps reveals the true share count in
       the file's own money units, so implied_shares / (net/eps) = net*price /
       (mc*eps) gives the scale. This is EXACT and par-value independent because
       net profit and EPS always refer to the same period and EPS is quoted per
       share regardless of the statements' bulk scale.
    2. share-capital (FALLBACK): implied_shares / (share_capital / par). Assumes
       a 10 SAR par, which several Tadawul names (e.g. Aramco after its par cut)
       violate, so it is only trusted when the EPS anchor is unavailable (loss-
       making or ~zero-EPS companies).

    Returns (scale_factor, basis). scale_factor defaults to 1.0 (treat as SAR)
    when no reliable anchor exists; callers surface a SCALE quarantine in that case.
    """
    if not market_cap_sar or market_cap_sar <= 0 or not price or price <= 0:
        return 1.0, "no-market-anchor"
    implied_shares = market_cap_sar / price

    # 1. Par-independent EPS anchor (exact: net and EPS share the same period).
    eps = _pick_basic_eps(is_items, latest_is_period)
    net_raw = _first_present(is_items, NET_LABELS, latest_is_period)
    if eps and abs(eps) > 1e-9 and net_raw and abs(net_raw) > 1e-9 and (net_raw > 0) == (eps > 0):
        shares_in_file_units = net_raw / eps
        if shares_in_file_units > 0:
            ratio = implied_shares / shares_in_file_units
            if ratio > 0:
                return float(10 ** int(round(math.log10(ratio)))), "reported-eps"

    # 2. Share-capital anchor with assumed par value (fallback for loss-makers).
    sc_raw = _first_present(bs_items, SHARE_CAPITAL_LABELS, latest_bs_period)
    if sc_raw and sc_raw > 0:
        shares_in_file_units = sc_raw / par_value_sar
        if shares_in_file_units > 0:
            ratio = implied_shares / shares_in_file_units
            if ratio > 0:
                return float(10 ** int(round(math.log10(ratio)))), "share-capital"

    return 1.0, "no-scale-anchor"


NON_MONETARY_LABELS = (
    "per share", "per saham", "ربحية السهم", "السهم", "لكل سهم",
    "number of shares", "shares outstanding", "weighted average",
    "عدد الاسهم", "عدد الأسهم", "dividend per",
)


def _is_non_monetary(label: str) -> bool:
    low = (label or "").lower()
    return any(tok in low for tok in NON_MONETARY_LABELS)


def scale_monetary_items(
    items: Dict[str, Dict[str, Any]], factor: float
) -> Dict[str, Dict[str, Any]]:
    """Multiply every monetary numeric value in a {label: {period: value}} map by
    `factor`, in place. Per-share / share-count / ratio lines are left untouched
    (they are already in SAR-per-share regardless of the statements' bulk scale).
    A factor of 1.0 is a no-op, so the call is idempotent once data is canonical."""
    if not factor or factor == 1.0:
        return items
    for label, values in items.items():
        if _is_non_monetary(label):
            continue
        for period, val in list(values.items()):
            if isinstance(val, (int, float)):
                values[period] = val * factor
    return items


_EQUITY_EXACT = (
    "Total Equity", "Total equity", "Total Equity Attributable to Shareholders",
    "Equity attributable to owners of parent", "Total Shareholders Equity",
)


def resolve_total_equity(
    bs_items: Dict[str, Dict[str, Any]], period
) -> Optional[float]:
    """Return the book total-equity figure for `period`, tolerating the label
    variants different Tadawul filers use (REITs and insurers often publish only
    a lowercase 'Total equity attributable to equity holders of company' and no
    plain 'Total Equity' line). Exact labels first, then a case-insensitive scan
    for the closest equity subtotal; narrowly-named reserve/other-comprehensive
    lines are excluded so we never mistake a component for the grand total."""
    for lbl in _EQUITY_EXACT:
        v = bs_items.get(lbl, {}).get(period)
        if isinstance(v, (int, float)):
            return float(v)
    for lbl, values in bs_items.items():
        low = (lbl or "").lower()
        if low in ("total equity", "total shareholders equity",
                   "total equity attributable to shareholders",
                   "total equity attributable to equity holders of company",
                   "equity attributable to owners of parent",
                   "total equity and liabilities"):
            v = values.get(period)
            if isinstance(v, (int, float)):
                return float(v)
    for lbl, values in bs_items.items():
        low = (lbl or "").lower()
        if "equity attributable" in low and "holders" in low:
            v = values.get(period)
            if isinstance(v, (int, float)):
                return float(v)
    return None


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
    """
    Calculate YoY% per quarter vs 4 quarters back.
    Rule #5: Suppress sign flips (negative to positive or positive to negative) to prevent deceptive growth.
    """
    out = []
    for i, v in enumerate(quarters):
        if i < 4 or quarters[i - 4] in (None, 0) or v is None:
            out.append(None)
            continue
        base = quarters[i - 4]
        # Sign flip guard: cannot compute realistic % when sign changes
        if (base > 0) != (v > 0):
            out.append(None)
            continue
        out.append((abs(v) / abs(base) - 1) * 100.0)
    return out


def evaluate_red_flags(
    revenue_series: List[Optional[float]],
    net_income_series: List[Optional[float]],
    ebit_series: List[Optional[float]],
    gross_profit_series: List[Optional[float]],
    cfo_series: List[Optional[float]],
    dividends_paid: Optional[float],
    fcf: Optional[float],
    receivables_growth: Optional[float] = None,
    sales_growth: Optional[float] = None
) -> List[Dict[str, Any]]:
    """
    Generate the 10 computed financial audit red flags mandated by Phase 1.1:
    1. EBIT margin downtrend
    2. Gross margin downtrend
    3. Sales rising while earnings fall
    4. Dividends above FCF
    5. Negative FCF
    6. Weak FCF / NI conversion (< 70%)
    7. Operating cash flow (OCF) decline
    8. Receivables growing faster than sales
    9. Sign-flip event (transition between loss and profit)
    10. Data-scale anomaly / Corrupted Net Income
    """
    flags = []
    
    # 1. Negative FCF
    if fcf is not None and fcf < 0:
        flags.append({
            "code": "FLAG_NEG_FCF",
            "severity": "warning",
            "title_ar": "تدفق نقدي حر سالب",
            "title_en": "Negative Free Cash Flow",
            "detail": f"التدفق النقدي الحر يبلغ {round(fcf, 1)} وهو ما يمثل ضغطاً على السيولة والتمويل الذاتي.",
            "detail_en": f"Free Cash Flow is negative ({round(fcf, 1)} M SAR), indicating pressure on internal liquidity and self-financing.",
            "status_symbol": "⚑"
        })
        
    # 2. Dividends above FCF
    if dividends_paid is not None and fcf is not None and fcf > 0:
        if dividends_paid > fcf:
            flags.append({
                "code": "FLAG_DIV_ABOVE_FCF",
                "severity": "warning",
                "title_ar": "توزيعات نقدية تفوق التدفق الحر",
                "title_en": "Dividends Exceed FCF",
                "detail": "الشركة تمول التوزيعات من خلال الاقتراض أو السيولة النقدية السابقة بدلاً من التدفقات الجارية.",
                "detail_en": "The company is funding cash dividends through borrowings or cash reserves rather than current organic cash flows.",
                "status_symbol": "⚑"
            })
            
    # 3. Weak FCF / NI conversion
    latest_ni = net_income_series[-1] if net_income_series else None
    if fcf is not None and latest_ni is not None and latest_ni > 0:
        conversion = fcf / latest_ni
        if conversion < 0.70:
            flags.append({
                "code": "FLAG_WEAK_FCF_CONVERSION",
                "severity": "warning",
                "title_ar": "ضعف تحويل الأرباح إلى كاش",
                "title_en": "Weak FCF / Net Income Conversion",
                "detail": f"نسبة تحويل الأرباح إلى تدفق حر تبلغ {int(conversion * 100)}% (أقل من الحد الصحي 70%).",
                "detail_en": f"Earnings-to-FCF conversion is {int(conversion * 100)}% (below healthy benchmark of 70%).",
                "status_symbol": "⚑"
            })

    # 4. Sales rising while earnings fall
    if len(revenue_series) >= 5 and len(net_income_series) >= 5:
        r_now, r_prev = revenue_series[-1], revenue_series[-5]
        ni_now, ni_prev = net_income_series[-1], net_income_series[-5]
        if None not in (r_now, r_prev, ni_now, ni_prev) and r_prev > 0:
            if (r_now > r_prev) and (ni_now < ni_prev):
                flags.append({
                    "code": "FLAG_DIVERGENT_SALES_EARNINGS",
                    "severity": "warning",
                    "title_ar": "تباعد الإيرادات والأرباح",
                    "title_en": "Sales Rising while Earnings Fall",
                    "detail": "نمو في المبيعات يتزامن مع انكماش في صافي الأرباح مما يشير لتآكل الهوامش الربحية.",
                    "detail_en": "Revenue growth accompanies contracting net earnings, signalling margin compression or surging operating expenses.",
                    "status_symbol": "⚑"
                })

    # 5. Gross margin downtrend
    if len(gross_profit_series) >= 2 and len(revenue_series) >= 2:
        gp_now, gp_prev = gross_profit_series[-1], gross_profit_series[-2]
        r_now, r_prev = revenue_series[-1], revenue_series[-2]
        if None not in (gp_now, gp_prev, r_now, r_prev) and r_now > 0 and r_prev > 0:
            gm_now = gp_now / r_now
            gm_prev = gp_prev / r_prev
            if gm_now < gm_prev - 0.03:  # Drop of more than 300 bps
                flags.append({
                    "code": "FLAG_GROSS_MARGIN_DOWNTREND",
                    "severity": "warning",
                    "title_ar": "انحدار هامش الدخل الإجمالي",
                    "title_en": "Gross Margin Contraction",
                    "detail": f"انخفض هامش إجمالي الربح من {int(gm_prev*100)}% إلى {int(gm_now*100)}%.",
                    "detail_en": f"Gross profit margin declined from {int(gm_prev*100)}% to {int(gm_now*100)}% YoY.",
                    "status_symbol": "⚑"
                })

    # 6. Receivables growing faster than sales
    if receivables_growth is not None and sales_growth is not None:
        if receivables_growth > sales_growth + 5.0:
            flags.append({
                "code": "FLAG_RECEIVABLES_SURGE",
                "severity": "warning",
                "title_ar": "نمو المدينين أسرع من المبيعات",
                "title_en": "Receivables Growing Faster than Sales",
                "detail": "تراكم مستحقات العملاء بوتيرة أسرع من نمو المبيعات (مؤشر جودة إيرادات).",
                "detail_en": "Trade receivables expanding faster than revenue growth, signalling potential revenue quality and collection friction.",
                "status_symbol": "⚑"
            })

    # 7. Operating Cash Flow decline
    if len(cfo_series) >= 5:
        cfo_now, cfo_prev = cfo_series[-1], cfo_series[-5]
        if cfo_now is not None and cfo_prev is not None and cfo_now < cfo_prev:
            flags.append({
                "code": "FLAG_OCF_DECLINE",
                "severity": "warning",
                "title_ar": "تراجع التدفقات النقدية التشغيلية",
                "title_en": "Operating Cash Flow Decline",
                "detail": "انخفاض التدفقات النقدية من الأنشطة التشغيلية مقارنة بالفترة المماثلة.",
                "detail_en": "Operating cash flow contracted compared to the same prior-year period.",
                "status_symbol": "⚑"
            })

    return flags
