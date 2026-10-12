"""Phase 3 calibration: thrust thresholds and the Stockbee move threshold on TASI.

python -m breadth.backtest.calibrate_thrusts
Outputs (storage/validation/):
  phase3_thrust_grid.csv       every (rule, parameter set): #signals, fwd 20/60/120 mean, hit rate, baseline
  phase3_thrust_events.csv     every historical occurrence of the configured rules
  phase3_move_threshold.csv    3% / 4% / 5%: frequency, cross-sectional sigma, fwd returns by ratio_10 state
Forward returns use the official TASI from 2007-01-06; earlier signals are listed but unscored.
Overlapping windows: counts of signals are honest, the return stats are descriptive.
"""
from __future__ import annotations

import itertools
import json

import numpy as np
import pandas as pd

from breadth import ROOT, load_config
from breadth.backtest.forward import forward_returns
from breadth.indicators.common import build_panels
from breadth.indicators.momentum import compute_momentum
from breadth.indicators.thrusts import first_of_run, low_to_high, pct50_thrust, ratio_thrust, zweig

H = (20, 60, 120)


def score_signals(sig: pd.Series, fwd: pd.DataFrame, label: dict) -> dict:
    d = sig[sig].index
    row = dict(label); row["n_signals"] = int(len(d)); row["n_scored"] = int(fwd.reindex(d)["fwd_20"].notna().sum())
    row["first"] = str(d.min().date()) if len(d) else ""; row["last"] = str(d.max().date()) if len(d) else ""
    for h in H:
        x = fwd.reindex(d)[f"fwd_{h}"].dropna()
        row[f"mean_{h}"] = round(x.mean(), 2) if len(x) else np.nan
        row[f"hit_{h}"] = round((x > 0).mean(), 2) if len(x) else np.nan
        row[f"base_{h}"] = round(fwd[f"fwd_{h}"].mean(), 2)
    return row


def run(cfg: dict) -> dict:
    st = ROOT / cfg["paths"]["storage_dir"]
    ad = pd.read_parquet(st / "features" / "advance_decline.parquet").set_index("date")
    part = pd.read_parquet(st / "features" / "participation.parquet").set_index("date")
    idx = pd.read_parquet(st / "clean" / "index.parquet")
    tasi = idx[idx["index_code"] == "TASI"].set_index("date")["close"]
    fwd = pd.DataFrame({f"fwd_{h}": 100 * (tasi.shift(-h) / tasi - 1) for h in H})
    t = cfg["thrusts"]; g = t["grid"]; cd = t["cooldown_days"]
    rows = []
    for lo, hi in itertools.product(g["zweig_low"], g["zweig_high"]):
        z = zweig(ad, t["zweig"]["ema_span"], lo, hi, t["zweig"]["window"], cd)
        rows.append(score_signals(z["zweig_signal"], fwd, {"rule": "zweig", "p1": lo, "p2": hi}))
    for r in g["whaley_ratio"]:
        rows.append(score_signals(ratio_thrust(ad, t["whaley"]["days"], r, cd, "w")["w_signal"], fwd, {"rule": "whaley_5d", "p1": r, "p2": ""}))
    for r in g["deemer_ratio"]:
        rows.append(score_signals(ratio_thrust(ad, t["deemer"]["days"], r, cd, "d")["d_signal"], fwd, {"rule": "deemer_10d", "p1": r, "p2": ""}))
    for hi, w in itertools.product(g["pct50_high"], g["pct50_window"]):
        s = pct50_thrust(part.reindex(ad.index), t["pct50_thrust"]["low"], hi, w, cd)["pct50_thrust_signal"]
        rows.append(score_signals(s, fwd, {"rule": "pct50_thrust", "p1": hi, "p2": w}))
    for th in g["lowry_threshold"]:
        up = first_of_run((ad["up_volume_pct"] >= th) & (ad["up_points_pct"] >= th), cd)
        dn = first_of_run((ad["up_volume_pct"] <= 1 - th) & (ad["up_points_pct"] <= 1 - th), cd)
        rows.append(score_signals(up, fwd, {"rule": "lowry_up", "p1": th, "p2": ""}))
        rows.append(score_signals(dn, fwd, {"rule": "lowry_down", "p1": th, "p2": ""}))
    grid = pd.DataFrame(rows)
    # signals per decade on the scored period (2007+), for the rarity criterion
    years = (fwd["fwd_20"].dropna().index.max() - fwd["fwd_20"].dropna().index.min()).days / 365.25
    grid["per_decade"] = (10 * grid["n_scored"] / years).round(1)
    grid.to_csv(st / "validation" / "phase3_thrust_grid.csv", index=False)

    # ---- events of the configured rules
    from breadth.indicators.thrusts import compute_thrusts
    th = compute_thrusts(ad, part, cfg)
    ev = []
    for col in [c for c in th.columns if c.endswith("_signal")]:
        for d in th.index[th[col].fillna(False)]:
            r = {"rule": col.replace("_signal", ""), "date": d.date(), "tasi": tasi.get(d, np.nan)}
            for h in H:
                r[f"fwd_{h}"] = round(fwd[f"fwd_{h}"].get(d, np.nan), 2)
            ev.append(r)
    events = pd.DataFrame(ev).sort_values(["rule", "date"])
    events.to_csv(st / "validation" / "phase3_thrust_events.csv", index=False)

    # ---- move threshold test (3/4/5%)
    prices = pd.read_parquet(st / "clean" / "prices.parquet")
    master = pd.read_parquet(st / "master" / "symbols.parquet")
    p = build_panels(prices, set(master.loc[master["in_universe"], "symbol"]))
    r = p.ret
    absr = r.abs().stack()
    xs_sigma = r.std(axis=1)                          # cross-sectional dispersion per day
    ts_sigma = r.std(axis=0)                          # per-stock daily sigma
    mrows = []
    for thr in cfg["momentum"]["test_thresholds"]:
        mom = compute_momentum(p, cfg, thr)
        row = {"threshold": thr,
               "pct_stock_days_abs_ge": round(100 * (absr >= thr).mean(), 2),
               "median_up_x_per_day": float(mom["up_x"].loc["2010":].median()),
               "median_down_x_per_day": float(mom["down_x"].loc["2010":].median()),
               "days_zero_up_and_down_pct": round(100 * ((mom["up_x"] == 0) & (mom["down_x"] == 0)).loc["2010":].mean(), 1)}
        # predictive separation of ratio_10 buckets on fwd 20
        st10 = pd.cut(mom["ratio_10"], [-np.inf, 0.5, 1, 2, np.inf], labels=["<0.5", "0.5-1", "1-2", ">2"])
        j = fwd.join(st10.rename("s"), how="inner").dropna(subset=["s"])
        for lab, gg in j.groupby("s", observed=True):
            row[f"fwd20_ratio10_{lab}"] = round(gg["fwd_20"].mean(), 2)
            row[f"n_ratio10_{lab}"] = int(gg["fwd_20"].notna().sum())
        row["fwd20_spread_hi_minus_lo"] = round(row.get("fwd20_ratio10_>2", np.nan) - row.get("fwd20_ratio10_<0.5", np.nan), 2)
        mrows.append(row)
    mt = pd.DataFrame(mrows)
    mt.to_csv(st / "validation" / "phase3_move_threshold.csv", index=False)
    vol = {"median_stock_daily_sigma_pct": round(100 * ts_sigma.median(), 2),
           "median_xs_sigma_pct": round(100 * xs_sigma.loc["2010":].median(), 2),
           "pct_abs_ret_ge_3": round(100 * (absr >= 0.03).mean(), 2),
           "pct_abs_ret_ge_4": round(100 * (absr >= 0.04).mean(), 2),
           "pct_abs_ret_ge_5": round(100 * (absr >= 0.05).mean(), 2)}
    (st / "validation" / "phase3_volatility.json").write_text(json.dumps(vol, indent=2))
    return {"grid_rows": len(grid), "events": len(events), "volatility": vol}


if __name__ == "__main__":
    print(json.dumps(run(load_config()), indent=2, default=str))
