"""
reset_tracker.py
================
يمسح كل الـ trades من جدول wallet_trades للمستخدم الحالي.
يعمل ريسيت كامل للـ Monthly Tracker.

Usage:
    cd d:\Work\LUMIVST\backend
    ..\venv\Scripts\python.exe scripts\reset_tracker.py
"""

import sys
import os

# Add the backend directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.database import SessionLocal
from app.models.wallet import WalletTrade


def reset_tracker():
    db = SessionLocal()
    try:
        count = db.query(WalletTrade).count()
        if count == 0:
            print("✅ الـ Monthly Tracker فاضي أصلاً — مفيش trades للمسح.")
            return

        print(f"⚠️  هيتم مسح {count} trade(s) من الـ Monthly Tracker...")
        confirm = input("متأكد؟ اكتب 'yes' للتأكيد: ").strip().lower()
        if confirm != "yes":
            print("❌ تم الإلغاء.")
            return

        db.query(WalletTrade).delete()
        db.commit()
        print(f"✅ تم مسح {count} trade(s) بنجاح! الـ Monthly Tracker بدأ من الصفر.")
    except Exception as e:
        db.rollback()
        print(f"❌ Error: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    reset_tracker()
