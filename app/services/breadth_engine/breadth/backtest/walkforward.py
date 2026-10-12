"""Phase 6 — walk-forward validation of the composite.

python -m breadth.backtest.walkforward
Procedure (expanding window, yearly refit, no look-ahead):
  for test_year in first_test_year..last:
      train = all days with date <= Dec-31 of (test_year-1) AND forward returns fully known
              (i.e. date <= Dec-31(test_year-1) - 60 trading days)
      fit weights on train (breadth.composite.score.fit_weights with the chosen λ / family set)
      score test_year with those weights; regimes assigned with the same hysteresis rules
  Stitch all test years -> pure out-of-sample score & regimes (2014-2026).
Variants: λ ∈ {0, 0.5, 1} × family sets {all, A+B+E} ; plus regime boundaries ±5 on the base variant.
Metrics per variant (OOS): Spearman(score, fwd20/fwd60), quintile spread (Q5-Q1 fwd60),
  regime table (n, mean/hit at 20/60, max drawdown within 60d), regime changes.
Outputs: storage/validation/phase6_walkforward_variants.csv, phase6_walkforward_regimes.csv,
         storage/features/composite_walkforward.parquet (base variant)
"""
from __future__ import annotations

import copy
import json

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from breadth import ROOT, load_config
from breadth.composite.features import family_scores, load_raw
from breadth.composite.score import assign_regimes, compute_score, fit_weights

FIRST_TEST_YEAR = 2014


def max_drawdown_within(tasi: pd.Series, horizon: int) -> pd.Series:
    """Worst peak-to-trough decline (pct) from T over the next `horizon` days (min of path / close_T - 1)."""
    fut_min = tasi[::-1].rolling(horizon, min_periods=1).min()[::-1].shift(-1)
    return 100 * (fut_min / tasi - 1)


def walk_forward(fam: pd.DataFrame, tasi: pd.Series, cfg: dict) -> tuple[pd.DataFrame, list[dict]]:
    cfg = copy.deepcopy(cfg)
    fams = list(cfg["composite"]["families"].keys())
    years = range(FIRST_TEST_YEAR, fam.index.max().year + 1)
    pieces, fits = [], []
    for y in years:
        cut = pd.Timestamp(f"{y-1}-12-31")
        cfg["composite"]["in_sample_end"] = str(cut.date())
        # forward returns must be fully known by `cut`: drop the last 60 trading days before cut
        known = tasi.index[tasi.index <= cut]
        train_end = known[-61] if len(known) > 61 else known[0]
        cfg["composite"]["in_sample_end"] = str(train_end.date())
        w = fit_weights(fam, tasi, cfg)
        cfg["composite"]["weights"] = {f: w[f] for f in fams}
        sc = compute_score(fam, cfg)
        sc = sc[(sc.index.year == y)]
        pieces.append(sc)
        fits.append({"test_year": y, "train_end": str(train_end.date()), **{f: w[f] for f in fams}})
    out = pd.concat(pieces).sort_index()
    span = cfg["composite"]["regimes"]["smooth_span"]
    out["score_smooth"] = out["score"].ewm(span=span, adjust=False).mean()
    out["regime"] = assign_regimes(out["score_smooth"], out["overlay"], cfg)
    return out, fits


def evaluate(sc: pd.DataFrame, tasi: pd.Series, label: dict) -> tuple[dict, pd.DataFrame]:
    f20 = 100 * (tasi.shift(-20) / tasi - 1); f60 = 100 * (tasi.shift(-60) / tasi - 1); f5 = 100 * (tasi.shift(-5) / tasi - 1)
    dd60 = max_drawdown_within(tasi, 60)
    d = sc.join(pd.DataFrame({"f5": f5, "f20": f20, "f60": f60, "dd60": dd60}), how="inner").dropna(subset=["score", "f60"])
    row = dict(label)
    row["n_days"] = len(d)
    row["spearman_20"] = round(spearmanr(d["score"], d["f20"]).correlation, 3)
    row["spearman_60"] = round(spearmanr(d["score"], d["f60"]).correlation, 3)
    q = pd.qcut(d["score"], 5, labels=False, duplicates="drop")
    g = d.groupby(q)["f60"].mean()
    row["q5_minus_q1_f60"] = round(g.iloc[-1] - g.iloc[0], 2)
    row["regime_changes"] = int((d["regime"] != d["regime"].shift()).sum())
    row["base_20"] = round(d["f20"].mean(), 2); row["base_60"] = round(d["f60"].mean(), 2)
    regs = []
    for r_, gg in d.groupby("regime"):
        regs.append({**label, "regime": r_, "n": len(gg), "mean_5": round(gg["f5"].mean(), 2),
                     "mean_20": round(gg["f20"].mean(), 2), "hit_20": round((gg["f20"] > 0).mean(), 2),
                     "mean_60": round(gg["f60"].mean(), 2), "hit_60": round((gg["f60"] > 0).mean(), 2),
                     "avg_dd60": round(gg["dd60"].mean(), 2), "worst_dd60": round(gg["dd60"].min(), 2),
                     "false_bullish_20": round(((gg["f20"] < -5)).mean(), 2) if r_ in ("Healthy", "Strong", "Washout") else np.nan})
        row[f"{r_}_f60"] = round(gg["f60"].mean(), 2)
    return row, pd.DataFrame(regs)


def run(cfg: dict) -> dict:
    st = ROOT / cfg["paths"]["storage_dir"]
    raw = load_raw(cfg); fam, _ = family_scores(raw, cfg)
    idx = pd.read_parquet(st / "clean" / "index.parquet")
    tasi = idx[idx["index_code"] == "TASI"].set_index("date")["close"]
    variants, regime_rows, base_out = [], [], None
    all_fams = dict(cfg["composite"]["families"])
    sets = {"all": all_fams, "A+B+E": {k: v for k, v in all_fams.items() if k[0] in "ABE"}}
    for set_name, fset in sets.items():
        for lam in (0.0, 0.5, 1.0):
            c = copy.deepcopy(cfg); c["composite"]["families"] = fset; c["composite"]["shrink"] = lam
            sc, fits = walk_forward(fam, tasi, c)
            row, regs = evaluate(sc, tasi, {"families": set_name, "lambda": lam, "boundaries": "40/60/75"})
            variants.append(row); regime_rows.append(regs)
            if set_name == "all" and lam == 0.5:
                base_out = sc; base_fits = fits
    # boundary sensitivity on the base variant (all, λ=0.5)
    for shift in (-5, 5):
        c = copy.deepcopy(cfg); B = [b + shift for b in c["composite"]["regimes"]["boundaries"]]
        c["composite"]["regimes"]["boundaries"] = B
        sc = base_out.copy()
        sc["regime"] = assign_regimes(sc["score_smooth"], sc["overlay"], c)
        row, regs = evaluate(sc, tasi, {"families": "all", "lambda": 0.5, "boundaries": "/".join(map(str, B))})
        variants.append(row); regime_rows.append(regs)
    v = pd.DataFrame(variants); v.to_csv(st / "validation" / "phase6_walkforward_variants.csv", index=False)
    r = pd.concat(regime_rows, ignore_index=True); r.to_csv(st / "validation" / "phase6_walkforward_regimes.csv", index=False)
    base_out.reset_index().to_parquet(st / "features" / "composite_walkforward.parquet", index=False)
    pd.DataFrame(base_fits).to_csv(st / "validation" / "phase6_walkforward_weights.csv", index=False)
    return {"variants": len(v), "oos_from": str(base_out.index.min().date()), "oos_to": str(base_out.index.max().date())}


if __name__ == "__main__":
    print(json.dumps(run(load_config()), indent=2))
