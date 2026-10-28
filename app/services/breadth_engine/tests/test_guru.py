# -*- coding: utf-8 -*-
"""Guru Stock Scans — offline unit contract for indicators/guru.py.

Synthetic Panels only (no DB, no parquet, no network), mirroring
test_indicators.py. Each scan is asserted on a hand-built series so the four
review checkpoints (prior-52w pivot, 97-Club recency, HVC rvol ordering,
CANSLIM = template + RS + momentum) are pinned. Run from breadth_engine/:
    python -m pytest tests/test_guru.py
"""
import numpy as np
import pandas as pd
import pytest

from breadth import load_config
from breadth.indicators.common import Panels
from breadth.indicators.guru import SCAN_ORDER, _weinstein_stage, compute_guru, scan_masks

N = 300          # > 253 so the 52-week / RS / template lookbacks are all live
IDX = pd.bdate_range("2024-01-01", periods=N)


@pytest.fixture
def cfg():
    return load_config()


def _mk(prices, vols=None):
    """prices/vols: {symbol: callable(idx)->array}. Volume defaults to flat 100."""
    close = pd.DataFrame({s: np.asarray(f(IDX), float) for s, f in prices.items()}, index=IDX)
    level = close.ffill()
    ret = close / level.shift() - 1
    _default_vol = lambda i: np.full(len(i), 100.0)
    vol = pd.DataFrame({s: np.asarray(((vols or {}).get(s) or _default_vol)(IDX), float)
                        for s in close.columns}, index=IDX)
    age = pd.DataFrame(np.arange(1, N + 1)[:, None].repeat(len(close.columns), 1), index=IDX, columns=close.columns)
    listed = pd.DataFrame(True, index=IDX, columns=close.columns)
    return Panels(close=close, level=level, ret=ret, volume=vol, age=age, listed=listed)


def _rising(a=10.0, b=60.0):
    return lambda i: np.linspace(a, b, len(i))


# ── Minervini trend template (imported primitive, verbatim) ────────────────

def test_minervini_passes_clean_uptrend(cfg):
    m = scan_masks(_mk({1: _rising()}), cfg)
    assert bool(m["minervini"].iloc[-1, 0]) is True


def test_minervini_fails_downtrend(cfg):
    m = scan_masks(_mk({1: lambda i: np.linspace(60, 20, len(i))}), cfg)
    assert bool(m["minervini"].iloc[-1, 0]) is False


# ── Qullamaggie: PRIOR 52-week high + volume expansion (checkpoint 1) ───────

def test_qullamaggie_needs_prior_high_and_volume(cfg):
    # declining series (prior-252 high ~46.9) that gaps to 55 on a 3x volume day
    px = lambda i: np.concatenate([np.linspace(50, 30, len(i) - 1), [55.0]])
    v_last = lambda i: np.concatenate([np.full(len(i) - 1, 100.0), [300.0]])
    m = scan_masks(_mk({1: px}, {1: v_last}), cfg)
    assert bool(m["qullamaggie"].iloc[-1, 0]) is True
    # same breakout WITHOUT the volume expansion is not a Qullamaggie signal
    m_novol = scan_masks(_mk({1: px}), cfg)
    assert bool(m_novol["qullamaggie"].iloc[-1, 0]) is False


# ── Stockbee movers ─────────────────────────────────────────────────────────

def test_sb_daily_movers(cfg):
    px = lambda i: np.concatenate([np.full(len(i) - 1, 20.0), [22.0]])   # +10% on the last day
    v_last = lambda i: np.concatenate([np.full(len(i) - 1, 100.0), [200.0]])
    m = scan_masks(_mk({1: px}, {1: v_last}), cfg)
    assert bool(m["sb_4pct"].iloc[-1, 0]) is True      # >=4%
    assert bool(m["sb_9m"].iloc[-1, 0]) is True        # >=9% (relative-adapted "9 points")


def test_sb_weekly_20pct(cfg):
    def px(i):
        x = np.full(len(i), 20.0); x[-6] = 16.0       # 20/16-1 = +25% over 5 sessions
        return x
    m = scan_masks(_mk({1: px}), cfg)
    assert bool(m["sb_weekly"].iloc[-1, 0]) is True


# ── 97 Club: whole-52w reference high + recent-high test (checkpoint 2) ─────

def test_club_97_at_recent_high(cfg):
    m = scan_masks(_mk({1: _rising()}), cfg)          # rising: today IS the 52w high
    assert bool(m["club_97"].iloc[-1, 0]) is True


def test_club_97_rejects_far_from_high(cfg):
    def px(i):                                          # peaked mid-year, now 30% below
        x = np.linspace(20, 60, len(i) // 2)
        return np.concatenate([x, np.linspace(60, 42, len(i) - len(x))])
    m = scan_masks(_mk({1: px}), cfg)
    assert bool(m["club_97"].iloc[-1, 0]) is False


# ── HVC: rvol ordering (checkpoint 3) ───────────────────────────────────────

def test_hvc_ranks_by_relative_volume(cfg):
    # two flat-price stocks; the one with the volume spike has higher rvol
    v_spike = lambda i: np.concatenate([np.full(len(i) - 1, 100.0), [500.0]])
    p = _mk({1: _rising(), 2: _rising()}, {1: v_spike})
    m = scan_masks(p, cfg)
    assert bool(m["hvc"].iloc[-1, 0]) and bool(m["hvc"].iloc[-1, 1])   # top_n=20, both fit
    # a name with no volume history (rvol NaN) is never in HVC
    p_zero = _mk({1: _rising()}, {1: lambda i: np.zeros(len(i))})
    assert bool(scan_masks(p_zero, cfg)["hvc"].iloc[-1, 0]) is False


# ── CANSLIM: template AND RS>=80 AND momentum (checkpoint 4) ────────────────

def test_canslim_requires_template_plus_rs_plus_momentum(cfg):
    assert bool(scan_masks(_mk({1: _rising()}), cfg)["canslim"].iloc[-1, 0]) is True
    # strong RS/momentum but NOT in a trend template (downtrend) -> no CANSLIM
    down = lambda i: np.linspace(60, 20, len(i))
    assert bool(scan_masks(_mk({1: down}) , cfg)["canslim"].iloc[-1, 0]) is False


# ── TML: template AND latest-quarter EPS YoY >= 25% (quarterly only) ────────

def test_tml_quarterly_gate(cfg):
    p = _mk({1: _rising()})
    assert bool(scan_masks(p, cfg, {"1": {"basis": "quarterly", "latest_yoy_pct": 30.0}})["tml"].iloc[-1, 0]) is True
    assert bool(scan_masks(p, cfg, {"1": {"basis": "quarterly", "latest_yoy_pct": 10.0}})["tml"].iloc[-1, 0]) is False
    assert bool(scan_masks(p, cfg, {"1": {"basis": "annual", "latest_yoy_pct": 40.0}})["tml"].iloc[-1, 0]) is False   # annual ignored
    assert bool(scan_masks(p, cfg, None)["tml"].iloc[-1, 0]) is False                                                    # no eps map


# ── stage classification (mirrors pro_dashboard.stage) ──────────────────────

def test_weinstein_stage_values(cfg):
    st = _weinstein_stage(_mk({1: _rising()}), cfg).iloc[-1, 0]
    assert int(st) == 2                                   # advancing
    st_d = _weinstein_stage(_mk({1: lambda i: np.linspace(60, 20, len(i))}), cfg).iloc[-1, 0]
    assert int(st_d) == 4                                 # declining


# ── compute_guru snapshot shape + occurrence ordering ───────────────────────

def test_compute_guru_snapshot(cfg):
    p = _mk({1: _rising(), 2: lambda i: np.linspace(60, 20, len(i))})
    res = compute_guru(p, cfg, {"1": {"basis": "quarterly", "latest_yoy_pct": 40.0}})
    assert set(res["columns"]) == set(SCAN_ORDER)
    assert res["date"] == IDX[-1].strftime("%Y-%m-%d")
    # symbol 1 (the leader) is in many columns; its count is the highest and its
    # badge carries an RS in 1..99 and a stage
    assert res["rows"]["1"]["rs"] is not None and 1 <= res["rows"]["1"]["rs"] <= 99
    assert res["rows"]["1"]["stage"] in (1, 2, 3, 4)
    assert res["rows"]["1"]["count"] >= res["rows"].get("2", {}).get("count", 0)
    # every listed symbol appears in exactly `count` columns
    for sym, row in res["rows"].items():
        present = sum(1 for k in SCAN_ORDER if int(sym) in res["columns"][k])
        assert present == row["count"]
