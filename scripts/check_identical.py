"""
Quick check: which symbols still have identical Sep17 vs Sep20 data?
"""
import sys
sys.path.insert(0, '.')
from app.core.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()

result = db.execute(text("""
    SELECT p20.symbol, p20.open as o20, p20.close as c20,
           p17.open as o17, p17.close as c17
    FROM prices p20
    JOIN prices p17 ON p17.symbol = p20.symbol AND p17.date = '2026-09-17'
    WHERE p20.date = '2026-09-20'
      AND p20.open = p17.open AND p20.close = p17.close
      AND p20.high = p17.high AND p20.low = p17.low
    ORDER BY p20.symbol
"""))
rows = result.fetchall()
print(f"Symbols with IDENTICAL Sep17=Sep20 data: {len(rows)}")
for r in rows:
    d = dict(r._mapping)
    print(f"  {d['symbol']}: open={d['o20']}, close={d['c20']}")

db.close()
