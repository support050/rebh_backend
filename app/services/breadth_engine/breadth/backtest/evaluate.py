"""Phase 6 — final evaluation table for every indicator state and the composite.

python -m breadth.backtest.evaluate
For each (indicator, state): n, TASI forward mean at 5/10/20/60, hit rates, average max-drawdown
within 60 days, split IS (<= 2017-12-31) / OOS (2018+), plus #signals and false-signal rate for
event-type signals (thrusts, Lowry, Washout entries). Output: storage/validation/phase6_indicator_table.csv
Verdict column: KEEP / CONTEXT (shown, not scored) / DROP — from the numbers, stated in the report.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from breadth import ROOT, load_config
from breadth.backtest.forward import bucket
from breadth.backtest.walkforward import max_drawdown_within

H = (5, 10, 20, 60)
SPLIT = pd.Timestamp("2017-12-31")


def load_all(cfg):
    st = ROOT / cfg["paths"]["storage_dir"]
    F = {n: pd.read_parquet(st / "features" / f"{n}.parquet").set_index("date") for n in
         ["participation", "advance_decline", "thrusts", "momentum", "sectors_summary", "concentration", "divergences", "composite", "composite_walkforward"]}
    idx = pd.read_parquet(st / "clean" / "index.parquet")
    tasi = idx[idx["index_code"] == "TASI"].set_index("date")["close"]
    return F, tasi, st


def states(F: dict) -> dict[str, tuple[pd.Series, str]]:
    p, ad, th, mom, sec, conc, div, comp, wf = (F[k] for k in
        ["participation", "advance_decline", "thrusts", "momentum", "sectors_summary", "concentration", "divergences", "composite", "composite_walkforward"])
    S = {}
    S["A %>200DMA"] = (bucket(p["pct_above_sma200"], [20, 40, 60, 80], ["<20", "20-40", "40-60", "60-80", ">80"]), "KEEP")
    S["A %>50DMA"] = (bucket(p["pct_above_sma50"], [20, 40, 60, 80], ["<20", "20-40", "40-60", "60-80", ">80"]), "KEEP")
    S["A High-Low Index"] = (bucket(p["hl_index"], [0.3, 0.7], ["<0.3", "0.3-0.7", ">0.7"]), "KEEP")
    S["A Net NH-NL"] = (bucket(p["net_nh_nl"], [-20, 0, 20], ["<-20", "-20..0", "0..20", ">20"]), "KEEP")
    S["A %Stage2 (Weinstein)"] = (bucket(p["pct_stage2"], [20, 40, 60, 80], ["<20", "20-40", "40-60", "60-80", ">80"]), "CONTEXT")
    S["A %Minervini"] = (bucket(p["pct_minervini"], [5, 10, 20], ["<5", "5-10", "10-20", ">20"]), "DROP")
    S["B McClellan Osc"] = (bucket(ad["mcclellan_osc"], [-100, 0, 100], ["<-100", "-100..0", "0..100", ">100"]), "KEEP")
    S["B Summation sign"] = (np.sign(ad["mcclellan_sum"]).map({-1: "neg", 1: "pos"}), "DROP")
    S["B A/D line 20d chg"] = (np.sign(ad["ad_line"].diff(20)).map({-1: "falling", 1: "rising"}), "KEEP")
    S["B TRIN-10"] = (bucket(ad["trin_10"], [0.8, 1.2], ["<0.8", "0.8-1.2", ">1.2"]), "KEEP (overlay)")
    S["C Zweig signal"] = (th["zweig_signal"].map({True: "signal"}), "KEEP (overlay)")
    S["C Whaley signal"] = (th["whaley_signal"].map({True: "signal"}), "KEEP (overlay)")
    S["C Deemer signal"] = (th["deemer_signal"].map({True: "signal"}), "DROP")
    S["C %>50DMA thrust"] = (th["pct50_thrust_signal"].map({True: "signal"}), "KEEP (overlay)")
    S["C Lowry 95% down"] = (th["lowry_down_signal"].map({True: "signal"}), "KEEP (overlay)")
    S["C Lowry 95% up"] = (th["lowry_up_signal"].map({True: "signal"}), "DROP")
    S["D ratio_10 (4%)"] = (bucket(mom["ratio_10"], [0.5, 1, 2, 4], ["<0.5", "0.5-1", "1-2", "2-4", ">4"]), "KEEP (overlay, contrarian)")
    S["D %up>=4% day"] = (bucket(mom["pct_up_x"], [2, 5, 10, 20], ["<2", "2-5", "5-10", "10-20", ">20"]), "CONTEXT (exhaustion >20)")
    S["D %down>=4% day"] = (bucket(mom["pct_down_x"], [2, 5, 10, 20], ["<2", "2-5", "5-10", "10-20", ">20"]), "KEEP (overlay)")
    S["D net 25%/quarter"] = (bucket(mom["up_25_q"] - mom["down_25_q"], [-20, 0, 20], ["<-20", "-20..0", "0..20", ">20"]), "CONTEXT")
    S["E % healthy sectors"] = (bucket(sec["pct_healthy"], [10, 30, 50, 70], ["<10", "10-30", "30-50", "50-70", ">70"]), "KEEP")
    S["E median sector score"] = (bucket(sec["median_score"], [30, 45, 60, 75], ["<30", "30-45", "45-60", "60-75", ">75"]), "KEEP")
    S["F EW-TASI 60d"] = (bucket(conc["spread_60"], [-5, -2, 2, 5], ["<-5", "-5..-2", "-2..2", "2..5", ">5"]), "CONTEXT (dropped from score)")
    S["G index 60d high confirmation"] = (div["n_bearish"].where(div["index_high"]).map(lambda v: "confirmed" if v == 0 else ("1 div" if v == 1 else "2+ div") if pd.notna(v) else np.nan), "CONTEXT (dropped from score)")
    S["G bullish divergence signal"] = (div["bullish_signal"].map({True: "signal"}), "DROP")
    S["COMPOSITE regime (in-sample fit)"] = (comp["regime"], "KEEP")
    S["COMPOSITE regime (walk-forward)"] = (wf["regime"], "KEEP")
    S["COMPOSITE Washout entry"] = ((wf["regime"] == "Washout") & (wf["regime"].shift() != "Washout"), "KEEP")
    S["COMPOSITE Washout entry"] = (S["COMPOSITE Washout entry"][0].map({True: "signal"}), "KEEP")
    S["COMPOSITE score quintile (walk-forward)"] = (pd.qcut(wf["score"], 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"]), "KEEP")
    return S


def run(cfg):
    F, tasi, st = load_all(cfg)
    fwd = pd.DataFrame({f"f{h}": 100 * (tasi.shift(-h) / tasi - 1) for h in H}); fwd["dd60"] = max_drawdown_within(tasi, 60)
    rows = []
    for name, (state, verdict) in states(F).items():
        d = fwd.join(state.rename("s"), how="inner").dropna(subset=["s", "f60"])
        for per, m in [("IS", d.index <= SPLIT), ("OOS", d.index > SPLIT), ("ALL", np.ones(len(d), bool))]:
            x = d[m]
            for s_, g in x.groupby("s", observed=True):
                r = {"indicator": name, "verdict": verdict, "period": per, "state": str(s_), "n": len(g)}
                for h in H:
                    r[f"mean_{h}"] = round(g[f"f{h}"].mean(), 2); r[f"hit_{h}"] = round((g[f"f{h}"] > 0).mean(), 2)
                r["avg_dd60"] = round(g["dd60"].mean(), 2)
                r["false_20"] = round((g["f20"] < -5).mean(), 2)     # "false" for bullish states: >5% loss within 20d
                r["base_20"] = round(x["f20"].mean(), 2); r["base_60"] = round(x["f60"].mean(), 2)
                rows.append(r)
    t = pd.DataFrame(rows); t.to_csv(st / "validation" / "phase6_indicator_table.csv", index=False)
    return {"rows": len(t)}


if __name__ == "__main__":
    print(json.dumps(run(load_config())))
