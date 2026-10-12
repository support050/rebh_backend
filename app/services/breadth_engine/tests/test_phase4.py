"""Hand-calculated examples for Phase 4 (sectors, concentration, divergences)."""
import numpy as np
import pandas as pd
import pytest

from breadth import load_config
from breadth.indicators.concentration import cap_weights
from breadth.indicators.divergences import compute_divergences
from breadth.indicators.thrusts import first_of_run


def test_cap_weights_redistributes_excess():
    w = pd.DataFrame([[0.50, 0.14] + [0.045] * 8])           # sums to 1.0 ; 10 names
    out = cap_weights(w, 0.15)
    row = out.iloc[0]
    assert row.sum() == pytest.approx(1.0)
    assert row.max() <= 0.15 + 1e-9
    assert row.iloc[0] == pytest.approx(0.15)
    # excess 0.35 spread pro-rata over the 9 uncapped names; 0.14 absorbs 0.35*0.14/0.5 = 0.098 -> 0.238 > cap
    # -> second pass caps it too; final: two names at 0.15, the eight small ones share the rest equally
    assert row.iloc[1] == pytest.approx(0.15)
    assert np.allclose(row.iloc[2:], (1 - 0.30) / 8)
    # infeasible cap (n*cap < 1): best effort, weights still sum to 1 and nothing is lost
    w2 = pd.DataFrame([[0.6, 0.2, 0.1, 0.1]])
    assert cap_weights(w2, 0.15).iloc[0].sum() == pytest.approx(1.0)


def test_divergence_bearish_when_ad_line_fails_to_confirm():
    cfg = load_config()
    cfg["divergences"].update({"index_window": 3, "confirm_window": 3, "tolerance_days": 0, "min_gap_days": 1,
                               "metrics": ["ad_line"]})
    n = 8
    idx = pd.Series([100, 101, 102, 103, 104, 105, 106, 107], index=pd.RangeIndex(n), dtype=float)  # new high every day
    ad = pd.Series([0, 1, 2, 3, 4, 3, 2, 1], dtype=float)                                            # peaks on day 4
    m = pd.DataFrame({"ad_line": ad})
    out = compute_divergences(idx, m, cfg)
    # days 2..4: index at 3-day high AND ad_line at its 3-day high -> confirmed
    # days 5..7: index at 3-day high, ad_line below its 3-day max -> bearish divergence
    assert out["index_high"].iloc[2:].all()
    assert out["bear_ad_line"].tolist()[2:] == [False, False, False, True, True, True]
    assert out["n_bearish"].iloc[5] == 1 and out["n_bearish"].iloc[4] == 0


def test_divergence_bullish_at_lows():
    cfg = load_config()
    cfg["divergences"].update({"index_window": 2, "confirm_window": 2, "tolerance_days": 0, "min_gap_days": 1,
                               "metrics": ["ad_line"]})
    idx = pd.Series([10, 9, 8, 7], dtype=float)
    ad = pd.Series([5, 4, 4.5, 5], dtype=float)      # breadth turns up while index makes new lows
    out = compute_divergences(idx, pd.DataFrame({"ad_line": ad}), cfg)
    assert out["bull_ad_line"].tolist() == [False, False, True, True]


def test_sector_pooling_and_score():
    from breadth.indicators.common import Panels
    from breadth.indicators.sectors import compute_sectors
    cfg = load_config()
    cfg["sectors"]["min_stocks"] = 2
    cfg["sectors"]["ad_window"] = 1; cfg["sectors"]["mom_window"] = 1
    cfg["indicators"]["participation"]["new_high_low_window"] = 2
    n = 260
    d = pd.date_range("2020-01-01", periods=n, freq="B")
    # 3 stocks: 1,2 in "Banks" (rising), 3 alone in "Energy" (falling) -> Energy pooled into Other (<2) and dropped
    close = pd.DataFrame({1: np.linspace(10, 20, n), 2: np.linspace(10, 30, n), 3: np.linspace(30, 10, n)}, index=d)
    level = close.copy(); ret = close / close.shift() - 1
    age = pd.DataFrame(np.arange(1, n + 1)[:, None].repeat(3, 1), index=d, columns=close.columns)
    p = Panels(close=close, level=level, ret=ret, volume=pd.DataFrame(100.0, index=d, columns=close.columns).cumsum(),
               age=age, listed=pd.DataFrame(True, index=d, columns=close.columns))
    master = pd.DataFrame({"symbol": [1, 2, 3], "industry_group": ["Banks", "Banks", "Energy"]})
    long, summ = compute_sectors(p, master, cfg)
    last = long[long["date"] == d[-1]].set_index("sector")
    assert list(last.index) == ["Banks"]
    b = last.loc["Banks"]
    assert b["pct50"] == 100 and b["pct200"] == 100 and b["nhnl"] == 100 and b["ad10"] == 100
    assert b["score"] == pytest.approx(100)
    assert summ.loc[d[-1], "n_healthy"] == 1 and summ.loc[d[-1], "n_sectors"] == 1


def test_first_of_run_used_by_divergence_dedupe():
    s = pd.Series([True] * 5)
    assert first_of_run(s, 20).sum() == 1
