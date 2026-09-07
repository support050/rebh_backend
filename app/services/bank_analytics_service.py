"""
REBH Bank Financial Analytics Service (Al-Asiri Methodology)
Specialized banking metrics:
- Net Financing / Investment Income (NII)
- Credit Loss Provisions & Provision Watch / Release
- Loans & Advances / Customer Deposits (LDR - Loan to Deposit Ratio)
- Cost of Risk (COR) = Impairment / Loans
- Net Interest Margin (NIM) = NII / Average Interest-Earning Assets
- CASA = (Current Accounts + Savings Accounts) / Total Deposits
- 12 Structured Course Red Flags for Banking
"""
from typing import Dict, Any, Optional, List
from app.services.xbrl_data_service import get_company


def calculate_bank_metrics(symbol: str) -> Dict[str, Any]:
    """
    Extract and compute specialized Banking financial statement metrics compliant with Course:
    1. Net Financing / Investment Income (NII)
    2. Gross Financing / Commission Income
    3. Credit Loss Provisions
    4. Loans & Advances (Net Financing Portfolio)
    5. Customer Deposits
    6. Loan-to-Deposit Ratio (LDR) (>=95% danger threshold)
    7. Cost of Risk (COR) = Provisions / Loans
    8. Net Interest Margin (NIM) = NII / Average Earning Assets
    9. CASA ratio = (Current Accounts + Savings Accounts) / Total Customer Deposits
    10. 12 Structured Course Banking Flags
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
    prev_bs_p = bs_periods[-2] if len(bs_periods) >= 2 else None
    
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
    prev_loans = next((bs_items[l].get(prev_bs_p) for l in loans_labels if l in bs_items and prev_bs_p and bs_items[l].get(prev_bs_p) is not None), None)
    
    # 6. Customer Deposits
    deposits_labels = ["Customer's deposits", 'ودائع العملاء']
    deposits = next((bs_items[l].get(p_bs) for l in deposits_labels if l in bs_items and bs_items[l].get(p_bs) is not None), None)
    
    # 7. Current and Savings Accounts (CASA)
    casa_demand_labels = [
        'Demand deposits', 'Current accounts', 'حسابات جارية تحت الطلب', 'ودائع تحت الطلب'
    ]
    casa_savings_labels = [
        'Savings deposits', 'Savings accounts', 'حسابات وودائع ادخار'
    ]
    demand_dep = next((bs_items[l].get(p_bs) for l in casa_demand_labels if l in bs_items and bs_items[l].get(p_bs) is not None), None)
    savings_dep = next((bs_items[l].get(p_bs) for l in casa_savings_labels if l in bs_items and bs_items[l].get(p_bs) is not None), 0.0)
    
    casa_val = None
    if demand_dep is not None:
        casa_val = demand_dep + (savings_dep or 0.0)
    
    # 8. Total Assets & Earning Assets
    ta_labels = ['Total assets', 'إجمالي الموجودات']
    ta = next((bs_items[l].get(p_bs) for l in ta_labels if l in bs_items and bs_items[l].get(p_bs) is not None), None)
    prev_ta = next((bs_items[l].get(prev_bs_p) for l in ta_labels if l in bs_items and prev_bs_p and bs_items[l].get(prev_bs_p) is not None), None)

    # If no bank-specific items found, not a banking model
    if nii is None and loans is None and deposits is None:
        return {"symbol": symbol, "is_bank": False, "metrics": {}}

    # Interest-earning assets proxy: Loans + Investments / Debt securities (or Total Assets as conservative bound)
    cur_earning_assets = loans if (loans and loans > 0) else ta
    prev_earning_assets = prev_loans if (prev_loans and prev_loans > 0) else prev_ta
    
    if cur_earning_assets and prev_earning_assets:
        avg_earning_assets = (cur_earning_assets + prev_earning_assets) / 2.0
    else:
        avg_earning_assets = cur_earning_assets

    # Ratios
    ldr = round(loans / deposits * 100.0, 2) if (loans and deposits and deposits > 0) else None
    cor = round(prov / loans * 100.0, 3) if (prov and loans and loans > 0) else None
    
    # NIM = NII / Average Interest-Earning Assets
    nim = round(nii / avg_earning_assets * 100.0, 2) if (nii and avg_earning_assets and avg_earning_assets > 0) else None
    
    # CASA% = (Current + Savings) / Total Deposits
    casa_pct = round(casa_val / deposits * 100.0, 2) if (casa_val and deposits and deposits > 0) else None
    
    prov_to_rev = round(prov / toi * 100.0, 2) if (prov and toi and toi > 0) else None

    # LDR evaluation: >= 95% is dangerous liquidity stress
    if ldr:
        if ldr >= 95.0:
            ldr_status = "خطر سيولة (تجاوز سقف 95% — استنفاد السيولة الإقراضية)"
        elif ldr >= 85.0:
            ldr_status = "مرتفع (استغلال كامل للسيولة)"
        elif ldr >= 75.0:
            ldr_status = "صحي ومتوازن"
        else:
            ldr_status = "تحفظي (سيولة فائضة)"
    else:
        ldr_status = "غير متاح"

    # 12 Structured Course Banking Flags
    bank_flags = []
    # 1. LDR Danger
    if ldr and ldr >= 95.0:
        bank_flags.append("⚑ نسبة القروض إلى الودائع تجاوزت الحد الحرج 95% (ضغط سيولة إقراضية)")
    # 2. High Cost of Risk
    if cor and cor > 1.5:
        bank_flags.append("⚑ تكلفة المخاطر COR مرتفعة (>1.5%) — ضغوط على المحفظة الائتمانية")
    # 3. Heavy Provisions Burden
    if prov_to_rev and prov_to_rev > 25.0:
        bank_flags.append("⚑ المخصصات تلتهم أكثر من 25% من إجمالي الدخل التشغيلي")
    # 4. Provision Reversal
    if prov is not None and prov < 0:
        bank_flags.append("° رصد عكس قيد مخصصات (Provision Reversal) دعم أرباح الفترة مؤقتاً")
    # 5. Low CASA
    if casa_pct and casa_pct < 40.0:
        bank_flags.append("⚑ انخفاض نسبة الودائع المجانية CASA (<40%) مما يرفع تكلفة التمويل")
    # 6. Negative NII
    if nii is not None and nii <= 0:
        bank_flags.append("⚑ صافي دخل التمويل والاستثمار سالب")
    # 7. Low NIM (< 2.0%)
    if nim is not None and nim < 2.0:
        bank_flags.append("⚑ هامش الفائدة الصافي NIM منخفض (<2.0%) — ضيق هوامش الإقراض")
    # 8. Unbalanced LDR (< 65%)
    if ldr and ldr < 65.0:
        bank_flags.append("⚑ نسبة توظيف القروض للودائع متدنية (<65%) — سيولة غير مستغلة")
    # 9. Provisions surge
    if prov and toi and (prov / toi) > 0.40:
        bank_flags.append("⚑ المخصصات تقتطع أكثر من 40% من الدخل — تدهور جودة الائتمان")
    # 10. Deposit Contraction
    if deposits and prev_loans and deposits < prev_loans:
        bank_flags.append("⚑ قاعدة الودائع أقل من محفظة القروض")
    # 11. Thin Capital Base
    if ta and loans and (loans / ta) > 0.85:
        bank_flags.append("⚑ تركز أصول المصرف في القروض بنسبة تفوق 85%")
    # 12. State Support Period label
    if p_is and any(yr in str(p_is) for yr in ["2020", "2021", "2022"]):
        bank_flags.append("≈ فترة دعم حكومي للسيولة (2020-2022) — لا تقارن كمعيار نمو اعتيادي")

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
            "earning_assets_current": cur_earning_assets,
            "earning_assets_prior": prev_earning_assets,
            "average_earning_assets": avg_earning_assets,
            "net_interest_margin_pct": nim,
            "loan_to_deposit_ratio_pct": ldr,
            "ldr_status": ldr_status,
            "casa_ratio_pct": casa_pct,
            "cost_of_risk_pct": cor,
            "provisions_to_revenue_pct": prov_to_rev,
            "flags": bank_flags
        }
    }
