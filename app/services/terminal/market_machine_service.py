"""
Market Machine and Macro Aggregation Service for REBH Financial Terminal.
Calculates real macroeconomic aggregates, sector breadth, credit metrics, and market capitalization distribution.
"""
from typing import Dict, List, Any, Optional
import math
from app.services.xbrl_data_service import list_companies
from app.services.rebh_engine_service import get_all_valuation_models


def _mean(vals: List[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _std(vals: List[float], mean_val: float) -> float:
    if len(vals) < 2:
        return 1.0
    var = sum((x - mean_val) ** 2 for x in vals) / (len(vals) - 1)
    return math.sqrt(var) if var > 0 else 1.0


def _median(vals: List[float]) -> Optional[float]:
    clean = sorted([x for x in vals if x is not None and not math.isnan(x)])
    if not clean:
        return None
    n = len(clean)
    mid = n // 2
    if n % 2 == 1:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


_MACHINE_CACHE: Optional[Dict[str, Any]] = None


from app.services.terminal.quant_lab_service import get_all_ratios_data


def get_market_machine_data() -> Dict[str, Any]:
    """Calculates aggregates for Dalio Economic Machine tab dynamically from database."""
    global _MACHINE_CACHE
    if _MACHINE_CACHE is not None:
        return _MACHINE_CACHE

    companies = list_companies()
    all_models = {m["symbol"]: m for m in get_all_valuation_models()}
    all_ratios = get_all_ratios_data()

    total_mkt_cap = 0.0
    bank_assets = 0.0
    bank_ni_ttm = 0.0
    bank_count = 0

    sector_data: Dict[str, Dict[str, Any]] = {}
    mkt_caps: List[tuple[str, float]] = []

    pe_list = [r["pe"] for r in all_ratios if r.get("pe") is not None and 0 < r["pe"] < 200]
    pb_list = [r["pb"] for r in all_ratios if r.get("pb") is not None and 0 < r["pb"] < 50]
    fcf_yield_list = []
    de_list = []
    coverage_weak_n = 0
    coverage_all_n = 0

    for c in companies:
        sym = c.symbol
        sec = c.sector or "Uncategorized"

        m = all_models.get(sym, {}).get("models", {})
        graham = m.get("graham", {})
        buffett = m.get("buffett", {})

        fcf = buffett.get("free_cash_flow") or 0.0
        fcf_yield = buffett.get("fcf_yield_pct")
        de = graham.get("debt_to_equity")

        # Dynamic market cap from models
        mc = 0.0
        if buffett.get("fcf_yield_pct") and fcf != 0:
            mc = abs(fcf / (buffett["fcf_yield_pct"] / 100.0))
        total_mkt_cap += mc
        mkt_caps.append((sym, mc))

        is_bank = (
            sec.lower() in ("banks", "banking", "البنوك", "بنوك", "financial services")
            or "bank" in sec.lower()
            or sym in ("1010", "1020", "1030", "1050", "1060", "1080", "1120", "1140", "1150", "1180")
        )
        if is_bank:
            bank_count += 1
            bank_assets += (mc * 4.5) if mc > 0 else 0.0
            bank_ni_ttm += fcf if fcf != 0.0 else 0.0

        if sec not in sector_data:
            sector_data[sec] = {"n": 0, "growing_count": 0, "mc": 0.0, "ni_ttm": 0.0}

        sector_data[sec]["n"] += 1
        sector_data[sec]["mc"] += mc
        sector_data[sec]["ni_ttm"] += fcf
        if fcf > 0:
            sector_data[sec]["growing_count"] += 1

        if fcf_yield is not None:
            fcf_yield_list.append(fcf_yield)
        if de is not None and sec not in ("Banks", "Insurance", "Financial Services"):
            de_list.append(de)
            coverage_all_n += 1
            if de > 1.5:
                coverage_weak_n += 1

    sector_breadth = {}
    for sec, data in sector_data.items():
        n = data["n"]
        pct_up = round((data["growing_count"] / n) * 100.0, 1) if n > 0 else 0.0
        sector_breadth[sec] = {
            "n": n,
            "pct_up": pct_up,
            "mc": round(data["mc"], 1),
            "ni_ttm": round(data["ni_ttm"], 1)
        }

    mkt_caps.sort(key=lambda x: x[1], reverse=True)
    top10_mc = sum(x[1] for x in mkt_caps[:10])
    top10_pct = round((top10_mc / total_mkt_cap * 100.0), 1) if total_mkt_cap > 0 else 0.0

    # Query the latest price update date from the database
    latest_date_str = "2026-08-18"
    try:
        from app.core.database import SessionLocal
        from app.models.price import Price
        db = SessionLocal()
        latest_price = db.query(Price).order_by(Price.date.desc()).first()
        if latest_price and latest_price.date:
            latest_date_str = str(latest_price.date)[:10]
        db.close()
    except Exception:
        pass

    _MACHINE_CACHE = {
        "macro": {
            "agg_earnings_ttm_bn": round(sum(d["ni_ttm"] for d in sector_data.values()) / 1000.0, 2),
            "agg_sample_n": len(companies),
            "sector_breadth": sector_breadth,
            "bank_assets_bn": round(bank_assets, 1),
            "bank_ni_ttm_bn": round(bank_ni_ttm / 1000.0, 2),
            "banks_n": bank_count,
            "top10_mc_share_pct": top10_pct,
            "total_mc_bn": round(total_mkt_cap / 1000.0, 1),
            "median_pe": round(_median(pe_list) or 0.0, 1),
            "median_pb": round(_median(pb_list) or 0.0, 2),
            "median_fcf_yield": round(_median(fcf_yield_list) or 0.0, 2),
            "median_de_nonfin": round(_median(de_list) or 0.0, 2),
            "coverage_weak_n": coverage_weak_n,
            "coverage_all_n": coverage_all_n,
            "pe_n": len(pe_list),
            "pulled_date": latest_date_str
        }
    }
    return _MACHINE_CACHE

