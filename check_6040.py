# one-off: verify symbol 6040 price jump around its 2026-09-30 ex-date in our DB
from pathlib import Path
from sqlalchemy import create_engine, text

url = [l.split("=", 1)[1].strip() for l in open(Path(__file__).parent / ".env", encoding="utf-8")
       if l.startswith("DATABASE_URL")][0]
e = create_engine(url)
with e.connect() as c:
    rows = c.execute(text(
        "SELECT date, close, change_percent, volume_traded FROM prices "
        "WHERE symbol='6040' AND date BETWEEN '2026-09-20' AND '2026-09-30' ORDER BY date"
    )).fetchall()
for r in rows:
    print(tuple(r))
