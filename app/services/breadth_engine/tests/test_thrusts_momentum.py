"""Hand-calculated examples for Phase 3 (thrusts + momentum breadth)."""
import numpy as np
import pandas as pd
import pytest

from breadth import load_config
from breadth.indicators.common import Panels
from breadth.indicators.momentum import compute_momentum, daily_movers
from breadth.indicators.thrusts import first_of_run, low_to_high, ratio_thrust, zweig


def test_first_of_run_cooldown():
    s = pd.Series([False, True, True, False, True, False, False, False, True])
    out = first_of_run(s, cooldown=2)
    # day1 fires; day2 same run; day4 is only 3 days after day1 (> cooldown 2) -> fires; day8 fires
    assert out.tolist() == [False, True, False, False, True, False, False, False, True]


def test_low_to_high_window():
    x = pd.Series([0.30, 0.50, 0.70, 0.70, 0.70, 0.70])
    st = low_to_high(x, low=0.40, high=0.615, window=2)
    # day2: prior 2 values (0.30, 0.50) min 0.30 < 0.40 and 0.70 > 0.615 -> True
    # day3: prior (0.50, 0.70) min 0.50 -> False ; day4+: False
    assert st.tolist() == [False, False, True, False, False, False]


def test_zweig_hand_example():
    # A/(A+D) ratio constant 0.2 for 10 days then 1.0 for 10 days; EMA span 10 (alpha 2/11)
    A = pd.Series([20] * 10 + [100] * 10, dtype=float); D = pd.Series([80] * 10 + [0] * 10, dtype=float)
    ad = pd.DataFrame({"advances": A, "declines": D})
    z = zweig(ad, span=10, low=0.40, high=0.615, window=10, cooldown=20)
    e = z["zweig_value"]
    assert e.iloc[9] == pytest.approx(0.2)
    # after k days at 1.0: ema = 1 - 0.8 * (1-alpha)^k ; crosses 0.615 when (9/11)^k < 0.48125 -> k = 4
    k = 4
    assert e.iloc[9 + k] == pytest.approx(1 - 0.8 * (9 / 11) ** k)
    assert e.iloc[9 + k - 1] < 0.615 < e.iloc[9 + k]
    assert z["zweig_signal"].tolist().index(True) == 9 + k
    assert z["zweig_signal"].sum() == 1


def test_ratio_thrust_5d():
    A = pd.Series([100, 100, 100, 100, 100, 100], dtype=float)
    D = pd.Series([50, 50, 50, 50, 20, 20], dtype=float)
    r = ratio_thrust(pd.DataFrame({"advances": A, "declines": D}), days=5, ratio=3.0, cooldown=20, name="w")
    assert r["w_value"].iloc[4] == pytest.approx(500 / 220)     # 2.27 < 3 -> no signal
    assert r["w_value"].iloc[5] == pytest.approx(500 / 190)     # 2.63 -> still no signal
    assert not r["w_signal"].any()
    r2 = ratio_thrust(pd.DataFrame({"advances": A, "declines": D}), days=5, ratio=2.5, cooldown=20, name="w")
    assert r2["w_signal"].tolist() == [False, False, False, False, False, True]


def _panels(close, volume):
    close = close.astype(float); level = close.ffill()
    ret = close / level.shift() - 1
    age = pd.DataFrame(np.arange(1, len(close) + 1)[:, None].repeat(close.shape[1], 1), index=close.index, columns=close.columns)
    return Panels(close=close, level=level, ret=ret, volume=volume.astype(float), age=age,
                  listed=pd.DataFrame(True, index=close.index, columns=close.columns))


def test_daily_movers_with_volume_condition():
    d = pd.date_range("2024-01-01", periods=2)
    close = pd.DataFrame({1: [10, 10.5], 2: [10, 10.4], 3: [10, 9.5], 4: [10, 9.5]}, index=d)   # +5%, +4%, -5%, -5%
    vol = pd.DataFrame({1: [100, 200], 2: [100, 50], 3: [100, 150], 4: [100, 100]}, index=d)
    up, dn = daily_movers(_panels(close, vol), 0.04, require_volume_up=True)
    assert up.iloc[1] == 1      # stock 2 is +4% but volume fell
    assert dn.iloc[1] == 1      # stock 4 is -5% but volume flat (not >)
    up2, dn2 = daily_movers(_panels(close, vol), 0.04, require_volume_up=False)
    assert up2.iloc[1] == 2 and dn2.iloc[1] == 2


def test_month_quarter_counts():
    cfg = load_config()
    cfg["momentum"].update({"month_days": 2, "quarter_days": 3, "d34_days": 2, "ratio_windows": [2]})
    d = pd.date_range("2024-01-01", periods=4)
    # stock1: 10 -> 13 over 2 days (+30%): counts for 25%/month, 13%/34d ; over 3 days from 10 -> 13 (+30%) for quarter
    close = pd.DataFrame({1: [10, 11, 12, 13], 2: [10, 10, 10, 7]}, index=d)   # stock2 -30% over 2 days
    vol = pd.DataFrame(100, index=d, columns=[1, 2])
    m = compute_momentum(_panels(close, vol), cfg)
    last = m.iloc[3]
    assert last["up_25_m"] == 0            # 11 -> 13 = +18% over 2 days (not 25)
    assert last["down_25_m"] == 1          # 10 -> 7 = -30%
    assert last["up_13_34"] == 1 and last["down_13_34"] == 1
    assert last["up_25_q"] == 1            # 10 -> 13 over 3 days = +30%
    assert np.isnan(m.iloc[2]["up_25_q"]) or m.iloc[2]["up_25_q"] == 0   # age 3 not > 3 -> ineligible


def test_lowry_sequence_and_rare_variants():
    """95% down day on day 2, 90% up day on day 5 -> sequence signal on day 5; rare Zweig/Deemer columns exist."""
    import numpy as np, pandas as pd
    from breadth import load_config
    from breadth.indicators.thrusts import compute_thrusts
    cfg = load_config()
    n = 60
    idx = pd.bdate_range("2024-01-01", periods=n)
    A = np.full(n, 100.0); D = np.full(n, 100.0)
    upv = np.full(n, 0.5); upp = np.full(n, 0.5)
    A[2], D[2], upv[2], upp[2] = 5, 195, 0.02, 0.02          # 95% down day
    A[5], D[5], upv[5], upp[5] = 190, 10, 0.95, 0.95         # 90%+ up day within 10 sessions
    ad = pd.DataFrame({"advances": A, "declines": D, "up_volume_pct": upv, "up_points_pct": upp}, index=idx)
    part = pd.DataFrame({"pct_above_sma50": np.full(n, 30.0)}, index=idx)
    out = compute_thrusts(ad, part, cfg)
    assert bool(out.loc[idx[5], "lowry_seq_signal"]) and bool(out.loc[idx[2], "lowry_down_signal"])
    assert out["lowry_seq_signal"].sum() == 1
    assert out.loc[idx[2], "lowry_down_points"] and out.loc[idx[2], "lowry_down_volume"]
    assert {"zweig_rare_signal", "deemer_rare_signal"} <= set(out.columns)
