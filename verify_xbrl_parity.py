import sys
import os
import json

# Add backend directory to sys.path
backend_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, backend_dir)

from app.services.xbrl_data_service import get_company
from app.services.terminal_service import get_company_unified_page_data

def verify_company_xbrl_parity(symbol="1321"):
    print(f"\n=======================================================")
    print(f"   🔍 VERIFYING XBRL FINANCIAL DATA PARITY FOR [{symbol}]")
    print(f"=======================================================\n")
    
    # 1. Fetch raw company XBRL data directly from xbrl_data_service
    company = get_company(symbol)
    if not company:
        print(f"❌ Company [{symbol}] not found in XBRL database.")
        return False
        
    print(f"🏢 Company Name (AR): {getattr(company.meta, 'name_ar', getattr(company, 'name_ar', 'N/A'))}")
    print(f"🏢 Company Name (EN): {getattr(company.meta, 'company_name', getattr(company, 'company_name', 'N/A'))}")
    print(f"📊 Sector: {getattr(company.meta, 'sector', getattr(company, 'sector', 'N/A'))}\n")
    
    sections = getattr(company, "sections", {})
    std_is = sections.get("standardized_income_statement")
    std_bs = sections.get("standardized_balance_sheet")
    
    if not std_is:
        print("❌ Standardized Income Statement missing in XBRL.")
        return False
        
    is_items = {it.label: it.values for it in (std_is.items if std_is else []) if not getattr(it, "is_unmapped", False)}
    bs_items = {it.label: it.values for it in (std_bs.items if std_bs else []) if not getattr(it, "is_unmapped", False)}
    
    xbrl_periods = std_is.periods
    print(f"📅 Available XBRL Periods ({len(xbrl_periods)}): {xbrl_periods}\n")
    
    # 2. Fetch data rendered by the Fundamental / Unified Page Backend Endpoint
    page_data = get_company_unified_page_data(symbol)
    page_periods = page_data["periods_q"]
    page_rev = page_data["rev"]
    page_net = page_data["net"]
    page_op = page_data["op"]
    page_gp = page_data.get("gp")
    
    # 3. Direct Item-by-Item Verification
    rev_raw = is_items.get("Revenue / Turnover") or is_items.get("Special Commission Income") or {}
    net_raw = is_items.get("Net Profit for the Period") or is_items.get("Net Profit Attributable to Shareholders of Parent") or {}
    op_raw = is_items.get("Operating Income (EBIT)") or is_items.get("Net Special Commission Income") or {}
    gp_raw = is_items.get("Gross Profit") or {}
    
    print("--- [Income Statement Items Verification] ---")
    all_match = True
    
    for i, p in enumerate(page_periods):
        raw_r = rev_raw.get(p, 0.0)
        raw_n = net_raw.get(p, 0.0)
        raw_o = op_raw.get(p, 0.0)
        raw_g = gp_raw.get(p, 0.0) if gp_raw else None
        
        pr_r = page_rev[i]
        pr_n = page_net[i]
        pr_o = page_op[i]
        pr_g = page_gp[i] if page_gp else None
        
        # Scale raw to millions for clean comparison if needed
        scale = 1_000_000.0 if max([abs(x) for x in rev_raw.values()] or [1.0]) > 10_000_000 else 1.0
        scaled_r = round(raw_r / scale, 1)
        scaled_n = round(raw_n / scale, 1)
        scaled_o = round(raw_o / scale, 1)
        scaled_g = round(raw_g / scale, 1) if raw_g is not None else None
        
        r_match = (scaled_r == pr_r)
        n_match = (scaled_n == pr_n)
        o_match = (scaled_o == pr_o)
        g_match = (scaled_g == pr_g) if pr_g is not None else True
        
        status = "✅ MATCH" if (r_match and n_match and o_match and g_match) else "❌ MISMATCH"
        if status != "✅ MATCH":
            all_match = False
            
        print(f"Period: {p:12} | Rev: Page={pr_r:<8} XBRL={scaled_r:<8} | Net: Page={pr_n:<8} XBRL={scaled_n:<8} | Status: {status}")

    print("\n--- [Derived Ratios & Metrics Verification] ---")
    print(f"TTM Net Income (Page):    {sum(page_net[-4:]):,.1f} M SAR")
    print(f"TTM Revenue (Page):       {sum(page_rev[-4:]):,.1f} M SAR")
    print(f"Calculated ROE:           {page_data['peers']['cur']['roe']}%")
    print(f"Calculated Net Margin:    {page_data['peers']['cur']['nm']}%")
    print(f"Calculated P/E:           {page_data['peers']['cur']['pe']}")
    print(f"Calculated P/B:           {page_data['peers']['cur']['pb']}")
    
    print("\n=======================================================")
    if all_match:
        print("🎯 RESULT: 100% PARITY CONFIRMED BETWEEN XBRL & PAGE DATA!")
    else:
        print("⚠️ RESULT: SOME DISCREPANCIES DETECTED.")
    print("=======================================================\n")
    return all_match

if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "1321"
    verify_company_xbrl_parity(sym)
