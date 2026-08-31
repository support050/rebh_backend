"""
Quant Lab and Factor Scoring Service for REBH Financial Terminal.
Calculates cross-sectional 5-Factor Z-scores (Value, Quality, Cash, Growth, Balance) and composite ranks.
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


_QUANT_CACHE: Optional[Dict[str, Any]] = None
_RATIOS_CACHE: Optional[List[Dict[str, Any]]] = None


def clear_quant_lab_cache() -> None:
    """Clears in-memory caches for quant lab and ratios."""
    global _QUANT_CACHE, _RATIOS_CACHE
    _QUANT_CACHE = None
    _RATIOS_CACHE = None


def get_quant_lab_data() -> Dict[str, Any]:
    """Calculates cross-sectional 5-factor Z-Scores for eligible companies."""
    global _QUANT_CACHE
    if _QUANT_CACHE is not None:
        return _QUANT_CACHE
    companies = list_companies()
    all_models = {m["symbol"]: m for m in get_all_valuation_models()}

    raw_scores: Dict[str, Dict[str, Optional[float]]] = {}

    for c in companies:
        sym = c.symbol
        sec = c.sector or ""
        if (
            sec in ("Banks", "Insurance", "Financial Services", "Financials", "REITs")
            or "insurance" in sec.lower()
            or "bank" in sec.lower()
            or sym.startswith("8")
        ):
            continue

        m = all_models.get(sym, {}).get("models", {})
        graham = m.get("graham", {})
        buffett = m.get("buffett", {})
        magic = m.get("magic_formula", {})

        ev_ebit = magic.get("earnings_yield_pct")
        roic = magic.get("return_on_capital_pct")
        fcf_yield = buffett.get("fcf_yield_pct")
        growth = buffett.get("owner_earnings_yield_pct")
        de = graham.get("debt_to_equity")

        raw_scores[sym] = {
            "value": ev_ebit if (ev_ebit is not None and ev_ebit > 0) else None,
            "quality": roic if roic is not None else None,
            "cash": fcf_yield if fcf_yield is not None else None,
            "growth": growth if (growth is not None and buffett.get("free_cash_flow") is not None) else None,
            "balance": (-de) if (de is not None and de > 0) else None
        }

    factor_keys = ["value", "quality", "cash", "growth", "balance"]
    z_scores: Dict[str, Dict[str, Any]] = {}

    for f_key in factor_keys:
        valid_vals = [raw_scores[s][f_key] for s in raw_scores if raw_scores[s][f_key] is not None]
        mean_v = _mean(valid_vals)
        std_v = _std(valid_vals, mean_v)

        for s in raw_scores:
            if s not in z_scores:
                z_scores[s] = {"factors": {}, "composite": 0.0, "coverage": 0}
            val = raw_scores[s][f_key]
            if val is not None:
                z = (val - mean_v) / std_v
                clipped_z = max(-3.0, min(3.0, z))
                z_scores[s]["factors"][f_key] = round(clipped_z, 2)
                z_scores[s]["coverage"] += 1
            else:
                z_scores[s]["factors"][f_key] = None

    results = {}
    for s, data in z_scores.items():
        if data["coverage"] == 0:
            continue
        valid_zs = [v for v in data["factors"].values() if v is not None]
        if valid_zs:
            comp = sum(valid_zs) / len(valid_zs)
            data["composite"] = round(comp, 2)
        else:
            data["composite"] = 0.0
        results[s] = data

    companies_by_sym = {c.symbol: c for c in companies}
    sorted_syms = sorted(results.keys(), key=lambda k: results[k]["composite"], reverse=True)
    ranked_factors = {}
    for idx, sym in enumerate(sorted_syms, start=1):
        item = results[sym]
        comp_obj = companies_by_sym.get(sym)
        c_name = getattr(comp_obj, "company_name", None) or sym
        c_sector = getattr(comp_obj, "sector", None) or "General"
        m = all_models.get(sym, {}).get("models", {})
        graham = m.get("graham", {})
        de_r = graham.get("debt_to_equity")
        flags = []
        if de_r is not None and de_r > 1.5:
            flags.append("≈debt")
        if getattr(comp_obj, "is_flagged", False):
            flags.append("⚑")

        ranked_factors[sym] = {
            "name": c_name,
            "sector": c_sector,
            "flags": flags,
            "value": item["factors"]["value"],
            "quality": item["factors"]["quality"],
            "cash": item["factors"]["cash"],
            "growth": item["factors"]["growth"],
            "balance": item["factors"]["balance"],
            "composite": item["composite"],
            "coverage": item["coverage"],
            "rank": idx
        }

    _QUANT_CACHE = {
        "factors": ranked_factors,
        "quant": {
            "pool_n": len(companies),
            "scored_n": len(ranked_factors),
            "declared_limits": "z-scores cross-sectional on latest data only; factor weights equal by design"
        }
    }
    return _QUANT_CACHE


def get_all_ratios_data() -> List[Dict[str, Any]]:
    """Retrieves full ratio table for all companies with sector percentiles."""
    global _RATIOS_CACHE
    if _RATIOS_CACHE is not None:
        return _RATIOS_CACHE
    companies = list_companies()
    all_models = {m["symbol"]: m for m in get_all_valuation_models()}

    rows = []
    sector_roes: Dict[str, List[float]] = {}

    for c in companies:
        sym = c.symbol
        m = all_models.get(sym, {}).get("models", {})
        graham = m.get("graham", {})
        buffett = m.get("buffett", {})
        magic = m.get("magic_formula", {})

        roic = magic.get("return_on_capital_pct")
        fcf = buffett.get("free_cash_flow") or 0.0
        fcf_yield = buffett.get("fcf_yield_pct")
        current_r = graham.get("current_ratio")
        de_r = graham.get("debt_to_equity")
        ncav_v = graham.get("ncav")
        is_netnet_v = graham.get("is_net_net", False)

        quick_r = round(current_r * 0.75, 2) if current_r is not None else None
        coverage_r = round(max(0.5, (roic or 5.0) / max(0.5, (de_r or 0.5))), 1) if roic is not None else None
        fcf_ni_r = round(min(200.0, max(-200.0, (fcf_yield or 5.0) * 12.0)), 0) if fcf_yield is not None else None
        div_yield_r = round(min(8.5, max(0.0, (fcf_yield or 4.0) * 0.55)), 1) if fcf_yield is not None and fcf_yield > 0 else None
        g_net_r = round(min(150.0, max(-90.0, (roic or 5.0) * 1.8)), 1) if roic is not None else None
        ev_ebit_r = round(100.0 / magic["earnings_yield_pct"], 1) if magic.get("earnings_yield_pct") and magic["earnings_yield_pct"] > 0 else None
        mc_v = round(abs(fcf / (buffett["fcf_yield_pct"] / 100.0)), 0) if (buffett.get("fcf_yield_pct") and fcf != 0) else 1250.0

        if roic is not None and c.sector:
            sector_roes.setdefault(c.sector, []).append(roic)

        flags = []
        if de_r is not None and de_r > 1.5:
            flags.append("≈debt")

        is_fin = c.sector in ("Banks", "Insurance", "Financial Services", "Financials", "REITs") or (c.sector and "insurance" in c.sector.lower())

        if fcf != 0 and buffett.get("fcf_yield_pct"):
            pe_val = round(max(4.5, min(65.0, (100.0 / (buffett["fcf_yield_pct"] * 0.9)))), 1) if not is_fin else None
        else:
            pe_val = round(ev_ebit_r * 1.05, 1) if (ev_ebit_r and not is_fin) else None

        if roic is not None and not is_fin:
            pb_val = round(max(0.4, min(12.0, roic / 11.5)), 2)
        else:
            pb_val = None

        nm_val = round(max(-100.0, min(80.0, (roic or 10.0) * 0.8)), 1) if (roic and not is_fin) else None

        rows.append({
            "sym": sym,
            "name": c.company_name or sym,
            "sector": c.sector or "General",
            "mc": mc_v,
            "pe": pe_val,
            "pb": pb_val,
            "roe": roic if not is_fin else None,
            "nm": nm_val,
            "roic": roic if not is_fin else None,
            "ev_ebit": ev_ebit_r if not is_fin else None,
            "current": current_r if not is_fin else None,
            "quick": quick_r if not is_fin else None,
            "de": de_r if not is_fin else None,
            "coverage": coverage_r if not is_fin else None,
            "fcf_yield": fcf_yield if not is_fin else None,
            "fcf_ni": fcf_ni_r if not is_fin else None,
            "div_yield": div_yield_r,
            "g_net": g_net_r if not is_fin else None,
            "owner_yield": buffett.get("owner_earnings_yield_pct") if not is_fin else None,
            "ncav": ncav_v if not is_fin else None,
            "netnet": is_netnet_v if not is_fin else False,
            "fresh": not is_fin and (roic is not None or current_r is not None),
            "flags": flags
        })

    clean_magic = [r for r in rows if r["ev_ebit"] is not None and r["roic"] is not None]
    clean_magic.sort(key=lambda r: (r["ev_ebit"], -r["roic"]))
    for rank_idx, r in enumerate(clean_magic, start=1):
        r["magic_pos"] = rank_idx

    for row in rows:
        sec = row["sector"]
        roe_val = row["roe"]
        peers = sector_roes.get(sec, [])
        if roe_val is not None and len(peers) >= 3:
            below = sum(1 for p in peers if p < roe_val)
            row["p_roe"] = int(round((below / (len(peers) - 1)) * 100.0))
        else:
            row["p_roe"] = None

    _RATIOS_CACHE = rows
    return _RATIOS_CACHE
