from fastapi import APIRouter, HTTPException, Query
from typing import Optional

from app.schemas.xbrl_financials import CompanyFinancials, CompanyListItem
from app.services import xbrl_data_service
from app.services import rebh_engine_service

router = APIRouter(prefix="/api/companies", tags=["XBRL Companies"])


@router.get("", response_model=list[CompanyListItem])
@router.get("/", response_model=list[CompanyListItem])
def list_companies():
    return xbrl_data_service.list_companies()


@router.get("/models/all")
@router.get("/models/all/")
def get_all_valuation_models():
    return rebh_engine_service.get_all_valuation_models()


@router.get("/{symbol}", response_model=CompanyFinancials)
def get_company(symbol: str):
    company = xbrl_data_service.get_company(symbol)
    if not company:
        raise HTTPException(404, detail=f"Company '{symbol}' not found")
    return company


@router.get("/{symbol}/sections")
def get_sections(symbol: str):
    company = xbrl_data_service.get_company(symbol)
    if not company:
        raise HTTPException(404, detail=f"Company '{symbol}' not found")
    return {"symbol": symbol, "sections": list(company.sections.keys())}


@router.get("/{symbol}/sections/{section_key}")
def get_section(symbol: str, section_key: str):
    company = xbrl_data_service.get_company(symbol)
    if not company:
        raise HTTPException(404, detail=f"Company '{symbol}' not found")
    section = company.sections.get(section_key)
    if not section:
        raise HTTPException(
            404,
            detail=f"Section '{section_key}' not found. Available: {list(company.sections.keys())}",
        )
    return {"symbol": symbol, "section": section_key, "meta": company.meta, **section.model_dump()}


@router.get("/{symbol}/signals")
@router.get("/{symbol}/signals/")
def get_signals(symbol: str):
    return rebh_engine_service.get_company_signals(symbol)


@router.get("/{symbol}/trust-badge")
@router.get("/{symbol}/trust-badge/")
def get_trust_badge(symbol: str):
    return rebh_engine_service.get_trust_badge_status(symbol)


@router.get("/{symbol}/derived")
@router.get("/{symbol}/derived/")
def get_derived(symbol: str, metric: str = Query("Net Profit for the Period")):
    company = xbrl_data_service.get_company(symbol)
    if not company:
        raise HTTPException(404, detail=f"Company '{symbol}' not found")
    
    std_is = company.sections.get("standardized_income_statement")
    if not std_is:
        return {"symbol": symbol, "metric": metric, "periods": [], "values": [], "ttm": [], "yoy": []}
    
    periods = std_is.periods
    items_map = {it.label: it.values for it in std_is.items if not getattr(it, "is_unmapped", False)}
    values = [items_map.get(metric, {}).get(p) for p in periods]
    
    return {
        "symbol": symbol,
        "metric": metric,
        "periods": periods,
        "values": values,
        "ttm": rebh_engine_service.ttm_series(values),
        "yoy": rebh_engine_service.yoy_series(values)
    }


@router.get("/{symbol}/models")
@router.get("/{symbol}/models/")
def get_valuation_models(symbol: str, market_cap: Optional[float] = None):
    return rebh_engine_service.calculate_valuation_models(symbol, market_cap_m=market_cap)

