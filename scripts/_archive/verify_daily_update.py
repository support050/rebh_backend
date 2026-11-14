"""
✅ سكريبت التحقق من التحديث اليومي
شغله بعد daily_market_update.py للتأكد من أن البيانات حفظت بنجاح
"""

import sys
from pathlib import Path
import logging
from datetime import date
from sqlalchemy import text

# إضافة المسار
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal

# إعداد Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TARGET_DATE = date(2026, 5, 31)

def verify_daily_update():
    """
    التحقق من نجاح التحديث اليومي
    """
    db = SessionLocal()
    
    try:
        logger.info(f"\n{'='*50}")
        logger.info(f"✅ بدء التحقق من التحديث ليوم {TARGET_DATE}")
        logger.info(f"{'='*50}\n")
        
        # 1️⃣ فحص rs_daily_v2
        logger.info("1️⃣ جدول rs_daily_v2 (بيانات RS للأسهم):")
        result = db.execute(text("""
            SELECT COUNT(*) as count FROM rs_daily_v2 
            WHERE date = :target_date
        """), {"target_date": TARGET_DATE})
        count_rs = result.scalar()
        logger.info(f"   📊 السجلات: {count_rs}")
        
        if count_rs > 0:
            sample = db.execute(text("""
                SELECT symbol, rs_rating, date 
                FROM rs_daily_v2 
                WHERE date = :target_date 
                LIMIT 3
            """), {"target_date": TARGET_DATE})
            logger.info("   📋 عينة من البيانات:")
            for row in sample:
                logger.info(f"      - {row[0]}: RS Rating = {row[1]}")
        logger.info("")
        
        # 2️⃣ فحص prices
        logger.info("2️⃣ جدول prices (أسعار الإغلاق):")
        result = db.execute(text("""
            SELECT COUNT(*) as count FROM prices 
            WHERE date = :target_date
        """), {"target_date": TARGET_DATE})
        count_price = result.scalar()
        logger.info(f"   📊 السجلات: {count_price}")
        
        if count_price > 0:
            sample = db.execute(text("""
                SELECT symbol, close, change_percent, date 
                FROM prices 
                WHERE date = :target_date 
                LIMIT 3
            """), {"target_date": TARGET_DATE})
            logger.info("   📋 عينة من البيانات:")
            for row in sample:
                logger.info(f"      - {row[0]}: Close = {row[1]}, Change% = {row[2]}%")
        logger.info("")
        
        # 3️⃣ فحص stock_indicators
        logger.info("3️⃣ جدول stock_indicators (المؤشرات الفنية):")
        result = db.execute(text("""
            SELECT COUNT(*) as count FROM stock_indicators 
            WHERE date = :target_date
        """), {"target_date": TARGET_DATE})
        count_tech = result.scalar()
        logger.info(f"   📊 السجلات: {count_tech}")
        
        if count_tech > 0:
            sample = db.execute(text("""
                SELECT symbol, sma_50, sma_200, date 
                FROM stock_indicators 
                WHERE date = :target_date 
                LIMIT 3
            """), {"target_date": TARGET_DATE})
            logger.info("   📋 عينة من البيانات:")
            for row in sample:
                logger.info(f"      - {row[0]}: SMA50 = {row[1]}, SMA200 = {row[2]}")
        logger.info("")
        
        # 4️⃣ فحص industry_group_history
        logger.info("4️⃣ جدول industry_group_history (مجموعات الصناعة):")
        result = db.execute(text("""
            SELECT COUNT(*) as count FROM industry_group_history 
            WHERE date = :target_date
        """), {"target_date": TARGET_DATE})
        count_ig = result.scalar()
        logger.info(f"   📊 السجلات: {count_ig}")
        
        if count_ig > 0:
            sample = db.execute(text("""
                SELECT industry_group, rs_score, rank, date 
                FROM industry_group_history 
                WHERE date = :target_date 
                LIMIT 3
            """), {"target_date": TARGET_DATE})
            logger.info("   📋 عينة من البيانات:")
            for row in sample:
                logger.info(f"      - {row[0]}: RS Score = {row[1]}, Rank = {row[2]}")
        logger.info("")
        
        # 5️⃣ فحص market_breadth
        logger.info("5️⃣ جدول market_breadth (اتساع السوق):")
        result = db.execute(text("""
            SELECT COUNT(*) as count FROM market_breadth 
            WHERE date = :target_date
        """), {"target_date": TARGET_DATE})
        count_breadth = result.scalar()
        logger.info(f"   📊 السجلات: {count_breadth}")
        
        if count_breadth > 0:
            sample = db.execute(text("""
                SELECT * FROM market_breadth 
                WHERE date = :target_date 
                LIMIT 1
            """), {"target_date": TARGET_DATE})
            logger.info("   📋 البيانات:")
            for row in sample:
                logger.info(f"      - {row}")
        logger.info("")
        
        # 6️⃣ حالة التحديث
        logger.info("6️⃣ حالة التحديث:")
        result = db.execute(text("""
            SELECT is_updating, latest_ready_date, completed_at 
            FROM update_status 
            WHERE id = 1
        """))
        row = result.fetchone()
        if row:
            is_updating, latest_ready, completed = row
            logger.info(f"   🔄 في حالة تحديث: {is_updating}")
            logger.info(f"   📅 آخر تاريخ جاهز: {latest_ready}")
            logger.info(f"   ✅ وقت الاكتمال: {completed}")
        logger.info("")
        
        # ملخص النتائج
        total = count_rs + count_price + count_tech + count_ig + count_breadth
        
        logger.info(f"{'='*50}")
        logger.info(f"📊 ملخص النتائج")
        logger.info(f"{'='*50}")
        logger.info(f"📅 التاريخ: {TARGET_DATE}")
        logger.info(f"📈 إجمالي السجلات: {total}\n")
        
        logger.info("📋 التفاصيل:")
        logger.info(f"   ├─ rs_daily_v2: {count_rs} ✅" if count_rs > 0 else f"   ├─ rs_daily_v2: {count_rs} ❌")
        logger.info(f"   ├─ prices: {count_price} ✅" if count_price > 0 else f"   ├─ prices: {count_price} ❌")
        logger.info(f"   ├─ stock_indicators: {count_tech} ✅" if count_tech > 0 else f"   ├─ stock_indicators: {count_tech} ❌")
        logger.info(f"   ├─ industry_group_history: {count_ig} ✅" if count_ig > 0 else f"   ├─ industry_group_history: {count_ig} ❌")
        logger.info(f"   └─ market_breadth: {count_breadth} ✅" if count_breadth > 0 else f"   └─ market_breadth: {count_breadth} ❌")
        
        # التقييم النهائي
        logger.info(f"\n{'='*50}")
        if count_rs > 0 and count_price > 0 and count_ig > 0:
            logger.info("🎉 التحديث نجح بنجاح! جميع البيانات الأساسية موجودة")
        elif count_rs > 0 or count_price > 0:
            logger.info("⚠️  التحديث جزئي - توجد بيانات لكن قد تكون هناك أخطاء")
        else:
            logger.info("❌ التحديث فشل - لم يتم حفظ البيانات")
        logger.info(f"{'='*50}\n")

    except Exception as e:
        logger.error(f"❌ خطأ في التحقق: {e}")
        raise
    finally:
        db.close()

if __name__ == "__main__":
    verify_daily_update()
