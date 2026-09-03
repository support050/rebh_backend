"""
REBH Bank Financial Analytics Service (Al-Asiri Methodology)
Specialized banking metrics:
- Net Financing / Investment Income (NII)
- Credit Loss Provisions & Provision Watch / Release
- Loans & Advances / Customer Deposits (LDR - Loan to Deposit Ratio)
- Cost of Risk (COR) = Impairment / Loans
- Net Interest Margin Proxy (NIM) = NII / Total Assets
- 100% Porter Compensation / Safety Exemption
"""
from typing import Dict, Any, Optional
from app.services.xbrl_data_service import get_company


def calculate_bank_metrics(symbol: str) -> Dict[str, Any]:
    """
    Extract and compute specialized Banking financial statement metrics:
    1. Net Financing / Investment Income (NII)
    2. Gross Financing / Commission Income
    3. Credit Loss Provisions
    4. Loans & Advances (Net Financing Portfolio)
    5. Customer Deposits
    6. Loan-to-Deposit Ratio (LDR)
    7. Cost of Risk (COR) = Provisions / Loans
    8. Net Interest Margin Proxy (NIM) = NII / Total Assets
    """
    company = get_company(symbol)
    if not company:
        return {"symbol": symbol, "is_bank": False, "metrics": {}}

    is_sec = company.sections.get('income_statement')
    bs_sec = company.sections.get('balance_sheet')
    
    is_items = {it.label: it.values for it in (is_sec.items if is_sec else [])}
    bs_items = {it.label: it.values for it in (bs_sec.items if bs_sec else [])}
    
    is_periods = is_sec.periods if is_sec else []
    bs_periods = bs_sec.periods if bs_sec else []
    
    p_is = is_periods[-1] if is_periods else None
    p_bs = bs_periods[-1] if bs_periods else None
    
    if not p_is and not p_bs:
        return {"symbol": symbol, "is_bank": False, "metrics": {}}

    # 1. NII (Net Financing / Investment Income)
    nii_labels = [
        'Special commission income (expense)/ financing and investment income (expense), net',
        'دخل (مصروف) العمولات الخاصة / دخل (مصاريف) التمويل والاستثمارات،صافي'
    ]
    nii = next((is_items[l].get(p_is) for l in nii_labels if l in is_items and is_items[l].get(p_is) is not None), None)
    
    # 2. Gross Financing / Investment Income
    gross_ii_labels = [
        'Special commission income/ gross financing and investment income',
        'دخل العمولات الخاصة / إجمالي دخل التمويل والاستثمارات'
    ]
    gross_ii = next((is_items[l].get(p_is) for l in gross_ii_labels if l in is_items and is_items[l].get(p_is) is not None), None)

    # 3. Credit Loss Provisions
    prov_labels = [
        'Impairment (reversal of impairment) charge for credit losses/ loans, financing and advances',
        'مخصص انخفاض (عكس قيد انخفاض) خسائر ائتمان / قروض وتمويل وسلف'
    ]
    prov = next((is_items[l].get(p_is) for l in prov_labels if l in is_items and is_items[l].get(p_is) is not None), None)
    
    # 4. Total Operating Income
    toi_labels = ['Total operating income', 'إجمالي الدخل التشغيلي']
    toi = next((is_items[l].get(p_is) for l in toi_labels if l in is_items and is_items[l].get(p_is) is not None), None)
    
    # 5. Loans and Advances (Net)
    loans_labels = ['Loans,financing and advances, net', 'قروض وتمويل وسلف، صافي']
    loans = next((bs_items[l].get(p_bs) for l in loans_labels if l in bs_items and bs_items[l].get(p_bs) is not None), None)
    
    # 6. Customer Deposits
    deposits_labels = ["Customer's deposits", 'ودائع العملاء']
    deposits = next((bs_items[l].get(p_bs) for l in deposits_labels if l in bs_items and bs_items[l].get(p_bs) is not None), None)
    
    # 7. Total Assets
    ta_labels = ['Total assets', 'إجمالي الموجودات']
    ta = next((bs_items[l].get(p_bs) for l in ta_labels if l in bs_items and bs_items[l].get(p_bs) is not None), None)
    
    # If no bank-specific items found, not a banking model
    if nii is None and loans is None and deposits is None:
        return {"symbol": symbol, "is_bank": False, "metrics": {}}

    # Ratios
    ldr = round(loans / deposits * 100.0, 2) if (loans and deposits and deposits > 0) else None
    cor = round(prov / loans * 100.0, 3) if (prov and loans and loans > 0) else None
    nim_proxy = round(nii / ta * 100.0, 2) if (nii and ta and ta > 0) else None
    
    ldr_status = "صحي / متوازن" if (ldr and 75 <= ldr <= 90) else ("مرتفع (استغلال كامل للسيولة)" if (ldr and ldr > 90) else "تحفظي")

    return {
        "symbol": symbol,
        "company_name": getattr(company.meta, "company_name", symbol) if company.meta else symbol,
        "sector": getattr(company.meta, "sector", "Banks") if company.meta else "Banks",
        "is_bank": True,
        "period_is": p_is,
        "period_bs": p_bs,
        "metrics": {
            "net_financing_income_sar": nii,
            "gross_financing_income_sar": gross_ii,
            "credit_loss_provisions_sar": prov,
            "total_operating_income_sar": toi,
            "loans_and_advances_sar": loans,
            "customer_deposits_sar": deposits,
            "total_assets_sar": ta,
            "loan_to_deposit_ratio_pct": ldr,
            "ldr_status": ldr_status,
            "cost_of_risk_pct": cor,
            "net_interest_margin_proxy_pct": nim_proxy
        }
    }
