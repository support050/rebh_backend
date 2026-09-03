import sys
sys.stdout.reconfigure(encoding='utf-8')
from app.services.xbrl_data_service import get_company

def analyze_bank(symbol):
    c = get_company(symbol)
    if not c:
        print(f"Company {symbol} not found")
        return
    is_sec = c.sections.get('income_statement')
    bs_sec = c.sections.get('balance_sheet')
    
    is_items = {it.label: it.values for it in is_sec.items} if is_sec else {}
    bs_items = {it.label: it.values for it in bs_sec.items} if bs_sec else {}
    
    is_periods = is_sec.periods if is_sec else []
    bs_periods = bs_sec.periods if bs_sec else []
    
    p_is = is_periods[-1] if is_periods else None
    p_bs = bs_periods[-1] if bs_periods else None
    
    print(f"\n=======================================================")
    print(f"=== Bank {symbol} Analysis (IS: {p_is} | BS: {p_bs}) ===")
    print(f"=======================================================")
    
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
    
    print(f"  • NII (صافي دخل التمويل): {nii:,.0f} SAR" if nii else "  • NII: N/A")
    print(f"  • Gross Financing Income (إجمالي دخل التمويل): {gross_ii:,.0f} SAR" if gross_ii else "  • Gross Financing: N/A")
    print(f"  • Credit Loss Provisions (مخصص خسائر الائتمان): {prov:,.0f} SAR" if prov else "  • Provisions: N/A")
    print(f"  • Total Operating Income (إجمالي الدخل التشغيلي): {toi:,.0f} SAR" if toi else "  • Operating Income: N/A")
    print(f"  • Loans & Advances (محفظة التمويل): {loans:,.0f} SAR" if loans else "  • Loans: N/A")
    print(f"  • Customer Deposits (ودائع العملاء): {deposits:,.0f} SAR" if deposits else "  • Deposits: N/A")
    print(f"  • Total Assets (إجمالي الأصول): {ta:,.0f} SAR" if ta else "  • Assets: N/A")
    
    # Ratios
    ldr = (loans / deposits * 100.0) if (loans and deposits and deposits > 0) else None
    cor = (prov / loans * 100.0) if (prov and loans and loans > 0) else None
    nim_proxy = (nii / ta * 100.0) if (nii and ta and ta > 0) else None
    
    print(f"\n  --- Key Banking Indicators ---")
    if ldr is not None:
        status_ldr = "ممتازة (ضمن النطاق الصحي)" if 75 <= ldr <= 90 else ("مرتفعة" if ldr > 90 else "تحفظية")
        print(f"  [1] Loan-to-Deposit Ratio (LDR - القروض للودائع): {ldr:.2f}%  ({status_ldr})")
    if cor is not None:
        print(f"  [2] Cost of Risk (تكلفة المخاطر الائتمانية): {cor:.3f}%")
    if nim_proxy is not None:
        print(f"  [3] Net Interest Margin Proxy (هامش صافي الفائدة/الأصول): {nim_proxy:.2f}%")

if __name__ == "__main__":
    for sym in ['1120', '1180', '1010', '1050', '1080']:
        analyze_bank(sym)
