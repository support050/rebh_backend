"""Hand-calculated examples for Phase 5 (composite score, weights, regimes)."""
import numpy as np
import pandas as pd
import pytest

from breadth import load_config
from breadth.composite.features import rolling_pct
from breadth.composite.score import assign_regimes, compute_score, fit_weights


def test_rolling_percentile_uses_only_past_and_today():
    s = pd.Series([1, 5, 3, 4, 2, 6], dtype=float)
    p = rolling_pct(s, window=3, min_periods=3)
    # day3 window (1,5,3): 3 is 2nd of 3 -> 66.7 ; day4 (5,3,4): 4 is 2nd -> 66.7 ; day5 (3,4,2): 2 lowest -> 33.3
    assert np.isnan(p.iloc[1])
    assert p.iloc[2] == pytest.approx(200 / 3)
    assert p.iloc[3] == pytest.approx(200 / 3)
    assert p.iloc[4] == pytest.approx(100 / 3)
    assert p.iloc[5] == pytest.approx(100)          # 6 is the max of (4,2,6)


def _cfg():
    cfg = load_config()
    cfg["composite"]["families"] = {"A": ["a"], "B": ["b"]}
    cfg["composite"]["weights"] = {"A": 0.75, "B": 0.25}
    return cfg


def test_score_weighted_average_and_renormalisation():
    cfg = _cfg()
    fam = pd.DataFrame({"A": [80.0, 80.0], "B": [40.0, np.nan], "overlay": [10.0, 10.0]})
    out = compute_score(fam, cfg)
    assert out["score"].iloc[0] == pytest.approx(0.75 * 80 + 0.25 * 40)     # 70
    assert out["score"].iloc[1] == pytest.approx(80)                        # B missing -> A carries full weight
    assert out["driver"].iloc[0] == "A"                                     # |0.75*30| > |0.25*(-10)|
    assert out["contrib_A"].iloc[0] == pytest.approx(22.5) and out["contrib_B"].iloc[0] == pytest.approx(-2.5)


def test_fit_weights_prefers_the_predictive_family():
    cfg = _cfg(); cfg["composite"]["in_sample_end"] = "2030-01-01"; cfg["composite"]["shrink"] = 1.0
    n = 400
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2010-01-01", periods=n)
    tasi = pd.Series(1000 * np.cumprod(1 + rng.normal(0, 0.01, n)), index=idx)
    f20 = (tasi.shift(-20) / tasi - 1).fillna(0)
    a = (50 + 400 * f20 + rng.normal(0, 5, n)).clip(0, 100)   # A "knows" the future 20-day return (+ noise)
    b = rng.uniform(0, 100, n)                                 # B is noise
    fam = pd.DataFrame({"A": a.values, "B": b, "overlay": 50.0}, index=idx)
    w = fit_weights(fam, tasi, cfg)
    assert w["A"] > 0.8 and w["B"] < 0.2
    assert w["_spearman_A"] > w["_spearman_B"]


def test_regime_hysteresis_and_min_days():
    cfg = load_config()
    cfg["composite"]["regimes"].update({"boundaries": [40, 60, 75], "hysteresis": 5, "min_days": 2,
                                        "washout_score_max": 45, "washout_overlay_min": 75, "washout_exit_overlay": 55})
    score = pd.Series([50, 62, 63, 66, 66, 66, 58, 54, 54, 30, 30, 30, 30, 30, 30], dtype=float)
    over = pd.Series([10] * 9 + [80, 80, 80, 50, 50, 50], dtype=float)
    r = assign_regimes(score, over, cfg).tolist()
    # start Neutral(50). 62/63 < 60+5 -> stay Neutral. 66 >= 65 -> Healthy (min_days met by then).
    # 58/54 not < 60-5=55 until 54 -> back to Neutral after >= 2 days in Healthy.
    # 30 with overlay 80 -> Weak then Washout (score<=45 & overlay>=75); overlay 50 < 55 -> exits to Weak.
    assert r[:3] == ["Neutral", "Neutral", "Neutral"]
    assert r[3] == "Healthy"
    assert "Neutral" in r[7:9]
    assert "Washout" in r[9:12]
    assert r[-1] == "Weak"
