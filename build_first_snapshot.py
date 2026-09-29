"""
سكربت لبناء اول snapshot للـ universe وتخزينه في DB.
شغّله مرة واحدة بعد create_snapshot_table.py:
    ..\venv\Scripts\python.exe build_first_snapshot.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

print("Building universe snapshot... (may take 30-60 seconds on first run)")

from app.services.khurafshi_engine_service import build_universe_snapshot

n = build_universe_snapshot()
print(f"[OK] Snapshot built successfully — {n} companies stored in rebh_universe_snapshots.")
print("The /api/rebh/peers endpoint will now respond in milliseconds.")
