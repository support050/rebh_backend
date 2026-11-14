"""
Inspect why F-score is low (<= 2) across 116 companies.
Examine specific test cases: 1010 (Riyad Bank), 2222 (Aramco), 2010 (SABIC).
"""
import sys
from pathlib import Path

backend_path = Path(r"d:\Work\LUMIVST\backend")
sys.path.insert(0, str(backend_path))

from app.services.xbrl_data_service import get_company
from app.services.market_universe_service import _detect_period_months

def inspect_company_fscore(sym: str):
    comp = get_company(sym)
    if not comp or not comp.sections:
        print(f"Company {sym} not found or no sections")
        return

    sec = comp.sections
    std_is = sec.get("standardized_income_statement")
    std_bs = sec.get("standardized_balance_sheet")
    std_cf = sec.get("standardized_cash_flow")

    print(f"\n--- Detailed Audit for [{sym}] {comp.meta.company_name} (Sector: {comp.meta.sector}) ---")
    print(f"IS Periods: {std_is.periods if std_is else 'None'}")
    print(f"BS Periods: {std_bs.periods if std_bs else 'None'}")
    print(f"CF Periods: {std_cf.periods if std_cf else 'None'}")

    if not std_is or not std_bs or len(std_is.periods) < 2 or len(std_bs.periods) < 2:
        print("Not enough periods for F-Score calculation!")
        return

    is_items = {it.label: it.values for it in std_is.items}
    bs_items = {it.label: it.values for it in std_bs.items}
    cf_items = {it.label: it.values for it in std_cf.items} if std_cf else {}

    cur_p = std_is.periods[-1]
    prv_p = std_is.periods[-2]
    cur_b = std_bs.periods[-1]
    prv_b = std_bs.periods[-2]

    print(f"Comparing IS: cur={cur_p}, prv={prv_p}")
    print(f"Comparing BS: cur={cur_b}, prv={prv_b}")

    # Check Cash Flow from Operating Activities
    cfo_in_is = is_items.get("Net Cash from Operating Activities", {}).get(cur_p)
    cfo_in_cf = None
    for k in ["Net Cash from Operating Activities (CFO)", "Net Cash Flows from Operating Activities"]:
        if k in cf_items:
            cfo_in_cf = cf_items[k].get(cur_p) or cf_items[k].get(std_cf.periods[-1] if std_cf and std_cf.periods else "")
            if cfo_in_cf is not None:
                print(f"Found CFO in cash flow section under '{k}': {cfo_in_cf}")
                break

    print(f"CFO looked up in Income Statement: {cfo_in_is}")

    # Check Current Assets / Current Liabilities for Banks
    ca_cur = bs_items.get("Total Current Assets", {}).get(cur_b)
    cl_cur = bs_items.get("Total Current Liabilities", {}).get(cur_b)
    print(f"Current Assets: {ca_cur}, Current Liabilities: {cl_cur}")

    # Check Gross Profit
    gp_cur = is_items.get("Gross Profit", {}).get(cur_p)
    print(f"Gross Profit: {gp_cur}")

    # Check Net Profit
    ni_cur = is_items.get("Net Profit for the Period", {}).get(cur_p)
    ni_prv = is_items.get("Net Profit for the Period", {}).get(prv_p)
    print(f"Net Profit cur: {ni_cur}, prv: {ni_prv}")

    # Check Revenue
    rev_cur = is_items.get("Revenue / Turnover", {}).get(cur_p)
    rev_prv = is_items.get("Revenue / Turnover", {}).get(prv_p)
    print(f"Revenue cur: {rev_cur}, prv: {rev_prv}")

for test_sym in ["1010", "2222", "2010"]:
    inspect_company_fscore(test_sym)
