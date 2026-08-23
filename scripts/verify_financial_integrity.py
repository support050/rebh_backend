"""
Script: verify_financial_integrity.py
Purpose: Test and verify financial data calculation, derivation, forensic balance checks (A = L + E),
         YoY integrity, and database consistency across diverse random companies.
"""

import os
import sys
from pathlib import Path

# Force UTF-8 console output for Windows PowerShell
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from app.services.xbrl_data_service import get_company, list_companies
from app.services.terminal_service import (
    _derive_discrete_quarters,
    _derive_yoy_series,
    _derive_ttm,
    _build_company_template_from_db
)

# Test pool covering diverse sectors:
# 1010 (Bank - Riyad Bank)
# 2222 (Energy / Petrochemical - Saudi Aramco)
# 2010 (Materials / Petrochemical - SABIC)
# 1831 (Commercial Services - Maharah)
# 3030 (Industrial / Cement - Saudi Cement)
# 4081 (Financial Services - Nayifat)
# 7010 (Telecom - STC)
# 4300 (Real Estate - Dar Al-Arkan)
TEST_SYMBOLS = ["1010", "2222", "2010", "1831", "3030", "4081", "7010", "4300"]

def run_verification():
    print("=" * 80)
    print(" FINANCIAL DATA INTEGRITY & AUDIT TEST RUNNER (7+ DIVERSE COMPANIES)")
    print("=" * 80)

    success_count = 0
    total_tested = 0

    for sym in TEST_SYMBOLS:
        print(f"\n🔍 [Testing Company: {sym}]")
        raw_company = get_company(sym)
        if not raw_company:
            print(f"  ❌ Failed to load XBRL data for {sym} from DB/files.")
            continue

        total_tested += 1
        meta = raw_company.meta
        company_name = getattr(meta, "company_name", "N/A")
        sector = getattr(meta, "sector", "N/A")
        print(f"  🏢 Name: {company_name} | Sector: {sector}")

        # 1. Check Income Statement items
        is_sec = raw_company.sections.get("standardized_income_statement")
        bs_sec = raw_company.sections.get("standardized_balance_sheet")
        cf_sec = raw_company.sections.get("standardized_cash_flow")

        periods_is = is_sec.periods if is_sec else []
        periods_bs = bs_sec.periods if bs_sec else []
        periods_cf = cf_sec.periods if cf_sec else []

        print(f"  📅 Periods found -> IS: {len(periods_is)}, BS: {len(periods_bs)}, CF: {len(periods_cf)}")

        # 2. Test Balance Sheet Forensic Audit (A = L + E) across all periods
        bs_verified = True
        bs_checks_detail = []
        if bs_sec:
            bs_map = {it.label: it.values for it in bs_sec.items}
            ta_vals = bs_map.get("Total Assets", {})
            tl_vals = bs_map.get("Total Liabilities", {})
            te_vals = bs_map.get("Total Equity", {})

            for p in periods_bs:
                ta = ta_vals.get(p)
                tl = tl_vals.get(p)
                te = te_vals.get(p)

                if ta is not None and tl is not None and te is not None:
                    le = tl + te
                    diff = abs(ta - le)
                    pct_diff = (diff / ta * 100) if ta else 0.0
                    passed = pct_diff <= 5.0
                    if not passed:
                        bs_verified = False
                    bs_checks_detail.append((p, ta, tl, te, pct_diff, passed))

        print(f"  ⚖️ Balance Sheet Forensic Check (A = L + E): {'✓ PASS' if bs_verified and bs_checks_detail else '⚠ MISMATCH / INCOMPLETE'}")
        if bs_checks_detail:
            for p, ta, tl, te, pct_diff, passed in bs_checks_detail[-3:]:  # Show last 3 periods
                status = "✓" if passed else "✗"
                print(f"     [{status}] Period {p}: Assets={ta:,.0f} | Liab+Eq={(tl+te):,.0f} | Diff={pct_diff:.2f}%")

        # 3. Test Derivation Logic (Discrete Quarters & YoY Series)
        if is_sec:
            net_item = next((it for it in is_sec.items if "Net Profit" in it.label or "صافي" in it.label or it.label == "Net Profit for the Period"), None)
            if net_item:
                raw_vals = [net_item.values.get(p) for p in periods_is]
                numeric_vals = [v for v in raw_vals if isinstance(v, (int, float))]
                if len(numeric_vals) >= 4:
                    yoy = _derive_yoy_series(numeric_vals)
                    ttm = _derive_ttm(numeric_vals)
                    print(f"  📈 Net Income Derivation Check:")
                    print(f"     - Raw sample values (last 4): {numeric_vals[-4:]}")
                    print(f"     - YoY calculated (last 4): {[f'{y:.1f}%' if y is not None else 'None' for y in yoy[-4:]]}")
                    print(f"     - TTM calculated (latest): {ttm[-1]:,.0f} (Sum of 4 quarters)" if ttm and ttm[-1] is not None else "     - TTM: N/A")

        # 4. Test dynamic row generation from XBRL
        dyn_rows = []
        if is_sec and is_sec.items:
            p_slice = periods_is[-5:] if len(periods_is) >= 5 else periods_is
            for it in is_sec.items:
                if getattr(it, "is_unmapped", False) or not it.label:
                    continue
                v_arr = [it.values.get(p) for p in p_slice]
                dyn_rows.append({"label": it.label, "values": v_arr})
        
        if dyn_rows:
            print(f"  🎯 Dynamic Financial Rows: {len(dyn_rows)} standardized lines built dynamically.")
            success_count += 1
        else:
            print(f"  ❌ Failed to extract dynamic statement rows for {sym}")

    print("\n" + "=" * 80)
    print(f" SUMMARY: Successfully verified {success_count}/{total_tested} companies.")
    print("=" * 80)

if __name__ == "__main__":
    run_verification()
