"""python -m breadth.composite.run [--fit]
--fit : derive family weights in-sample and write them into config/config.yaml (composite.weights)
Always: compute family scores, Breadth Score, regimes, drivers -> storage/features/composite.parquet
"""
from __future__ import annotations

import argparse
import json

import pandas as pd
import yaml

from breadth import ROOT, load_config
from breadth.composite.features import family_scores, load_raw
from breadth.composite.score import assign_regimes, compute_score, fit_weights


def run(cfg: dict, fit: bool = False) -> dict:
    st = ROOT / cfg["paths"]["storage_dir"]
    raw = load_raw(cfg)
    fam, pct = family_scores(raw, cfg)
    idx = pd.read_parquet(st / "clean" / "index.parquet")
    tasi = idx[idx["index_code"] == "TASI"].set_index("date")["close"]
    info = {}
    if fit:
        w = fit_weights(fam, tasi, cfg)
        info["fit"] = w
        cfg["composite"]["weights"] = {k: v for k, v in w.items() if not k.startswith("_")}
        cfg_path = ROOT / "config" / "config.yaml"
        text = cfg_path.read_text(encoding="utf-8")
        # replace the weights line only (keeps comments elsewhere intact)
        new_line = "  weights: " + json.dumps(cfg["composite"]["weights"])
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if ln.strip().startswith("weights:") and "composite" in "".join(lines[max(0, i - 40):i]):
                lines[i] = new_line + "   # derived in-sample by breadth.composite.run --fit; do not hand-edit"
                break
        cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sc = compute_score(fam, cfg)
    span = cfg["composite"]["regimes"]["smooth_span"]
    sc["score_smooth"] = sc["score"].ewm(span=span, adjust=False).mean()
    sc["regime"] = assign_regimes(sc["score_smooth"], sc["overlay"], cfg)
    out = fam.join(sc, rsuffix="_s")
    out = out.join(raw.add_prefix("raw_"))
    out.reset_index().to_parquet(st / "features" / "composite.parquet", index=False)
    pct.reset_index().to_parquet(st / "features" / "composite_percentiles.parquet", index=False)
    last = out.dropna(subset=["score"]).iloc[-1]
    info["last"] = {"date": str(out.dropna(subset=["score"]).index[-1].date()), "score": round(float(last["score"]), 1),
                    "regime": last["regime"], "driver": last["driver"], "overlay": round(float(last["overlay"]), 1)}
    info["weights"] = cfg["composite"]["weights"]
    return info


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--fit", action="store_true")
    a = ap.parse_args()
    print(json.dumps(run(load_config(), a.fit), indent=2, default=str))
