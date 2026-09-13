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
    Extracts the latest-period value of the requested metric from a raw company dict.
    Returns None if the data is absent or produces a degenerate result.
    """
    is_ = company_data.get("income_statement", {})
    bs   = company_data.get("bs", {})
    cf   = company_data.get("cf", {})

    def last(arr) -> Optional[float]:
        if not arr:
            return None
        v = arr[-1]
        return float(v) if v is not None and v != 0 else None

    try:
        if metric == "nm":
            rev = last(is_.get("rev"))
            net = last(is_.get("net"))
            if rev and net is not None:
                return (net / rev) * 100
        elif metric == "gm":
            rev = last(is_.get("rev"))
            gp  = last(is_.get("gp"))
            if rev and gp is not None:
                return (gp / rev) * 100
        elif metric == "opm":
            rev = last(is_.get("rev"))
            op  = last(is_.get("op"))
            if rev and op is not None:
                return (op / rev) * 100
        elif metric == "roe":
            net = last(is_.get("net"))
            eq  = last(bs.get("total_equity"))
            if eq and eq > 0 and net is not None:
                return (net / eq) * 100
        elif metric == "de":
            sd = last(bs.get("short_debt")) or 0.0
            ld = last(bs.get("long_debt"))  or 0.0
            eq = last(bs.get("total_equity"))
            if eq and eq > 0:
                return (sd + ld) / eq
        elif metric == "current":
            ca = last(bs.get("current_assets"))
            cl = last(bs.get("current_liabilities"))
            if ca and cl and cl > 0:
                return ca / cl
        elif metric == "cfo_ni":
            cfo = last(cf.get("cfo"))
            net = last(is_.get("net"))
            if cfo is not None and net and net != 0:
                return (cfo / net) * 100
        elif metric == "revenue":
            return last(is_.get("rev"))
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
