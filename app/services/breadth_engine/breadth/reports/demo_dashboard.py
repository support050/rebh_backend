"""Demo copy of the dashboard with SYNTHETIC data (for layout review only — no TASI data inside).

python -m breadth.reports.demo_dashboard -> storage/reports/breadth_dashboard_DEMO.html
Every series is generated from random walks / noise with plausible ranges; dates are a synthetic
trading calendar; sector and stock names are generic. A banner marks the file as a demo.
"""
from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd

from breadth import ROOT
from breadth.reports.pro_dashboard import TEMPLATE

rng = np.random.default_rng(7)


def walk(n, start, vol, drift=0.0, lo=None, hi=None, theta=0.03):
    """Mean-reverting (OU) path around `start`, clipped to [lo, hi]."""
    x = np.empty(n); x[0] = start
    for i in range(1, n):
        x[i] = x[i - 1] + theta * (start - x[i - 1]) + rng.normal(drift, vol)
    if lo is not None or hi is not None:
        x = np.clip(x, lo, hi)
    return x


def build() -> str:
    n = 2500
    dates = pd.bdate_range(end="2026-09-24", periods=n)
    D = {"dates": [d.strftime("%Y-%m-%d") for d in dates]}
    S = {}
    px = 8000 * np.exp(np.cumsum(rng.normal(0.0002, 0.011, n)))
    S["tasi"] = px.round(2).tolist()
    S["tasi_o"] = (px * (1 + rng.normal(0, 0.004, n))).round(2).tolist()
    S["tasi_h"] = (np.maximum(px, S["tasi_o"]) * (1 + abs(rng.normal(0, 0.005, n)))).round(2).tolist()
    S["tasi_l"] = (np.minimum(px, S["tasi_o"]) * (1 - abs(rng.normal(0, 0.005, n)))).round(2).tolist()
    S["tasi_vol"] = (rng.lognormal(18.5, 0.3, n)).round(0).tolist()
    S["ew"] = (7000 * np.exp(np.cumsum(rng.normal(0.00025, 0.010, n)))).round(2).tolist()
    ser = pd.Series(px)
    S["tasi_ema21"] = ser.ewm(span=21).mean().round(2).tolist(); S["tasi_sma50"] = ser.rolling(50).mean().round(2).tolist(); S["tasi_sma200"] = ser.rolling(200).mean().round(2).tolist()
    S["tasi_chg"] = ser.pct_change().round(4).tolist()
    for k, lo, hi in [("pct_above_ema10", 5, 95), ("pct_above_sma20", 5, 95), ("pct_above_sma50", 5, 95), ("pct_above_sma150", 10, 90), ("pct_above_sma200", 10, 90), ("pct_stage2", 5, 80), ("pct_stage4", 5, 80), ("pct_minervini", 0, 35), ("hl_index", 0, 1), ("pct_healthy", 0, 100), ("median_score", 15, 85), ("score", 5, 95), ("score_smooth", 5, 95), ("overlay", 10, 90), ("A_participation", 5, 95), ("B_advance_decline", 5, 95), ("E_sectors", 5, 95), ("pct_stage1", 5, 40), ("pct_stage3", 2, 25), ("up_volume_pct", 0.1, 0.9), ("top5_share", 0.05, 0.8)]:
        S[k] = walk(n, (lo + hi) / 2, (hi - lo) * 0.05, lo=lo, hi=hi, theta=0.02).round(2).tolist()
    S["score_smooth"] = pd.Series(S["score"]).ewm(span=5).mean().round(2).tolist()
    nh = np.clip(rng.poisson(6, n) * (rng.random(n) < 0.7), 0, 80); nl = np.clip(rng.poisson(6, n) * (rng.random(n) < 0.7), 0, 120)
    S["new_highs"] = nh.tolist(); S["new_lows"] = nl.tolist(); net = pd.Series(nh - nl); S["net_nh_nl"] = net.tolist()
    S["nhnl_sum5"] = net.rolling(5).sum().tolist(); S["nhnl_sum10"] = net.rolling(10).sum().tolist(); S["nhnl_thr"] = [-180.0] * n; S["n_nhnl"] = [240] * n; S["n_sma200"] = [243] * n
    A = rng.integers(40, 200, n); Dd = 250 - A - rng.integers(2, 12, n)
    S["advances"] = A.tolist(); S["declines"] = Dd.tolist(); S["ad_net"] = (A - Dd).tolist(); S["ad_line"] = np.cumsum(A - Dd).tolist(); S["ad_ratio"] = (A / Dd).round(2).tolist()
    S["ud_volume_ratio"] = np.clip(rng.lognormal(0, 0.6, n), 0.1, 6).round(2).tolist(); S["net_up_volume"] = rng.normal(0, 4e7, n).round(0).tolist()
    rana = pd.Series(1000 * (A - Dd) / (A + Dd)); S["rana"] = rana.round(1).tolist()
    osc = rana.ewm(span=19).mean() - rana.ewm(span=39).mean(); S["mcclellan_osc"] = osc.round(1).tolist(); S["mcclellan_sum"] = osc.cumsum().round(1).tolist()
    adn = pd.Series(A - Dd); S["mcclellan_trad"] = (adn.ewm(span=19).mean() - adn.ewm(span=39).mean()).round(1).tolist()
    S["trin"] = np.clip(rng.lognormal(0, 0.4, n), 0.2, 4).round(2).tolist(); S["trin_10"] = pd.Series(S["trin"]).rolling(10).mean().round(2).tolist(); S["trin_open10"] = S["trin_10"]
    S["zweig_value"] = pd.Series(A / (A + Dd)).ewm(span=10).mean().round(3).tolist(); S["whaley_value"] = (pd.Series(A).rolling(5).sum() / pd.Series(Dd).rolling(5).sum()).round(2).tolist(); S["deemer_value"] = (pd.Series(A).rolling(10).sum() / pd.Series(Dd).rolling(10).sum()).round(2).tolist()
    up = rng.poisson(5, n); dn = rng.poisson(5, n)
    S["up_x"] = up.tolist(); S["down_x"] = dn.tolist(); S["ratio_5"] = (pd.Series(up).rolling(5).sum() / pd.Series(dn).rolling(5).sum().replace(0, np.nan)).round(2).tolist(); S["ratio_10"] = (pd.Series(up).rolling(10).sum() / pd.Series(dn).rolling(10).sum().replace(0, np.nan)).round(2).tolist()
    for k in ["up_25_q", "down_25_q"]: S[k] = rng.poisson(12, n).tolist()
    for k in ["up_25_m", "down_25_m", "up_50_m", "down_50_m"]: S[k] = rng.poisson(1, n).tolist()
    for k in ["up_13_34", "down_13_34"]: S[k] = rng.poisson(15, n).tolist()
    S["pct_up_x"] = (100 * up / 250).round(1).tolist(); S["pct_down_x"] = (100 * dn / 250).round(1).tolist(); S["n_mom"] = [250] * n
    S["n_sectors"] = [18] * n; S["n_healthy"] = rng.integers(0, 12, n).tolist(); S["n_weak"] = rng.integers(0, 12, n).tolist()
    sp = pd.Series(S["ew"]) / pd.Series(S["tasi"]); S["ratio"] = (sp / sp.iloc[0]).round(3).tolist(); S["ratio_ma"] = pd.Series(S["ratio"]).rolling(50).mean().round(3).tolist()
    S["spread_20"] = (100 * (pd.Series(S["ew"]).pct_change(20) - pd.Series(S["tasi"]).pct_change(20))).round(2).tolist(); S["spread_60"] = (100 * (pd.Series(S["ew"]).pct_change(60) - pd.Series(S["tasi"]).pct_change(60))).round(2).tolist()
    S["top5_contrib"] = rng.normal(0, 0.4, n).round(2).tolist(); S["cw_proxy_ret"] = rng.normal(0, 0.8, n).round(2).tolist()
    S["up_from_open"] = rng.integers(60, 190, n).tolist(); S["down_from_open"] = (250 - np.array(S["up_from_open"])).tolist(); S["up_on_volume"] = rng.integers(5, 60, n).tolist(); S["down_on_volume"] = rng.integers(5, 60, n).tolist()
    st = pd.DataFrame(rng.dirichlet([2, 3, 1, 3], n)).rolling(15, min_periods=1).mean().to_numpy(); st = st / st.sum(axis=1, keepdims=True) * 245; S["stage1"], S["stage2"], S["stage3"], S["stage4"] = [st[:, k].round(0).tolist() for k in range(4)]; S["n_stage"] = [245] * n
    for k in range(1, 5): S[f"pct_stage{k}"] = (100 * st[:, k - 1] / 245).round(1).tolist()
    sc = pd.Series(S["score_smooth"]); reg = np.where(sc < 40, "Weak", np.where(sc < 60, "Neutral", np.where(sc < 75, "Healthy", "Strong"))); S["regime"] = reg.tolist()
    cyc = []; c = 0; sma = ser.rolling(21).mean()
    for i in range(n):
        a = px[i] > sma.iloc[i] if i >= 20 else True
        c = (c + 1 if c > 0 else 1) if a else (c - 1 if c < 0 else -1); cyc.append(c)
    S["dv_cycle"] = cyc
    D["series"] = S
    pick = lambda k: [D["dates"][i] for i in sorted(rng.choice(n, k, replace=False))]
    E = {"zweig": pick(12), "whaley": pick(10), "pct50": pick(9), "lowry_down": pick(8), "bear_div": pick(15), "bull_div": pick(6), "day90_down": pick(20), "day90_up": pick(20), "dv_stress": pick(14)}
    E["regime_changes"] = [{"date": D["dates"][i], "regime": reg[i]} for i in range(1, n) if reg[i] != reg[i - 1]]
    eps = []
    for s0 in sorted(rng.choice(n - 120, 8, replace=False)):
        e0 = s0 + int(rng.integers(20, 110)); seg = px[s0:e0 + 1]
        eps.append({"start": D["dates"][s0], "end": D["dates"][e0], "c0": round(px[s0], 2), "c1": round(px[e0], 2), "pct": round(100 * (px[e0] / px[s0] - 1), 2), "days": e0 - s0 + 1, "max_read": int(rng.integers(-140, -40)), "sum5": int(rng.integers(-420, -180)), "low": round(seg.min(), 2), "dd": round(100 * (seg.min() / px[s0] - 1), 2)})
    E["canary"] = eps; D["events"] = E
    D["signals_table"] = sorted([{"signal": s_, "date": d, "tasi": round(float(px[D["dates"].index(d)]), 0), "f5": round(rng.normal(0.3, 2), 1), "f20": round(rng.normal(1, 4), 1), "f60": round(rng.normal(2, 7), 1), "f120": round(rng.normal(3, 10), 1)} for s_, k in [("Zweig", "zweig"), ("Whaley 5d", "whaley"), ("%>50DMA thrust", "pct50"), ("Lowry 95% down", "lowry_down")] for d in E[k]], key=lambda r: r["date"], reverse=True)
    sectors = [f"Sector {chr(65 + k)}" for k in range(18)]
    D["sectors"] = {"dates": [d.strftime("%m-%d") for d in dates[-30:]], "rows": [{"sector": s_, "scores": walk(30, 50, 4, lo=5, hi=95).round(0).tolist(), "n": int(rng.integers(5, 40)), "pct50": float(rng.integers(0, 100)), "pct200": float(rng.integers(0, 100)), "nhnl": float(rng.integers(-30, 30)), "ad10": float(rng.integers(20, 80)), "mom10": float(rng.integers(0, 100))} for s_ in sectors]}
    per = ["Today", "1W", "1M", "3M", "6M", "12M", "YTD"]
    rows = []
    for s_ in sectors:
        r = {"name": s_, **{p_: round(float(rng.normal(0, [0.8, 2, 4, 7, 10, 15, 12][i])), 2) for i, p_ in enumerate(per)}, "stocks": int(rng.integers(5, 40))}
        rows.append(r)
    for p_ in per:
        order = sorted(range(len(rows)), key=lambda i: -rows[i][p_])
        for rk, i in enumerate(order, 1):
            rows[i].setdefault("rank", {})[p_] = rk; rows[i].setdefault("rank_chg", {})[p_] = int(rng.integers(-5, 6))
    D["sector_perf"] = {"periods": per, "rows": rows, "market": {p_: round(float(rng.normal(0, 3)), 2) for p_ in per}}
    D["stocks"] = {s_: [{"sym": 1000 + 10 * k + j, "name": f"Company {chr(65+k)}{j+1}", "close": round(float(rng.uniform(5, 200)), 2), "d1": round(float(rng.normal(0, 1.5)), 2), "m1": round(float(rng.normal(0, 6)), 1), "m3": round(float(rng.normal(0, 10)), 1), "vs50": round(float(rng.normal(0, 6)), 1), "vs200": round(float(rng.normal(0, 10)), 1), "hi52": round(float(-abs(rng.normal(10, 10))), 1), "lo52": round(float(abs(rng.normal(15, 12))), 1), "stage": int(rng.integers(1, 5)), "rvol": round(float(rng.lognormal(0, 0.4)), 2), "traded": True} for j in range(int(rng.integers(5, 14)))] for k, s_ in enumerate(sectors)}
    D["sector_pool"] = {s_: s_ for s_ in sectors}
    D["thresholds"] = [{"indicator": nm, "p2": -2, "p5": -1, "p95": 1, "p98": 2, "us": "demo"} for nm in ["NH−NL daily net", "NH−NL 5-day sum", "McClellan Oscillator (ratio-adj.)", "McClellan Summation", "TRIN-10", "%>200DMA", "%>50DMA"]]
    D["today"] = {"date": D["dates"][-1], "score": round(float(S["score"][-1]), 1), "score_chg": round(float(S["score"][-1] - S["score"][-2]), 1), "smooth": round(float(S["score_smooth"][-1]), 1), "regime": reg[-1], "driver": "B advance decline", "overlay": round(float(S["overlay"][-1]), 1), "tasi": round(float(px[-1]), 2), "tasi_chg": round(100 * float(px[-1] / px[-2] - 1), 2), "regime_since": E["regime_changes"][-1]["date"], "n_universe": 250, "fam": {"A": round(float(S["A_participation"][-1])), "B": round(float(S["B_advance_decline"][-1])), "E": round(float(S["E_sectors"][-1]))}}
    def conv(o):
        if isinstance(o, (np.integer,)): return int(o)
        if isinstance(o, (np.floating,)): return None if np.isnan(o) else float(o)
        if isinstance(o, float) and np.isnan(o): return None
        raise TypeError(str(type(o)))
    txt = json.dumps(D, separators=(",", ":"), default=conv).replace("NaN", "null")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/", "const DATA=" + txt + ";")
    html = html.replace("<title>TASI Market Breadth</title>", "<title>Market Breadth — DEMO (synthetic data)</title>")
    html = html.replace("<h1>TASI Market Breadth</h1>", "<h1>Market Breadth — DEMO</h1><span class='badge' style='background:var(--critical)'>SYNTHETIC DATA — layout review only</span>")
    out = ROOT / "storage" / "reports" / "breadth_dashboard_DEMO.html"; out.write_text(html, encoding="utf-8")
    return str(out)


if __name__ == "__main__":
    print(build())
