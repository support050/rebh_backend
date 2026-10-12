"""Hand-calculated examples for Phase 2 indicators."""
import numpy as np
import pandas as pd
import pytest

from breadth import load_config
from breadth.indicators.advance_decline import compute_advance_decline, ema_sma_seeded
from breadth.indicators.common import Panels, build_panels
from breadth.indicators.participation import compute_participation


@pytest.fixture
def cfg():
    c = load_config()
    c["indicators"]["advance_decline"]["min_stocks"] = 1
    return c


def _panels(close: pd.DataFrame, volume: pd.DataFrame | None = None) -> Panels:
    close = close.astype(float)
    level = close.ffill()
    prev = level.shift()
    ret = close / prev - 1
    vol = volume.astype(float) if volume is not None else close.notna().astype(float) * 100
    age = pd.DataFrame(np.arange(1, len(close) + 1)[:, None].repeat(close.shape[1], 1), index=close.index, columns=close.columns)
    listed = pd.DataFrame(True, index=close.index, columns=close.columns)
    return Panels(close=close, level=level, ret=ret, volume=vol, age=age, listed=listed)


def test_advance_decline_counts_and_trin(cfg):
    d = pd.date_range("2024-01-01", periods=2)
    close = pd.DataFrame({1: [10, 11], 2: [10, 9], 3: [10, 10], 4: [10, np.nan]}, index=d)
    vol = pd.DataFrame({1: [0, 300], 2: [0, 100], 3: [0, 50], 4: [0, 0]}, index=d)
    ad = compute_advance_decline(_panels(close, vol), cfg).iloc[1]
    assert ad["advances"] == 1 and ad["declines"] == 1 and ad["unchanged"] == 1 and ad["n_ad"] == 3
    assert ad["up_volume"] == 300 and ad["down_volume"] == 100
    assert ad["ad_ratio"] == 1 and ad["ud_volume_ratio"] == 3
    assert ad["trin"] == pytest.approx((1 / 1) / (300 / 100))          # 0.3333
    assert ad["up_points"] == 1 and ad["down_points"] == 1                # |11-10|, |9-10|
    assert ad["up_volume_pct"] == pytest.approx(0.75)
    assert ad["rana"] == 0                                                # 1000*(1-1)/2


def test_lowry_90_percent_day(cfg):
    d = pd.date_range("2024-01-01", periods=2)
    close = pd.DataFrame({1: [10, 12], 2: [10, 11], 3: [10, 9.9]}, index=d)   # points up 3, down 0.1 -> 96.8%
    vol = pd.DataFrame({1: [0, 900], 2: [0, 50], 3: [0, 50]}, index=d)        # up vol 950/1000 = 95%
    ad = compute_advance_decline(_panels(close, vol), cfg).iloc[1]
    assert bool(ad["day90_up"]) is True and bool(ad["day90_down"]) is False


def test_mcclellan_ema_seed_and_recursion():
    s = pd.Series([100.0, 200.0, 300.0, 400.0])
    e = ema_sma_seeded(s, 3)
    assert np.isnan(e.iloc[0]) and np.isnan(e.iloc[1])
    assert e.iloc[2] == pytest.approx(200.0)                    # SMA seed of first 3
    assert e.iloc[3] == pytest.approx(200 + 0.5 * (400 - 200))  # alpha = 2/(3+1) = 0.5 -> 300


def test_pct_above_sma_and_eligibility(cfg):
    cfg["indicators"]["participation"]["sma_windows"] = [3]
    cfg["indicators"]["participation"]["ema_windows"] = []
    d = pd.date_range("2024-01-01", periods=4)
    # stock 1: 10,10,10,13 -> day4 SMA3 = 11, price 13 above ; stock 2: 10,10,10,7 -> below
    close = pd.DataFrame({1: [10, 10, 10, 13], 2: [10, 10, 10, 7]}, index=d)
    out = compute_participation(_panels(close), cfg)
    assert np.isnan(out["pct_above_sma3"].iloc[1])              # not enough history (age 2 < 3)
    assert out["pct_above_sma3"].iloc[2] == 0                   # day3: 10 > 10 is False for both
    assert out["pct_above_sma3"].iloc[3] == 50 and out["n_sma3"].iloc[3] == 2


def test_new_highs_lows(cfg):
    cfg["indicators"]["participation"]["new_high_low_window"] = 3
    cfg["indicators"]["participation"]["sma_windows"] = []
    cfg["indicators"]["participation"]["ema_windows"] = []
    d = pd.date_range("2024-01-01", periods=4)
    close = pd.DataFrame({1: [10, 11, 12, 13], 2: [10, 9, 8, 7], 3: [10, 10, 10, 10]}, index=d)
    out = compute_participation(_panels(close), cfg)
    assert out["new_highs"].iloc[3] == 1 and out["new_lows"].iloc[3] == 1
    assert out["net_nh_nl"].iloc[3] == 0                        # flat stock 3 is neither
    assert out["new_highs"].iloc[1] == 0                        # window not full yet


def test_panels_from_clean_frame_excludes_suspended_returns():
    d = pd.date_range("2024-01-01", periods=3)
    rows = []
    for i, (c, s) in enumerate([(10, False), (np.nan, True), (12, False)]):
        rows.append({"symbol": 1, "date": d[i], "close_adj": c, "last_close_adj": 10 if np.isnan(c) else c,
                     "volume_adj": 0 if s else 100, "is_suspended": s, "adj_event": False, "is_listed": True})
    p = build_panels(pd.DataFrame(rows))
    assert np.isnan(p.ret.iloc[1, 0]) and p.ret.iloc[2, 0] == pytest.approx(0.2)   # 12/10 - 1, gap skipped
    assert p.age.iloc[2, 0] == 3


def test_rs_rating_and_weekly_stages():
    """RS rating: the stock with the best weighted return gets 99; weekly stages classify a rising stock as Stage 2."""
    import numpy as np, pandas as pd
    from breadth import load_config
    from breadth.indicators.common import Panels
    from breadth.indicators.participation import rs_rating, compute_weekly_stages
    cfg = load_config()
    idx = pd.bdate_range("2020-01-01", periods=400)
    n = len(idx)
    lvl = pd.DataFrame({1: np.linspace(10, 20, n), 2: np.linspace(20, 10, n), 3: np.full(n, 15.0)}, index=idx)
    p = Panels(close=lvl.copy(), level=lvl, ret=lvl.pct_change(), volume=pd.DataFrame(1.0, index=idx, columns=lvl.columns),
               age=pd.DataFrame(np.tile(np.arange(1, n + 1)[:, None], (1, 3)), index=idx, columns=lvl.columns),
               listed=pd.DataFrame(True, index=idx, columns=lvl.columns))
    rs = rs_rating(p)
    last = rs.iloc[-1]
    assert last[1] == 99 and last[2] < last[3] < last[1]
    w = compute_weekly_stages(p, cfg)
    assert w["stage2_w"].iloc[-1] == 1 and w["stage4_w"].iloc[-1] == 1 and w["n_stage_w"].iloc[-1] == 3


def test_pnf_buy_sell_state_and_hlli():
    """P&F 2% boxes, 3-box reversal: buy on the first X column that exceeds the previous X top; sell on the mirror."""
    import numpy as np, pandas as pd
    from breadth.indicators.pnf import pnf_buy_state
    # up to 108 (X), reverse down to 96 (O), then up past 108 -> buy from the day it breaks (110)
    p = np.array([100, 102, 104.1, 106.2, 108.3, 104, 102, 100, 98, 96, 110, 112, 114, 116, 118], dtype=float)
    st = pnf_buy_state(p, 0.02, 3)
    assert list(st[:10]) == [0] * 10 and list(st[10:]) == [1] * 5
    # then fall below the previous O bottom (96) -> sell
    p2 = np.concatenate([p, [110, 105, 100, 95, 90]])
    st2 = pnf_buy_state(p2, 0.02, 3)
    assert st2[-1] == -1 and st2[14] == 1
    # HLLI hand example: NH=10, NL=5, issues=200 every day -> min/issues = 2.5%
    from breadth import load_config
    from breadth.indicators.classic import compute_classic
    idx = pd.bdate_range("2024-01-01", periods=80); n = len(idx)
    ad = pd.DataFrame({"advances": 100.0, "declines": 100.0, "n_ad": 200, "mcclellan_osc": -1.0, "up_volume": 1.0, "down_volume": 1.0, "up_volume_pct": 0.5}, index=idx)
    part = pd.DataFrame({"new_highs": 10, "new_lows": 5, "n_nhnl": 200}, index=idx)
    tasi = pd.Series(np.linspace(100, 110, n), index=idx)
    out = compute_classic(ad, part, tasi, load_config())
    assert abs(out["hlli"].iloc[-1] - 2.5) < 1e-9
    assert bool(out["hindenburg"].iloc[-1])        # NH 5% & NL 2.5% of issues, index up 50d, McClellan < 0, NH <= 2 NL
