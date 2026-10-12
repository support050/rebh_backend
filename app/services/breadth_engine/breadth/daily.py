"""ONE command for the daily update:

    python -m breadth.daily [--prices <csv>] [--index <csv>] [--skip-data]

1. Picks the newest `TASI_prices_ALL_*.csv` and `historical_reports_tasi_*.csv` in storage/raw/
   (or the files given) -> data build (clean + adjust + EW index)
2. Indicators (families A-G) -> composite (score, regime, drivers)
3. Dashboard HTML + Arabic one-page summary in storage/reports/
Prints a JSON status with today's score/regime and any newly detected corporate actions to review.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import time

import pandas as pd

from breadth import ROOT, load_config


def newest(pattern: str) -> str | None:
    files = sorted(glob.glob(str(ROOT / "storage" / "raw" / pattern)), key=os.path.getmtime)
    return os.path.basename(files[-1]) if files else None


def run(prices: str | None = None, index: str | None = None, skip_data: bool = False) -> dict:
    t0 = time.time()
    cfg = load_config()
    status = {}
    if not skip_data:
        p = prices or newest("TASI_prices_ALL_*.csv"); i = index or newest("historical_reports_tasi_*.csv")
        if not p or not i:
            raise SystemExit("raw files not found in storage/raw/ (need TASI_prices_ALL_*.csv and historical_reports_tasi_*.csv)")
        cfg["paths"]["raw_prices_csv"] = f"raw/{p}"; cfg["paths"]["raw_index_csv"] = f"raw/{i}"
        from breadth.data.build import run as build_data
        status["data"] = build_data(cfg)
    from breadth.indicators.run import run as run_ind
    status["indicators"] = run_ind(cfg)
    from breadth.composite.run import run as run_comp
    status["composite"] = run_comp(cfg)
    from breadth.reports.dashboard import build as build_dash
    from breadth.reports.daily_summary import build as build_sum
    status["dashboard"] = build_dash(cfg)
    from breadth.reports.pro_dashboard import build as build_pro
    status["pro_dashboard"] = build_pro(cfg)
    from breadth.reports.canary_study import run as run_canary
    run_canary(cfg)
    status["summary"] = build_sum(cfg)
    ca = ROOT / "storage" / "master" / "corporate_actions.csv"
    if ca.exists():
        ev = pd.read_csv(ca); ev["ex_date"] = pd.to_datetime(ev["ex_date"])
        recent = ev[ev["ex_date"] >= pd.Timestamp.today() - pd.Timedelta(days=30)]
        status["corporate_actions_to_review"] = recent[["symbol", "ex_date", "action_type", "factor", "confidence"]].astype(str).to_dict("records")
    status["seconds"] = round(time.time() - t0, 1)
    return status


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices"); ap.add_argument("--index"); ap.add_argument("--skip-data", action="store_true")
    a = ap.parse_args()
    print(json.dumps(run(a.prices, a.index, a.skip_data), indent=2, ensure_ascii=False, default=str))
