import os
import sys

# إضافة المسار الرئيسي للمشروع عشان نقدر نستدعي الاتصال بقاعدة البيانات
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal
from sqlalchemy import text

def get_available_symbols_from_db():
    db = SessionLocal()
    try:
        # هنا بنجيب عمود company_symbol من جدول company_financial_metrics
        # بنستخدم DISTINCT عشان نجيب الرموز بدون تكرار
        query = text("SELECT DISTINCT company_symbol FROM company_financial_metrics ORDER BY company_symbol")
        result = db.execute(query)
        
        # استخراج الرموز من النتيجة
        symbols = [str(row[0]) for row in result]
        
        print("\n" + "="*50)
        print(f"📊 تم العثور على {len(symbols)} سهم مسجل لهم بيانات في قاعدة البيانات:")
        print(",".join(symbols))
        print("="*50)
        
        # بنحفظهم برضو في ملف لو حبيت تنسخهم بسهولة
        with open("db_available_symbols.txt", "w", encoding="utf-8") as f:
            f.write(",".join(symbols))
        print("\n📁 تم حفظ الرموز في ملف db_available_symbols.txt")
        
    except Exception as e:
        print(f"❌ حدث خطأ أثناء الاتصال بقاعدة البيانات: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    get_available_symbols_from_db()
