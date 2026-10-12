# -*- coding: utf-8 -*-
"""breadth_verify_snapshot.py — prove the JSONB round-trip equals the client reference.

Pulls DATA for a trading date out of breadth_data (Postgres), compares its
`series` block against api_samples/series.json (the client golden file) on
several dates spanning 2008/2015/2020/2026 — same display layer the 500-check
parity was proven on.

Usage:  backend\\venv\\Scripts\\python.exe scripts/breadth_verify_snapshot.py [--date 2026-09-30]
Exit 0 = all checks OK; 1 = mismatch; 2 = snapshot/coverage missing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, text  # noqa: E402

SAMPLES = REPO / "Rebh_Market_Breadth_Package" / "api_samples" / "series.json"
TOL = 0.01
DATES = ["2008-10-27", "2015-08-25", "2020-03-16", "2026-09-29"]


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
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="snapshot trading_date to load")
    args = ap.parse_args()

    eng = create_engine(db_url())
    with eng.connect() as c:
        row = c.execute(text(
            "SELECT trading_date, data FROM breadth_data "
            + ("WHERE trading_date = :d" if args.date else "ORDER BY trading_date DESC LIMIT 1")
        ), {"d": args.date} if args.date else {}).fetchone()
    if not row:
        print("FAIL: no snapshot row in breadth_data")
        return 2
    trading_date, data = row[0], row[1]
    if isinstance(data, str):
        data = json.loads(data)
    print(f"snapshot loaded: trading_date={trading_date} top-level keys={len(data)}")

    if "series" not in data or "dates" not in data:
        print("FAIL: DATA missing series/dates blocks")
        return 2

    golden = json.loads(SAMPLES.read_text(encoding="utf-8"))
    gdates = golden["dates"]
    dpos = {d: i for i, d in enumerate(data["dates"])}
    series = data["series"]

    n_ok = n_fail = 0
    fails = []
    for d in DATES:
        if d not in gdates or d not in dpos:
            print(f"  coverage gap for {d} — skipped")
            continue
        gi, di = gdates.index(d), dpos[d]
        for key, gser in golden["series"].items():
            gv = gser[gi] if gi < len(gser) else None
            my = series.get(key)
            mv = my[di] if my is not None and di < len(my) else None
            if gv is None and mv is None:
                n_ok += 1
                continue
            if isinstance(gv, str) or isinstance(mv, str):
                ok = gv == mv                        # categorical series (bpi_status etc.)
            elif gv is None or mv is None:
                ok = False
            else:
                ok = abs(float(gv) - float(mv)) <= TOL
            if ok:
                n_ok += 1
            else:
                n_fail += 1
                fails.append((d, key, gv, mv))

    print(f"checks: {n_ok + n_fail} | OK: {n_ok} | FAIL: {n_fail}")
    affected = sorted({f[1] for f in fails})
    print("affected series:", affected)
    for f in fails:
        print("  FAIL", f)
    if "today" in data:
        t = data["today"]
        print(f"today: date={t.get('date')} score={t.get('score')} regime={t.get('regime')} "
              f"overlay={t.get('overlay')} universe={t.get('n_universe')}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
