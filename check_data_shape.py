# one-off: dump the top-level shape of the stored DATA snapshot
from pathlib import Path
import json
from sqlalchemy import create_engine, text

url = [l.split("=", 1)[1].strip() for l in open(Path(__file__).parent / ".env", encoding="utf-8")
       if l.startswith("DATABASE_URL")][0]
e = create_engine(url)
with e.connect() as c:
    raw = c.execute(text(
        "SELECT jsonb_object_keys(data) FROM breadth_data ORDER BY trading_date DESC LIMIT 1"
    )).fetchall()
    blob = c.execute(text(
        "SELECT data::text FROM breadth_data ORDER BY trading_date DESC LIMIT 1"
    )).scalar()
D = json.loads(blob)
print("top-level keys:", sorted(r[0] for r in raw))
for k in sorted(D):
    v = D[k]
    if isinstance(v, list):
        print(f"  {k:18} list[{len(v)}]", "item0:", (sorted(v[0]) if v and isinstance(v[0], dict) else str(v[:1])[:80]))
    elif isinstance(v, dict):
        print(f"  {k:18} dict({len(v)}) keys0:", str(list(v)[:6])[1:-1][:90])
    else:
        print(f"  {k:18} {type(v).__name__}: {str(v)[:60]}")
