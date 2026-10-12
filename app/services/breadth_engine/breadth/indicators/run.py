"""python -m breadth.indicators.run  -> storage/features/{participation,advance_decline,thrusts,momentum,sectors,sectors_summary,concentration,divergences}.parquet"""
from __future__ import annotations

import json
import time

import pandas as pd

from breadth import ROOT, load_config
from breadth.indicators.advance_decline import compute_advance_decline
from breadth.indicators.common import build_panels
from breadth.indicators.momentum import compute_momentum
from breadth.indicators.participation import compute_participation, compute_weekly_stages
from breadth.indicators.thrusts import compute_thrusts
from breadth.indicators.sectors import compute_sectors
from breadth.indicators.concentration import compute_concentration
from breadth.indicators.divergences import build_metrics, compute_divergences
from breadth.indicators.widget import compute_widget
from breadth.indicators.classic import compute_classic
from breadth.indicators.pnf import compute_bpi


def load_clean(cfg: dict):
    st = ROOT / cfg["paths"]["storage_dir"]
    prices = pd.read_parquet(st / "clean" / "prices.parquet")
    master = pd.read_parquet(st / "master" / "symbols.parquet")
    index = pd.read_parquet(st / "clean" / "index.parquet")
    return prices, master, index, st


def run(cfg: dict) -> dict:
    t0 = time.time()
    prices, master, index, st = load_clean(cfg)
    uni = set(master.loc[master["in_universe"], "symbol"])
    p = build_panels(prices, uni)
    part = compute_participation(p, cfg)
    ad = compute_advance_decline(p, cfg)
    (st / "features").mkdir(exist_ok=True)
    part.reset_index().to_parquet(st / "features" / "participation.parquet", index=False)
    ad.reset_index().to_parquet(st / "features" / "advance_decline.parquet", index=False)
    th = compute_thrusts(ad, part, cfg)
    mom = compute_momentum(p, cfg)
    th.reset_index().to_parquet(st / "features" / "thrusts.parquet", index=False)
    mom.reset_index().to_parquet(st / "features" / "momentum.parquet", index=False)
    sec_long, sec_sum = compute_sectors(p, master, cfg)
    sec_long.to_parquet(st / "features" / "sectors.parquet", index=False)
    sec_sum.reset_index().to_parquet(st / "features" / "sectors_summary.parquet", index=False)
    conc = compute_concentration(index, p, prices, cfg)
    conc.reset_index().to_parquet(st / "features" / "concentration.parquet", index=False)
    tasi = index[index["index_code"] == "TASI"].set_index("date")["close"]
    div = compute_divergences(tasi, build_metrics(ad, part), cfg)
    div.reset_index().to_parquet(st / "features" / "divergences.parquet", index=False)
    open_adj = prices[prices["symbol"].isin(uni)].pivot(index="date", columns="symbol", values="open_adj").sort_index()
    wd = compute_widget(p, open_adj, cfg)
    compute_weekly_stages(p, cfg).reset_index().to_parquet(st / "features" / "stages_weekly.parquet", index=False)
    compute_classic(ad, part, tasi, cfg).reset_index().to_parquet(st / "features" / "classic.parquet", index=False)
    bpi, _ = compute_bpi(p, cfg)
    bpi.reset_index().to_parquet(st / "features" / "bpi.parquet", index=False)
    wd.reset_index().to_parquet(st / "features" / "widget.parquet", index=False)
    meta = {"rows": int(len(part)), "date_max": str(part.index.max().date()),
            "seconds": round(time.time() - t0, 1)}
    return meta


if __name__ == "__main__":
    print(json.dumps(run(load_config()), indent=2))
