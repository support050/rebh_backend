"""One command: python -m breadth.data.build [--config config/config.yaml]

Pipeline: raw CSV -> storage/raw/*.parquet -> storage/master/*.parquet -> storage/clean/*.parquet
Every step is deterministic and re-runnable; the raw parquet records the source file hash.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import pandas as pd

from breadth import ROOT, load_config
from breadth.data.cleaner import build_calendar, build_symbols_master, normalize_prices
from breadth.data.corporate_actions import apply_adjustments, apply_overrides, detect_events
from breadth.data.index import build_equal_weight, combine_indices
from breadth.data.loader import file_sha256, load_index_csv, load_prices_csv


def run(cfg: dict, root: Path = ROOT) -> dict:
    t0 = time.time()
    st = root / cfg["paths"]["storage_dir"]
    for sub in ["raw", "master", "clean", "features"]:
        (st / sub).mkdir(parents=True, exist_ok=True)

    # ---- raw
    p_csv = st / cfg["paths"]["raw_prices_csv"]
    i_csv = st / cfg["paths"]["raw_index_csv"]
    prices = load_prices_csv(p_csv)
    index_df = load_index_csv(i_csv)
    prices.to_parquet(st / "raw" / "prices_raw.parquet", index=False)
    index_df.to_parquet(st / "raw" / "index_raw.parquet", index=False)

    # ---- master
    calendar = build_calendar(prices, index_df)
    master = build_symbols_master(prices, cfg)
    calendar.to_parquet(st / "master" / "calendar.parquet", index=False)
    master.to_parquet(st / "master" / "symbols.parquet", index=False)

    # ---- clean
    clean = normalize_prices(prices, calendar, cfg)
    events = detect_events(clean, cfg)
    events = apply_overrides(events, root / cfg["paths"]["overrides_csv"])
    events.to_parquet(st / "master" / "corporate_actions.parquet", index=False)
    events.to_csv(st / "master" / "corporate_actions.csv", index=False)
    adj = apply_adjustments(clean, events)
    adj = adj.merge(master[["symbol", "in_universe", "industry_group", "sector"]], on="symbol", how="left")
    adj["year"] = adj["date"].dt.year
    out_dir = st / "clean" / "prices.parquet"
    if out_dir.exists():
        shutil.rmtree(out_dir)          # partitioned writes append; always rebuild from scratch
    adj.to_parquet(out_dir, index=False, partition_cols=["year"])

    ew = build_equal_weight(adj, master, cfg)
    idx = combine_indices(index_df, ew)
    idx.to_parquet(st / "clean" / "index.parquet", index=False)

    meta = {
        "built_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "prices_csv": str(p_csv.name), "prices_sha256": file_sha256(p_csv),
        "index_csv": str(i_csv.name), "index_sha256": file_sha256(i_csv),
        "rows_raw": int(len(prices)), "rows_clean": int(len(adj)),
        "symbols": int(master.shape[0]), "symbols_in_universe": int(master["in_universe"].sum()),
        "date_min": str(adj["date"].min().date()), "date_max": str(adj["date"].max().date()),
        "events_detected": int(len(events)), "seconds": round(time.time() - t0, 1),
    }
    (st / "build_meta.json").write_text(json.dumps(meta, indent=2))
    return meta


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    print(json.dumps(run(load_config(a.config)), indent=2))
