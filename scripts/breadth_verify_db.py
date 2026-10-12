# -*- coding: utf-8 -*-
"""breadth_verify_db.py — quick health check of the loaded breadth tables.

Reads DATABASE_URL from backend/.env (same rule as the loader) and prints:
  * per-table row count + max(date) for all breadth_* tables
  * the latest composite row (score/regime/overlay/driver)
  * last 4 breadth_runs entries
Exit 0 = all tables non-empty; 1 otherwise.

Usage:  backend\\venv\\Scripts\\python.exe scripts/breadth_verify_db.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, inspect, text  # noqa: E402


def db_url() -> str:
    for line in (BACKEND / ".env").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*DATABASE_URL\s*=\s*(.+)", line)
        if m:
            url = m.group(1).strip().strip('"').strip("'")
            if url.startswith("postgresql://"):
                url = "postgresql+psycopg2://" + url[len("postgresql://"):]
            return url
    raise SystemExit("DATABASE_URL not found in backend/.env")


def main() -> int:
    eng = create_engine(db_url())
    ins = inspect(eng)
    tables = sorted(t for t in ins.get_table_names()
                    if t.startswith("breadth_") and t != "breadth_runs")
    ok = True
    with eng.connect() as c:
        print(f"{'table':35} {'rows':>8}  last_date")
        print("-" * 62)
        for t in tables:
            n, last = c.execute(text(f"SELECT count(*), max(date) FROM {t}")).fetchone()
            if not n:
                ok = False
            print(f"{t:35} {n:>8}  {last}")

        last_date = c.execute(text("SELECT max(date) FROM breadth_composite")).scalar()
        row = c.execute(text(
            "SELECT round(score::numeric,1), regime, round(overlay::numeric,1), driver "
            "FROM breadth_composite WHERE date = :d"), {"d": last_date}).fetchone()
        print(f"\nlatest composite ({last_date}): score={row[0]} regime={row[1]} "
              f"overlay={row[2]} driver={row[3]}")

        print("\nbreadth_runs (last 4):")
        for r in c.execute(text(
                "SELECT id, asof, status, rows_prices, seconds FROM breadth_runs "
                "ORDER BY id DESC LIMIT 4")).fetchall():
            print("  ", tuple(r))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
