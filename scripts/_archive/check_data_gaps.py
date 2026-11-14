import sys
import os
from dateutil.relativedelta import relativedelta
from sqlalchemy import func

# تأكيد المسار عشان نقدر نعمل import للـ app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal
from app.models.economic_indicators import SP500History, TreasuryYieldCurve, EconomicIndicator

def get_expected_months(start_date, end_date):
    """حساب كل الشهور المتوقعة بين تاريخين"""
    expected = []
    current = start_date.replace(day=1)
    end = end_date.replace(day=1)
    while current <= end:
        expected.append((current.year, current.month))
        current += relativedelta(months=1)
    return expected

def check_gaps(db, model, date_column, name, filter_cond=None):
    print(f"\n{'='*50}")
    print(f"📊 فحص: {name}")
    
    # 1. جلب أول وآخر تاريخ وإجمالي السجلات
    query = db.query(
        func.min(date_column).label("min_date"),
        func.max(date_column).label("max_date"),
        func.count(date_column).label("count")
    )
    if filter_cond is not None:
        query = query.filter(filter_cond)
        
    res = query.one()
    min_date, max_date, total_count = res.min_date, res.max_date, res.count
    
    if not min_date or total_count == 0:
        print("  ❌ لا يوجد بيانات (الجدول فارغ).")
        return
        
    print(f"  📅 النطاق الزمني: من {min_date} إلى {max_date}")
    print(f"  🔢 إجمالي السجلات: {total_count}")
    
    # 2. تجميع السجلات حسب السنة والشهر
    ym_query = db.query(
        func.extract('year', date_column).label('year'),
        func.extract('month', date_column).label('month'),
        func.count(date_column).label('count')
    )
    if filter_cond is not None:
        ym_query = ym_query.filter(filter_cond)
        
    ym_query = ym_query.group_by('year', 'month').all()
    
    actual_months = {(int(r.year), int(r.month)): r.count for r in ym_query}
    expected_months = get_expected_months(min_date, max_date)
    
    missing_months = []
    
    # 3. مقارنة الشهور المتوقعة بالشهور الموجودة
    for ym in expected_months:
        if ym not in actual_months:
            missing_months.append(f"{ym[0]}-{ym[1]:02d}")
            
    if not missing_months:
        print("  ✅ ممتاز! لا يوجد أي شهور مفقودة بالكامل في هذا النطاق.")
    else:
        print(f"  ⚠️ تحذير: يوجد {len(missing_months)} شهور مفقودة تماماً (0 سجلات):")
        # نطبع أول 15 شهر مفقود بس عشان منزحمش الشاشة
        print("    " + ", ".join(missing_months[:15]))
        if len(missing_months) > 15:
            print(f"    ... بالإضافة لـ {len(missing_months) - 15} شهور أخرى.")

def run():
    print("\n🔍 جاري فحص الفجوات الزمنية في قاعدة البيانات...\n")
    db = SessionLocal()
    try:
        # فحص S&P 500
        check_gaps(db, SP500History, SP500History.trade_date, "S&P 500 History")
        
        # فحص Treasury
        check_gaps(db, TreasuryYieldCurve, TreasuryYieldCurve.report_date, "Treasury Yield Curves")
        
        # فحص Economic Indicators
        codes = db.query(EconomicIndicator.indicator_code).distinct().all()
        for code in codes:
            c = code[0]
            check_gaps(db, EconomicIndicator, EconomicIndicator.report_date, f"Economic Indicator: {c}", filter_cond=(EconomicIndicator.indicator_code == c))
            
    finally:
        db.close()
    print(f"\n{'='*50}")
    print("🏁 انتهى الفحص.")

if __name__ == "__main__":
    run()
