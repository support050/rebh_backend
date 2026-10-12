"""Breadth Score (0-100), in-sample weight derivation, regimes with hysteresis, driver attribution.

Score_T = sum_f w_f * FamilyScore_f,T          (w_f >= 0, sum w_f = 1)

Weights (fit_weights): on the in-sample window (<= in_sample_end, TASI available from 2007),
  target y_T = z(fwd_20_T) + z(fwd_60_T)        (standardised forward TASI returns, both horizons)
  X_T        = FamilyScore_f,T - 50               (centred family scores)
  Two estimators are computed and reported; `weight_method` in config picks the one used:
    nnls        w = argmin ||X w - y||, w >= 0 (scipy.optimize.nnls) — multivariate, unstable when families
                are collinear (A, B, E all measure participation), tends to load on one family.
    corr_shrink w_f ∝ (1-λ)/n + λ * max(ρ_f, 0) / Σ max(ρ, 0), ρ_f = Spearman corr(FamilyScore_f, y)
                (λ = shrink; λ=0 equal weights, λ=1 pure correlation weights). Few parameters, robust.
  Both normalised to sum 1. A family with ρ_f <= 0 gets only its shrinkage share (corr_shrink) or 0 (nnls).
  Overlapping forward windows: point estimates only; validity is judged out-of-sample / walk-forward (Phase 6).

Regimes: bands on the SMOOTHED score (EMA span `smooth_span`) with boundaries [40, 60, 75]
  -> Weak | Neutral | Healthy | Strong.
  Hysteresis h: from band k, move up only when score >= boundary_k + h, move down only when
  score < boundary_{k-1} - h; a regime must last >= min_days before it can change.
  Washout: (score <= washout_score_max AND overlay >= washout_overlay_min) enters Washout;
  it exits when overlay < washout_exit_overlay OR score > boundaries[0] + h (then the band rule applies).

Drivers: contribution_f = w_f * (FamilyScore_f - 50); the family with the largest |contribution|
  drives the score; delta_f = w_f * (FamilyScore_f,T - FamilyScore_f,T-1) explains today's change.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import nnls
from scipy.stats import spearmanr

REGIMES = ["Weak", "Neutral", "Healthy", "Strong"]


def fit_weights(fam: pd.DataFrame, tasi: pd.Series, cfg: dict) -> dict:
    c = cfg["composite"]
    fams = list(c["families"].keys())
    end = pd.Timestamp(c["in_sample_end"])
    f20 = tasi.shift(-20) / tasi - 1
    f60 = tasi.shift(-60) / tasi - 1
    df = fam[fams].join(pd.DataFrame({"f20": f20, "f60": f60}), how="inner").dropna()
    df = df[df.index <= end]
    y = ((df["f20"] - df["f20"].mean()) / df["f20"].std() + (df["f60"] - df["f60"].mean()) / df["f60"].std()).to_numpy()
    X = (df[fams] - 50).to_numpy()
    w_nnls, _ = nnls(X, y)
    w_nnls = w_nnls / w_nnls.sum() if w_nnls.sum() > 0 else np.ones(len(fams)) / len(fams)
    rho = np.array([spearmanr(df[f], y).correlation for f in fams])
    pos = np.clip(rho, 0, None)
    lam = c.get("shrink", 0.5); n = len(fams)
    w_cs = (1 - lam) / n + lam * (pos / pos.sum() if pos.sum() > 0 else 1 / n)
    w_cs = w_cs / w_cs.sum()
    chosen = w_cs if c.get("weight_method", "corr_shrink") == "corr_shrink" else w_nnls
    out = {f: round(float(v), 4) for f, v in zip(fams, chosen)}
    out["_method"] = c.get("weight_method", "corr_shrink")
    out["_n_obs"] = int(len(df)); out["_from"] = str(df.index.min().date()); out["_to"] = str(df.index.max().date())
    for f, r_, a, b in zip(fams, rho, w_nnls, w_cs):
        out[f"_spearman_{f}"] = round(float(r_), 3); out[f"_nnls_{f}"] = round(float(a), 4); out[f"_corr_shrink_{f}"] = round(float(b), 4)
    return out


def compute_score(fam: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    c = cfg["composite"]
    fams = list(c["families"].keys())
    w = {f: c["weights"].get(f, np.nan) for f in fams}
    if any(pd.isna(v) for v in w.values()) or sum(w.values()) == 0:
        w = {f: 1 / len(fams) for f in fams}
    ws = pd.Series(w)
    avail = fam[fams].notna()
    wn = avail.mul(ws, axis=1)
    wn = wn.div(wn.sum(axis=1).replace(0, np.nan), axis=0)          # renormalise over available families
    out = pd.DataFrame(index=fam.index)
    out["score"] = (fam[fams].fillna(0) * wn).sum(axis=1).where(avail.any(axis=1))
    for f in fams:
        out[f"contrib_{f}"] = wn[f] * (fam[f] - 50)
        out[f"delta_{f}"] = wn[f] * fam[f].diff()
    contrib = out[[f"contrib_{f}" for f in fams]]
    ok = contrib.notna().any(axis=1)
    out["driver"] = None
    out.loc[ok, "driver"] = contrib[ok].abs().idxmax(axis=1).str.replace("contrib_", "")
    out["score_change"] = out["score"].diff()
    out["overlay"] = fam["overlay"]
    return out


def assign_regimes(score: pd.Series, overlay: pd.Series, cfg: dict) -> pd.Series:
    r = cfg["composite"]["regimes"]
    B, h, mind = r["boundaries"], r["hysteresis"], r["min_days"]

    def band(x):
        return int(np.searchsorted(B, x, side="right"))   # 0..3

    out = []
    cur, days = None, 0
    for s, o in zip(score.to_numpy(), overlay.to_numpy()):
        if np.isnan(s):
            out.append(None); continue
        if cur is None:
            cur = REGIMES[band(s)]; days = 0
        else:
            days += 1
            if cur == "Washout":
                if days >= mind and ((not np.isnan(o) and o < r["washout_exit_overlay"]) or s > B[0] + h):
                    cur = REGIMES[band(s)]; days = 0
            else:
                k = REGIMES.index(cur)
                new = k
                if k < 3 and s >= B[k] + h:
                    new = band(s)
                elif k > 0 and s < B[k - 1] - h:
                    new = band(s)
                if days >= mind and new != k:
                    cur = REGIMES[new]; days = 0
                if cur in ("Weak", "Neutral") and s <= r["washout_score_max"] and not np.isnan(o) and o >= r["washout_overlay_min"]:
                    cur = "Washout"; days = 0
        out.append(cur)
    return pd.Series(out, index=score.index, dtype="object")
