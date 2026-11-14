import os
import sys
from datetime import timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func
from app.core.database import SessionLocal
from app.models.market_reports import (
    SubstantialShareholder,
    NetShortPosition,
    ForeignHeadroom,
    ShareBuyback,
    SBLPosition,
)

def check_model_history(db, model, name):
    print(f"\n--- {name} ---")
    
    # Get all distinct report dates
    dates = db.query(model.report_date).distinct().order_by(model.report_date).all()
    dates = [d[0] for d in dates if d[0]]
    
    if not dates:
        print("لا توجد بيانات (No data available).")
        return
        
    start_date = dates[0]
    end_date = dates[-1]
    
    print(f"إجمالي الأيام المسجلة (Total recorded days): {len(dates)}")
    print(f"تاريخ البداية (Start date): {start_date}")
    print(f"تاريخ النهاية (End date): {end_date}")
    
    # Generate all expected dates between start and end (excluding Friday and Saturday)
    # Saudi market is open Sunday to Thursday
    expected_dates = set()
    current_date = start_date
    while current_date <= end_date:
        if current_date.weekday() not in [4, 5]:  # 4 is Friday, 5 is Saturday
            expected_dates.add(current_date)
        current_date += timedelta(days=1)
        
    actual_dates = set(dates)
    missing_dates = sorted(list(expected_dates - actual_dates))
    
    if missing_dates:
        print(f"الأيام المفقودة (Missing business days): {len(missing_dates)}")
        for d in missing_dates:
            print(f" - {d}")
    else:
        print("لا يوجد أيام مفقودة ضمن هذه الفترة (No missing business days).")

def main():
    db = SessionLocal()
    try:
        check_model_history(db, SubstantialShareholder, "Substantial Shareholders (كبار الملاك)")
        check_model_history(db, NetShortPosition, "Net Short Positions (صافي المراكز المكشوفة)")
        check_model_history(db, ForeignHeadroom, "Foreign Headroom (النسب القصوى لاستثمار الأجانب)")
        check_model_history(db, ShareBuyback, "Share Buybacks (أسهم الخزينة)")
        check_model_history(db, SBLPosition, "SBL Positions (إقراض واقتراض الأوراق المالية)")
    finally:
        db.close()

if __name__ == "__main__":
    main()
