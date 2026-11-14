"""
🔍 فحص الجداول الموجودة فعلياً في قاعدة البيانات
"""

import sys
from pathlib import Path
import logging

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from sqlalchemy import text, inspect

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def check_tables():
    """قائمة بجميع الجداول الموجودة"""
    db = SessionLocal()
    
    try:
        inspector = inspect(db.bind)
        tables = inspector.get_table_names()
        
        logger.info("\n📋 الجداول الموجودة في قاعدة البيانات:\n")
        for table in sorted(tables):
            result = db.execute(text(f"SELECT COUNT(*) FROM {table}"))
            count = result.scalar()
            logger.info(f"  ├─ {table}: {count} سجل")
        
        logger.info(f"\n✅ إجمالي الجداول: {len(tables)}")

    except Exception as e:
        logger.error(f"❌ خطأ: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    check_tables()
