from fastapi import APIRouter, HTTPException, Query

from app.services import xbrl_data_service

router = APIRouter(prefix="/api/financials", tags=["XBRL Financials"])


def find_item(items: list[dict], frags: list[str]) -> dict | None:
    for frag in frags:
        for item in items:
            if not item["is_header"] and frag.lower() in item["label"].lower():
                return item
    return None


def pct_change(curr, prev) -> float | None:
    if curr is None or prev is None or prev == 0:
        return None
    try:
        return round(((float(curr) - float(prev)) / abs(float(prev))) * 100, 2)
    except (TypeError, ValueError):
        return None


KPI_DEFS = {
    "balance_sheet": [
        {"label": "Total Assets", "frags": ["total assets"]},
        {"label": "Total Equity", "frags": ["total equity"]},
        {"label": "Total Liabilities", "frags": ["total liabilities"]},
        {"label": "Loans & Advances", "frags": ["loans,financing", "loans and advances"]},
    ],
    "income_statement": [
        {"label": "Total Op. Income", "frags": ["total operating income", "revenue"]},
        {"label": "Net Profit", "frags": ["profit (loss) for the period", "profit for the period"]},
        {"label": "Operating Expenses", "frags": ["total operating expenses"]},
        {"label": "EPS", "frags": ["total basic earnings"]},
    ],
    "cash_flow": [
        {"label": "Operating CF", "frags": ["net cash from operating", "cash flows from operating"]},
        {"label": "Investing CF", "frags": ["net cash from investing", "cash flows from investing"]},
        {"label": "Financing CF", "frags": ["net cash from financing", "cash flows from financing"]},
        {"label": "Net Change", "frags": ["net change in cash", "increase (decrease) in cash"]},
    ],
}


@router.get("/{symbol}/kpis")
def get_kpis(symbol: str, section: str = Query("income_statement")):
    company = xbrl_data_service.get_company(symbol)
    if not company:
        raise HTTPException(404, detail=f"Company '{symbol}' not found")
    sec = company.sections.get(section)
    if not sec:
        raise HTTPException(404, detail=f"Section '{section}' not found")

    periods = sorted(sec.periods)
    latest, prev = (periods[-1], periods[-2]) if len(periods) >= 2 else (periods[-1], None)
    items_raw = [i.model_dump() for i in sec.items]

    kpis = []
    kpi_key = section.replace("standardized_", "")
    for kdef in KPI_DEFS.get(kpi_key, []):
        item = find_item(items_raw, kdef["frags"])
        curr_val = item["values"].get(latest) if item else None
        prev_val = item["values"].get(prev) if item and prev else None
        kpis.append(
            {
                "label": kdef["label"],
                "value": curr_val,
                "prev_value": prev_val,
                "period": latest,
                "prev_period": prev,
                "change_pct": pct_change(curr_val, prev_val),
            }
        )
    return {"symbol": symbol, "section": section, "kpis": kpis}


@router.get("/{symbol}/chart-data")
def get_chart_data(
    symbol: str,
    section: str = Query("income_statement"),
    metrics: list[str] = Query(default=[]),
    periods: list[str] = Query(default=[]),
):
    company = xbrl_data_service.get_company(symbol)
    if not company:
        raise HTTPException(404, detail=f"Company '{symbol}' not found")
    sec = company.sections.get(section)
    if not sec:
        raise HTTPException(404, detail=f"Section '{section}' not found")

    all_periods = sorted(sec.periods)
    active_periods = [p for p in all_periods if p in periods] if periods else all_periods
    items_raw = [i.model_dump() for i in sec.items]

    datasets = []
    for frag in (metrics or ["total assets", "total equity"]):
        item = find_item(items_raw, [frag])
        if not item:
            continue
        datasets.append(
            {
                "label": item["label"],
                "data": [{"period": p, "value": item["values"].get(p)} for p in active_periods],
            }
        )

    return {
        "symbol": symbol,
        "section": section,
        "periods": active_periods,
        "datasets": datasets,
    }


@router.get("/{symbol}/summary")
def get_summary(symbol: str):
    company = xbrl_data_service.get_company(symbol)
    if not company:
        raise HTTPException(404, detail=f"Company '{symbol}' not found")

    summary = {"meta": company.meta.model_dump(), "highlights": {}}

    for section_key, kpi_list in KPI_DEFS.items():
        sec = company.sections.get(section_key)
        if not sec:
            continue
        periods = sorted(sec.periods)
        latest = periods[-1]
        items_raw = [i.model_dump() for i in sec.items]
        section_summary = {}
        for kdef in kpi_list[:2]:
            item = find_item(items_raw, kdef["frags"])
            if item:
                section_summary[kdef["label"]] = item["values"].get(latest)
        if section_summary:
            summary["highlights"][section_key] = {"period": latest, "values": section_summary}

    return summary
