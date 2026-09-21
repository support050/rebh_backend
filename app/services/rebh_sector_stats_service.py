"""
REBH Sector Statistics Service
Computes sector-level median/percentile metrics from live company data.

Used by:
  - /api/rebh/sector-stats?sector=Materials&metric=nm (Studio comparison)
  - /api/rebh/sector-stats-full?sector=Materials      (full sector profile)

Design principles:
  - Uses only values already stored in XBRL output files — no new data sources.
  - Requires at least 3 companies with valid data before returning a median
    (fewer than 3 is flagged as low-coverage).
  - All ratios are computed from statement arrays, matching exactly the formulas
    used in the company-level engine.
  - Returns null for any metric where fewer than 3 valid data points exist.
"""
from typing import Dict, Any, List, Optional
import statistics
from app.services.xbrl_data_service import list_companies, get_company


# ---------------------------------------------------------------------------
# Supported metrics and how to derive them from a company's financial data
# ---------------------------------------------------------------------------
METRIC_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "nm": {
        "label": "هامش صافي الربح %",
        "formula": "صافي الربح ÷ الإيرادات × 100",
        "unit": "%",
    },
    "gm": {
        "label": "هامش الربح الإجمالي %",
        "formula": "إجمالي الربح ÷ الإيرادات × 100",
        "unit": "%",
    },
    "opm": {
        "label": "هامش التشغيل %",
        "formula": "الربح التشغيلي ÷ الإيرادات × 100",
        "unit": "%",
    },
    "roe": {
        "label": "العائد على حقوق الملكية %",
        "formula": "صافي الربح ÷ حقوق المساهمين × 100",
        "unit": "%",
    },
    "de": {
        "label": "نسبة الدين / الملكية",
        "formula": "(ديون قصيرة + طويلة) ÷ حقوق الملكية",
        "unit": "×",
    },
    "current": {
        "label": "نسبة التداول",
        "formula": "الأصول المتداولة ÷ الالتزامات المتداولة",
        "unit": "×",
    },
    "cfo_ni": {
        "label": "تحويل الأرباح CFO/NI %",
        "formula": "التدفق التشغيلي ÷ صافي الربح × 100",
        "unit": "%",
    },
    "revenue": {
        "label": "الإيرادات (آخر فترة)",
        "formula": "إيرادات القائمة الأخيرة",
        "unit": "M SAR",
    },
}


def _extract_metric(company_data: dict, metric: str) -> Optional[float]:
    """
    Extracts the latest-period value of the requested metric from a company dict.
    Supports both raw XBRL objects (with sections) and unified statements dicts.
    """
    sections = company_data.get("sections", {})
    std_is = sections.get("standardized_income_statement") or sections.get("income_statement")
    std_bs = sections.get("standardized_balance_sheet") or sections.get("balance_sheet")
    std_cf = sections.get("standardized_cash_flow") or sections.get("cash_flow")

    def _parse_items(sec) -> dict:
        if not sec:
            return {}
        # If it's a dict (from model_dump / dict()), access key "items"
        if isinstance(sec, dict):
            raw_items = sec.get("items")
            if isinstance(raw_items, list):
                return {
                    (it.get("label") if isinstance(it, dict) else getattr(it, "label", "")): 
                    (it.get("values") if isinstance(it, dict) else getattr(it, "values", {}))
                    for it in raw_items
                }
            return {}
        # If it's a Pydantic object
        if hasattr(sec, "items") and isinstance(sec.items, list):
            return {
                getattr(it, "label", ""): getattr(it, "values", {})
                for it in sec.items
                if not getattr(it, "is_unmapped", False)
            }
        return {}

    is_items = _parse_items(std_is)
    bs_items = _parse_items(std_bs)
    cf_items = _parse_items(std_cf)

    def get_latest(v_dict: dict) -> Optional[float]:
        if not v_dict or not isinstance(v_dict, dict):
            return None
        valid_vals = [float(v) for v in v_dict.values() if v is not None]
        return valid_vals[-1] if valid_vals else None

    # Fallback to flat arrays if passed from unified payload
    is_ = company_data.get("income_statement", {})
    bs  = company_data.get("bs", {})
    cf  = company_data.get("cf", {})

    def last_arr(arr) -> Optional[float]:
        if not arr: return None
        v = arr[-1]
        return float(v) if v is not None and v != 0 else None

    try:
        rev = get_latest(is_items.get("Revenue / Turnover") or is_items.get("Total revenue") or is_items.get("Special Commission Income") or is_items.get("Revenue")) or last_arr(is_.get("rev"))
        net = get_latest(is_items.get("Net Profit for the Period") or is_items.get("Net Profit Attributable to Shareholders of Parent") or is_items.get("Profit (loss) for the period")) or last_arr(is_.get("net"))
        gp = get_latest(is_items.get("Gross Profit") or is_items.get("إجمالي الربح") or is_items.get("Special commission income, net")) or last_arr(is_.get("gp"))
        op = get_latest(is_items.get("Operating Income") or is_items.get("الربح التشغيلي") or is_items.get("Total operating income")) or last_arr(is_.get("op"))

        eq = get_latest(bs_items.get("Total Equity") or bs_items.get("Total Equity Attributable to Shareholders") or bs_items.get("إجمالي حقوق الملكية")) or last_arr(bs.get("total_equity"))
        ca = get_latest(bs_items.get("Total Current Assets") or bs_items.get("إجمالي الأصول المتداولة")) or last_arr(bs.get("current_assets"))
        cl = get_latest(bs_items.get("Total Current Liabilities") or bs_items.get("إجمالي الالتزامات المتداولة")) or last_arr(bs.get("current_liabilities"))

        st_b = get_latest(bs_items.get("Short-term Borrowings & Debt") or bs_items.get("Short-term Debt & Current Portion of Long-term Debt") or {}) or 0.0
        cp_l = get_latest(bs_items.get("Current Portion of Long-term Debt") or {}) or 0.0
        lt_b = get_latest(bs_items.get("Long-term Borrowings & Debt") or bs_items.get("مرابحات، غير متداولة") or bs_items.get("صكوك وسندات، غير متداولة") or {}) or 0.0
        sd = (st_b + cp_l) if (st_b or cp_l) else (last_arr(bs.get("short_debt")) or 0.0)
        ld = lt_b if lt_b else (last_arr(bs.get("long_debt")) or 0.0)

        cfo = get_latest(cf_items.get("Net Cash from Operating Activities (CFO)") or cf_items.get("Net cash flows from (used in) operations") or {}) or last_arr(cf.get("cfo"))

        if metric == "nm":
            if rev and rev > 0 and net is not None:
                return round((net / rev) * 100, 2)
        elif metric == "gm":
            if rev and rev > 0 and gp is not None:
                return round((gp / rev) * 100, 2)
        elif metric == "opm":
            if rev and rev > 0 and op is not None:
                return round((op / rev) * 100, 2)
        elif metric == "roe":
            if eq and eq > 0 and net is not None:
                return round((net / eq) * 100, 2)
        elif metric == "de":
            if eq and eq > 0:
                return round((sd + ld) / eq, 2)
        elif metric == "current":
            if ca and cl and cl > 0:
                return round(ca / cl, 2)
        elif metric == "cfo_ni":
            if cfo is not None and net and net != 0:
                return round((cfo / net) * 100, 2)
        elif metric == "revenue":
            if rev is not None:
                return round(rev / 1_000_000.0, 1) if rev > 50_000_000 else round(rev, 1)
    except Exception:
        pass
    return None


def _get_sector_companies(sector: str) -> List[dict]:
    """
    Returns a list of raw company data dicts for all companies in the given sector.
    Only loads companies from the local/R2 store; does not call the engine.
    """
    company_list = list_companies()
    results = []
    for item in company_list:
        # Flexible sector match: substring, case-insensitive
        item_sector = (item.sector or "").lower()
        if sector.lower() not in item_sector and item_sector not in sector.lower():
            continue
        raw = get_company(item.symbol)
        if raw is None:
            continue
        # Convert Pydantic model to dict for _extract_metric
        try:
            d = raw.dict() if hasattr(raw, "dict") else dict(raw)
        except Exception:
            d = {}
        if d:
            results.append(d)
    return results


def compute_sector_stats(sector: str, metric: str) -> Dict[str, Any]:
    """
    Returns sector-level statistics for a single metric.

    Response shape:
    {
      "sector": "Materials",
      "metric": "nm",
      "label": "هامش صافي الربح %",
      "unit": "%",
      "formula": "...",
      "n": 12,                    # companies with valid data
      "n_sector": 18,             # total companies found in sector
      "coverage": "high",         # high / low / insufficient
      "median": 14.2,
      "mean": 15.1,
      "p25": 10.3,
      "p75": 19.8,
      "min": 3.1,
      "max": 38.4,
      "companies": [              # per-company breakdown
        {"symbol": "2222", "value": 38.4},
        ...
      ]
    }
    """
    meta = METRIC_DEFINITIONS.get(metric)
    if meta is None:
        return {
            "error": f"Metric '{metric}' not supported. Valid: {list(METRIC_DEFINITIONS.keys())}",
            "sector": sector,
            "metric": metric,
        }

    companies = _get_sector_companies(sector)
    n_sector = len(companies)

    values: List[float] = []
    company_breakdown: List[Dict[str, Any]] = []

    for d in companies:
        sym = (d.get("meta") or {}).get("symbol", "?")
        v = _extract_metric(d, metric)
        company_breakdown.append({"symbol": sym, "value": v})
        if v is not None:
            values.append(v)

    # Sort breakdown by value descending for readability
    company_breakdown.sort(key=lambda x: (x["value"] is not None, x["value"] or 0), reverse=True)

    n = len(values)

    if n < 3:
        return {
            "sector": sector,
            "metric": metric,
            "label": meta["label"],
            "unit": meta["unit"],
            "formula": meta["formula"],
            "n": n,
            "n_sector": n_sector,
            "coverage": "insufficient",
            "coverage_note": f"أقل من 3 شركات ({n}) تمتلك بيانات كافية — لا يمكن حساب وسيط قطاعي موثوق",
            "median": None,
            "mean": None,
            "p25": None,
            "p75": None,
            "min": None,
            "max": None,
            "companies": company_breakdown,
        }

    sorted_vals = sorted(values)
    med = statistics.median(values)
    mean = statistics.mean(values)
    p25 = sorted_vals[max(0, int(n * 0.25) - 1)]
    p75 = sorted_vals[min(n - 1, int(n * 0.75))]

    return {
        "sector": sector,
        "metric": metric,
        "label": meta["label"],
        "unit": meta["unit"],
        "formula": meta["formula"],
        "n": n,
        "n_sector": n_sector,
        "coverage": "high" if n >= 5 else "low",
        "coverage_note": f"{n} شركة من أصل {n_sector} في القطاع تمتلك بيانات كافية",
        "median": round(med, 2),
        "mean": round(mean, 2),
        "p25": round(p25, 2),
        "p75": round(p75, 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "companies": company_breakdown,
    }


def compute_sector_full_profile(sector: str) -> Dict[str, Any]:
    """
    Returns a full sector profile: all supported metrics computed at once.
    Used by the Studio sector comparison panel.
    """
    profile: Dict[str, Any] = {
        "sector": sector,
        "metrics": {},
        "metadata": {
            "supported_metrics": list(METRIC_DEFINITIONS.keys()),
            "note": (
                "الأرقام محسوبة من بيانات XBRL المحلية. "
                "عدد الشركات يتحدث عند كل استيراد جديد للبيانات. "
                "الوسيط القطاعي ليس بديلاً عن بيانات حصة السوق أو تقارير المحللين."
            ),
        },
    }
    for metric in METRIC_DEFINITIONS:
        stats = compute_sector_stats(sector, metric)
        profile["metrics"][metric] = stats
    return profile
