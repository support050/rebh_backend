"""
سكربت سريع لإنشاء جدول rebh_universe_snapshots مباشرةً بدون Alembic.
شغّله مرة واحدة فقط:
    ..\venv\Scripts\python.exe create_snapshot_table.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.core.database import engine, Base
import app.models.rebh_universe_snapshot  # noqa — registers model with Base

# إنشاء الجدول إذا لم يكن موجوداً (CREATE TABLE IF NOT EXISTS)
Base.metadata.create_all(bind=engine, tables=[Base.metadata.tables["rebh_universe_snapshots"]])
print("[OK] Table rebh_universe_snapshots created (or already exists).")
