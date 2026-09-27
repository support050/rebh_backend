"""Check if Sep 20 prices exist for banking stocks."""
import sys
sys.path.insert(0, '.')
from app.core.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()

# Check prices for bank stocks on Sep 17 and Sep 20
print("=== Prices for 1010 (RIBL) ===")
result = db.execute(text(
    "SELECT date, open, high, low, close FROM prices "
    "WHERE symbol = '1010' AND date >= '2026-09-15' ORDER BY date"
))
rows = result.fetchall()
for r in rows:
    print(dict(r._mapping))

print("\n=== Count of stocks with Sep 20 data ===")
result2 = db.execute(text(
    "SELECT COUNT(DISTINCT symbol) as cnt FROM prices WHERE date = '2026-09-20'"
))
print(result2.fetchone())

print("\n=== Count of stocks with Sep 17 data ===")
result3 = db.execute(text(
    "SELECT COUNT(DISTINCT symbol) as cnt FROM prices WHERE date = '2026-09-17'"
))
print(result3.fetchone())

print("\n=== Are 1010 Sep17 and Sep20 identical? ===")
result4 = db.execute(text("""
    SELECT p17.date as d17, p17.open as o17, p17.close as c17,
           p20.date as d20, p20.open as o20, p20.close as c20
    FROM prices p17
    JOIN prices p20 ON p17.symbol = p20.symbol
    WHERE p17.symbol = '1010'
      AND p17.date = '2026-09-17'
      AND p20.date = '2026-09-20'
"""))
row = result4.fetchone()
if row:
    print(dict(row._mapping))
else:
    print("NO Sep 20 data for 1010!")

db.close()
