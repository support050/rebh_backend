"""NH-NL "canary" study on TASI — replication of Hisham Naser's US study (NYSE+NASDAQ, 2005-2025).

Rule (his): NH-NL falls below zero, stays negative 5 consecutive days AND the 5-day sum reaches
-180/-200 (≈3% of ~6,000 issues) -> warning. Episode ends when NH-NL prints 5 consecutive positive days.
Adapted to TASI (≈240 eligible names): thresholds expressed as % of the eligible universe and
calibrated on 2007-2026 to match his rarity (~13-15 episodes in 20 years):
  start_T : net_T < 0 for 5 consecutive days AND sum_5(net) <= -theta * mean_5(n_eligible)
  end_T   : net_T > 0 for 5 consecutive days
Outputs (storage/reports/canary/):
  canary_episodes.xlsx / .csv   his table: start/end dates & closes, change, %, trading days, max reading,
                                lowest low, $ max DD, % max DD (+ 2 rows of averages)
  canary_charts.pdf             one chart per episode in his layout: TASI + NH-NL histogram + 5-day sum
  canary_thresholds.csv         TASI extreme readings for NH-NL, McClellan osc/summation (percentiles)
  canary_calibration.csv        the threshold sweep
"""
from __future__ import annotations

import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages

from breadth import ROOT, load_config

BLUE, ORANGE, RED, GREY = "#2a78d6", "#eb6834", "#e34948", "#52514e"


def load(cfg):
    st = ROOT / cfg["paths"]["storage_dir"]
    part = pd.read_parquet(st / "features" / "participation.parquet").set_index("date")
    ad = pd.read_parquet(st / "features" / "advance_decline.parquet").set_index("date")
    idx = pd.read_parquet(st / "clean" / "index.parquet")
    tasi = idx[idx["index_code"] == "TASI"].set_index("date")[["open", "high", "low", "close"]]
    return part, ad, tasi, st


def episodes(net: pd.Series, n: pd.Series, tasi_close: pd.Series, theta: float, single_day: float | None = None):
    sum5 = net.rolling(5).sum(); n5 = n.rolling(5).mean()
    neg5 = (net < 0).rolling(5).sum() == 5
    pos5 = (net > 0).rolling(5).sum() == 5
    start = neg5 & (sum5 <= -theta / 100 * n5)
    if single_day is not None:
        start = neg5 & (net <= -single_day / 100 * n)
    eps, active, s = [], False, None
    for d in net.index[net.index >= tasi_close.index.min()]:
        if not active and bool(start[d]):
            active, s = True, d
        elif active and bool(pos5[d]):
            eps.append((s, d)); active = False
    if active:
        eps.append((s, net.index[-1]))
    return eps


def table(eps, net, tasi):
    rows = []
    for k, (s, e) in enumerate(eps, 1):
        seg = tasi.loc[s:e]
        if seg.empty or seg["close"].isna().all():        # episode started after the last available index close
            rows.append({"#": k, "Start Date": s.date(), "Close": np.nan, "End Date": e.date(), "Close ": np.nan, "Change in Points": np.nan, "% Change": np.nan,
                         "No. of Trading Days": int(((net.index >= s) & (net.index <= e)).sum()), "Max Reading": int(net.loc[s:e].min()),
                         "Max Reading (5d sum)": int(net.rolling(5).sum().loc[s:e].min()), "Lowest Low": np.nan, "$ MAX DD": np.nan, "% MAX DD": np.nan})
            continue
        c0, c1 = seg["close"].iloc[0], seg["close"].iloc[-1]
        low = seg["low"].fillna(seg["close"]).min()
        rows.append({"#": k, "Start Date": s.date(), "Close": round(c0, 2), "End Date": e.date(), "Close ": round(c1, 2),
                     "Change in Points": round(c1 - c0, 2), "% Change": round(100 * (c1 / c0 - 1), 2),
                     "No. of Trading Days": len(seg), "Max Reading": int(net.loc[s:e].min()),
                     "Max Reading (5d sum)": int(net.rolling(5).sum().loc[s:e].min()),
                     "Lowest Low": round(low, 2), "$ MAX DD": round(low - c0, 2), "% MAX DD": round(100 * (low / c0 - 1), 2)})
    return pd.DataFrame(rows)


def chart(ax_price, ax_nh, ax_sum, tasi, net, s, e, title):
    pad = pd.Timedelta(days=120)
    w = tasi.loc[s - pad: e + pad]
    ax_price.plot(w.index, w["close"], color="black", lw=1)
    ax_price.set_title(title, fontsize=10, loc="left")
    for d, col in ((s, BLUE), (e, BLUE)):
        for ax in (ax_price, ax_nh, ax_sum):
            ax.axvline(d, color=col, lw=0.8, ls="--")
    c0, c1 = tasi.loc[s, "close"], tasi.loc[e, "close"]
    ax_price.annotate(f"{s.date()}\n{c0:,.0f}", (s, c0), textcoords="offset points", xytext=(-40, 15), fontsize=7, color=BLUE)
    ax_price.annotate(f"{e.date()}\n{c1:,.0f} ({100*(c1/c0-1):+.1f}%)", (e, c1), textcoords="offset points", xytext=(5, -25), fontsize=7, color=BLUE)
    seg = tasi.loc[s:e]; lo_d = seg["close"].idxmin()
    ax_price.annotate(f"low {seg['close'].min():,.0f} ({100*(seg['close'].min()/c0-1):+.1f}%)", (lo_d, seg["close"].min()), textcoords="offset points", xytext=(5, -12), fontsize=7, color=RED)
    nn = net.loc[w.index]
    ax_nh.bar(nn.index, nn.values, color=np.where(nn.values < 0, RED, BLUE), width=1.0)
    ax_nh.axhline(0, color=GREY, lw=0.6); ax_nh.set_ylabel("NH−NL", fontsize=7)
    s5 = net.rolling(5).sum().loc[w.index]
    ax_sum.bar(s5.index, s5.values, color=np.where(s5.values < 0, RED, BLUE), width=1.0)
    ax_sum.axhline(0, color=GREY, lw=0.6); ax_sum.set_ylabel("5-day sum", fontsize=7)
    for ax in (ax_price, ax_nh, ax_sum):
        ax.tick_params(labelsize=7); ax.grid(True, color="#e6e5e1", lw=0.5)


def run(cfg: dict, theta: float = 75.0) -> dict:
    part, ad, tasi, st = load(cfg)
    net, n = part["net_nh_nl"], part["n_nhnl"]
    out = st / "reports" / "canary"; out.mkdir(parents=True, exist_ok=True)
    # ---- calibration sweep
    rows = []
    for th in (10, 25, 50, 75, 100, 125, 150):
        t = table(episodes(net, n, tasi["close"], th), net, tasi)
        rows.append({"rule": f"5 neg days & 5d sum <= -{th}% of universe", "episodes": len(t), "avg_pct_change": round(t["% Change"].mean(), 2),
                     "avg_max_dd": round(t["% MAX DD"].mean(), 2), "share_dd_le_3": round((t["% MAX DD"] <= -3).mean(), 2),
                     "share_dd_le_10": round((t["% MAX DD"] <= -10).mean(), 2), "avg_days": round(t["No. of Trading Days"].mean(), 0)})
    for sd in (20, 25, 30):
        t = table(episodes(net, n, tasi["close"], 0, single_day=sd), net, tasi)
        rows.append({"rule": f"5 neg days & single-day net <= -{sd}% of universe", "episodes": len(t), "avg_pct_change": round(t["% Change"].mean(), 2),
                     "avg_max_dd": round(t["% MAX DD"].mean(), 2), "share_dd_le_3": round((t["% MAX DD"] <= -3).mean(), 2),
                     "share_dd_le_10": round((t["% MAX DD"] <= -10).mean(), 2), "avg_days": round(t["No. of Trading Days"].mean(), 0)})
    pd.DataFrame(rows).to_csv(out / "canary_calibration.csv", index=False)
    # ---- chosen rule
    eps = episodes(net, n, tasi["close"], theta)
    t = table(eps, net, tasi)
    avg = {c: round(t[c].mean(), 2) for c in ["% Change", "No. of Trading Days", "Max Reading", "$ MAX DD", "% MAX DD"]}
    big = t[t["% MAX DD"] <= -20]["#"].tolist()
    t_ex = t[~t["#"].isin(big)]
    avg_ex = {c: round(t_ex[c].mean(), 2) for c in ["% Change", "No. of Trading Days", "Max Reading", "$ MAX DD", "% MAX DD"]}
    t.to_csv(out / "canary_episodes.csv", index=False)
    with pd.ExcelWriter(out / "canary_episodes.xlsx") as w:
        t.to_excel(w, sheet_name="episodes", index=False)
        pd.DataFrame([{"row": "Avg.", **avg}, {"row": f"Avg. (excl. episodes {big} with DD <= -20%)", **avg_ex}]).to_excel(w, sheet_name="averages", index=False)
        pd.DataFrame(rows).to_excel(w, sheet_name="calibration", index=False)
    # ---- charts PDF
    with PdfPages(out / "canary_charts.pdf") as pdf:
        for k, (s, e) in enumerate(eps, 1):
            fig, axes = plt.subplots(3, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1, 1]})
            chart(*axes, tasi, net, s, e, f"TASI with NH−NL (52-week) — episode {k}: {s.date()} → {e.date()}")
            fig.tight_layout(); pdf.savefig(fig); plt.close(fig)
        # overview page: full history with episodes shaded
        fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
        axes[0].plot(tasi.index, tasi["close"], color="black", lw=0.8); axes[0].set_title("TASI 2007–2026 — canary episodes shaded", fontsize=10, loc="left")
        for s, e in eps:
            axes[0].axvspan(s, e, color=RED, alpha=0.15)
        s5 = net.rolling(5).sum().loc[tasi.index]
        axes[1].bar(s5.index, s5.values, color=np.where(s5.values < 0, RED, BLUE), width=1.5); axes[1].axhline(0, color=GREY, lw=0.6)
        axes[1].set_ylabel("NH−NL 5-day sum", fontsize=7)
        for ax in axes:
            ax.tick_params(labelsize=7); ax.grid(True, color="#e6e5e1", lw=0.5)
        fig.tight_layout(); pdf.savefig(fig); plt.close(fig)
    # ---- TASI extreme thresholds (percentiles 2007+)
    m = ad.loc["2007":]
    thr = pd.DataFrame([
        {"indicator": "NH−NL daily (net)", "p2": net.loc["2007":].quantile(.02), "p5": net.loc["2007":].quantile(.05), "p95": net.loc["2007":].quantile(.95), "p98": net.loc["2007":].quantile(.98)},
        {"indicator": "NH−NL 5-day sum", "p2": net.rolling(5).sum().loc["2007":].quantile(.02), "p5": net.rolling(5).sum().loc["2007":].quantile(.05), "p95": net.rolling(5).sum().loc["2007":].quantile(.95), "p98": net.rolling(5).sum().loc["2007":].quantile(.98)},
        {"indicator": "McClellan Oscillator (ratio-adjusted)", "p2": m["mcclellan_osc"].quantile(.02), "p5": m["mcclellan_osc"].quantile(.05), "p95": m["mcclellan_osc"].quantile(.95), "p98": m["mcclellan_osc"].quantile(.98)},
        {"indicator": "McClellan Summation", "p2": m["mcclellan_sum"].quantile(.02), "p5": m["mcclellan_sum"].quantile(.05), "p95": m["mcclellan_sum"].quantile(.95), "p98": m["mcclellan_sum"].quantile(.98)},
        {"indicator": "TRIN-10", "p2": m["trin_10"].quantile(.02), "p5": m["trin_10"].quantile(.05), "p95": m["trin_10"].quantile(.95), "p98": m["trin_10"].quantile(.98)},
    ]).round(1)
    thr.to_csv(out / "canary_thresholds.csv", index=False)
    return {"episodes": len(t), "avg": avg, "avg_ex": avg_ex, "excluded": big, "out": str(out)}


if __name__ == "__main__":
    print(json.dumps(run(load_config()), indent=2, default=str))
