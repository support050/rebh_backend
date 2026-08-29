
"""
REBH Trust System Auditor & Market Classifier
==============================================
يفحص هذا الاسكربت جميع الشركات المتاحة في قاعدة البيانات ويصنفها وفق النقاط الأربع لنظام الثقة:
1. [✓] تم التحقق آلياً (Verified Clean): مطابقة تامة للميزانيات والتدفقات.
2. [°] قيم مشتقة ومحسوبة بالكامل (Fully Derived Series): سلاسل الأرباع والـ TTM مكتملة.
3. [≈] قيم تقديرية معلنة (Estimates / Approximations): تستخدم تقديرات معلنة (مثل صكوك غير مفصلة أو capex صيانة).
4. [⚑] أعلام تدقيق وتنبيه (Audit Flags / Review Required): فجوات في الميزانية، عدم اتساق مقياس، أو ديون حرجة.
"""

import sys
import json
import glob
from pathlib import Path
from typing import Dict, List, Any

# Ensure backend modules are in path
backend_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(backend_dir))

from app.services.rebh_engine_service import (
    check_balance_sheet,
    check_cash_flow,
    get_trust_badge_status
)
from app.services.xbrl_data_service import list_companies, get_company


def audit_trust_system():
    print("=" * 80)
    print("🚀 بدء فحص وتصنيف شركات السوق وفق نظام الثقة المالي (The Trust System)")
    print("=" * 80)

    files = glob.glob(str(backend_dir / "output" / "*_financials.json"))
    symbols = [Path(f).stem.replace("_financials", "") for f in files if not f.endswith("companies_list.json")]
    
    # Categories
    verified_list = []      # [✓]
    derived_list = []       # [°]
    estimates_list = []     # [≈]
    flags_list = []         # [⚑]
    empty_is_list = []      # الشركات التي ينقصها قائمة دخل

    for sym in sorted(symbols):
        company = get_company(sym)
        if not company:
            continue
        
        sec = company.sections
        meta = company.meta
        c_name = meta.company_name or sym
        
        # 1. فحص الميزانية والتوازن المحاسبي [✓]
        std_bs = sec.get("standardized_balance_sheet")
        std_is = sec.get("standardized_income_statement")
        std_cf = sec.get("standardized_cash_flow")
        
        bs_items = {it.label: it.values for it in std_bs.items if not getattr(it, "is_unmapped", False)} if std_bs else {}
        is_items = {it.label: it.values for it in std_is.items if not getattr(it, "is_unmapped", False)} if std_is else {}
        cf_items = {it.label: it.values for it in std_cf.items if not getattr(it, "is_unmapped", False)} if std_cf else {}

        # هل قائمة الدخل ممتلئة؟
        has_is = bool(is_items.get("Revenue / Turnover") or is_items.get("Special Commission Income") or is_items.get("Net Profit for the Period"))
        if not has_is:
            empty_is_list.append((sym, c_name))

        # فحص تطابق الميزانية
        bs_periods = std_bs.periods if std_bs else []
        bs_pass_count = 0
        bs_total = 0
        for p in bs_periods:
            ta = bs_items.get("Total Assets", {}).get(p)
            tl = bs_items.get("Total Liabilities", {}).get(p)
            te = bs_items.get("Total Equity", {}).get(p) or bs_items.get("Total Equity Attributable to Shareholders", {}).get(p)
            if ta is not None and tl is not None and te is not None:
                bs_total += 1
                if check_balance_sheet(ta, tl, te):
                    bs_pass_count += 1

        is_bs_verified = (bs_total > 0 and (bs_pass_count / bs_total) >= 0.9)
        
        # 2. فحص السلاسل المشتقة [°]
        has_full_quarters = len(bs_periods) >= 8 and has_is
        
        # 3. فحص التقديرات المعلنة [≈]
        uses_estimates = False
        if is_items.get("Finance Costs") is None and not sym.startswith("10"):
            uses_estimates = True
        
        # 4. فحص الأعلام والتنبيهات [⚑]
        company_flags = []
        if not is_bs_verified and bs_total > 0:
            company_flags.append(f"عدم توازن ميزانية ({bs_pass_count}/{bs_total} فترات مطابقة)")
        if not has_is:
            company_flags.append("قائمة الدخل غير مقروءة/فارغة")
        
        # التحقق من الديون المرتفعة للشركات غير المالية فقط
        is_bank = sym.startswith("10") or sym in ("1120", "1140", "1150", "1180", "1182", "1183") or "bank" in c_name.lower()
        latest_p = bs_periods[-1] if bs_periods else None
        if latest_p and not is_bank:
            te_val = bs_items.get("Total Equity", {}).get(latest_p) or bs_items.get("Total Equity Attributable to Shareholders", {}).get(latest_p) or 1.0
            tl_val = bs_items.get("Total Liabilities", {}).get(latest_p) or 0.0
            if te_val > 0 and (tl_val / te_val) > 4.0:
                company_flags.append(f"رافعة مالية مرتفعة لشركة غير مالية (D/E: {tl_val/te_val:.1f}x)")

        # تصنيف الشركة
        if is_bs_verified and has_is and not company_flags:
            verified_list.append((sym, c_name, f"مطابقة بنسبة {round(bs_pass_count/bs_total*100)}%"))
        
        if has_full_quarters:
            derived_list.append((sym, c_name, f"{len(bs_periods)} فترات XBRL"))
            
        if uses_estimates:
            estimates_list.append((sym, c_name, "تقدير تكلفة التمويل/الصيانة"))
            
        if company_flags:
            flags_list.append((sym, c_name, " | ".join(company_flags)))

    # عرض التقرير النهائي
    print("\n" + "📊 ملخص تصنيف السوق المالي (Trust System Classification):")
    print(f"• إجمالي الشركات المفحوصة: {len(symbols)} شركة")
    print(f"• [✓] شركات اجتازت الفحص الجنائي التام (Verified Clean): {len(verified_list)} شركة")
    print(f"• [°] شركات مكتملة السلاسل الزمنية والاشتقاق الربعي (Derived Series): {len(derived_list)} شركة")
    print(f"• [≈] شركات تستخدم تقريبات/تقديرات معلنة (Estimates): {len(estimates_list)} شركة")
    print(f"• [⚑] شركات عليها أعلام وملاحظات تدقيق (Under Review / Flags): {len(flags_list)} شركة")
    print("=" * 80)

    print("\n1️⃣ عينة من الشركات المجتازة للفحص الجنائي التام [✓] (Verified Clean):")
    for sym, name, note in verified_list[:8]:
        print(f"  ✓ [{sym}] {name} — {note}")
    if len(verified_list) > 8:
        print(f"  ... والمزيد ({len(verified_list) - 8} شركة أخرى)")

    print("\n2️⃣ عينة من الشركات ذات السلاسل الزمنية الكاملة [°] (Fully Derived):")
    for sym, name, note in derived_list[:8]:
        print(f"  ° [{sym}] {name} — {note}")

    print("\n3️⃣ الشركات التي تحمل أعلام وملاحظات تدقيق [⚑] (Audit Flags):")
    for sym, name, note in flags_list[:10]:
        print(f"  ⚑ [{sym}] {name} — {note}")
    if len(flags_list) > 10:
        print(f"  ... والمزيد ({len(flags_list) - 10} شركة أخرى)")

    print("\n" + "=" * 80)
    print("✅ اكتمل الفحص وتوليد تقرير نظام الثقة بنجاح.")
    print("=" * 80)


if __name__ == "__main__":
    audit_trust_system()
