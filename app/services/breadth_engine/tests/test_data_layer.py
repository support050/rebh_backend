"""Hand-calculated examples for the Phase 1 data layer."""
import numpy as np
import pandas as pd
import pytest

from breadth import load_config
from breadth.data.cleaner import build_calendar, build_symbols_master, normalize_prices
from breadth.data.corporate_actions import apply_adjustments, detect_events
from breadth.data.index import build_equal_weight, stock_returns


@pytest.fixture
def cfg():
    c = load_config()
    c["corporate_actions"]["detect_from_date"] = "2000-01-01"
    c["index"]["equal_weight_base_date"] = "2000-01-01"
    c["index"]["min_stocks_per_day"] = 1
    return c


def _raw(rows):
    cols = ["symbol", "date", "open", "high", "low", "close", "volume", "value", "trades", "market_cap"]
    df = pd.DataFrame(rows, columns=cols)
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype("int32")
    df["name"] = "X"; df["industry_group"] = "Banks"; df["sector"] = pd.NA
    df["source_row_id"] = range(len(df))
    return df


def test_suspension_rules_and_missing_day(cfg):
    # Day 2: volume 0 with carried close  -> suspended, close NaN
    # Day 3: close 0                       -> suspended
    # Day 4: absent in source but market open (symbol 2 traded) -> inserted, suspended
    rows = [
        (1, "2024-01-01", 10, 11, 9, 10, 100, 1000, 5, np.nan),
        (1, "2024-01-02", 0, 0, 0, 10, 0, 0, 0, np.nan),
        (1, "2024-01-03", 0, 0, 0, 0, 0, 0, 0, np.nan),
        (1, "2024-01-05", 12, 12, 11, 11, 50, 550, 3, np.nan),
        (2, "2024-01-04", 5, 5, 5, 5, 10, 50, 1, np.nan),
    ]
    raw = _raw(rows)
    cal = build_calendar(raw)
    out = normalize_prices(raw, cal, cfg)
    s1 = out[out.symbol == 1].set_index("date")
    assert list(s1.index.strftime("%Y-%m-%d")) == ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    assert s1.loc["2024-01-02", "is_suspended"] and np.isnan(s1.loc["2024-01-02", "close"])
    assert s1.loc["2024-01-03", "is_suspended"]
    assert s1.loc["2024-01-04", "quality_flag"] == "row_inserted" and s1.loc["2024-01-04", "is_suspended"]
    assert s1.loc["2024-01-04", "last_close"] == 10          # forward-filled level
    assert not s1.loc["2024-01-05", "is_suspended"] and s1.loc["2024-01-05", "close"] == 11


def test_open_missing_and_hl_clamp(cfg):
    rows = [
        (1, "2024-01-01", 0, 10, 9, 10, 100, 1000, 5, np.nan),    # open 0 with volume -> missing
        (1, "2024-01-02", 10, 10, 9.5, 10.2, 100, 1020, 5, np.nan),  # close > high -> clamp high to 10.2
    ]
    raw = _raw(rows)
    out = normalize_prices(raw, build_calendar(raw), cfg)
    assert np.isnan(out.loc[0, "open"]) and out.loc[0, "quality_flag"] == "open_missing"
    assert out.loc[1, "high"] == 10.2 and out.loc[1, "quality_flag"] == "hl_clamped"


def test_bonus_detection_and_adjustment(cfg):
    # 1:3 bonus (4 new for 3 old): price 30 -> opens 22.5 ; shares x1.3333 (market cap constant)
    rows = [
        (1, "2024-01-01", 30, 30, 30, 30, 100, 3000, 1, 3000),
        (1, "2024-01-02", 30, 31, 30, 30, 100, 3000, 1, 3000),
        (1, "2024-01-03", 30, 30, 30, 30, 100, 3000, 1, 3000),
        (1, "2024-01-04", 30, 30, 30, 30, 100, 3000, 1, 3000),
        (1, "2024-01-05", 22.5, 23, 22, 23, 100, 2300, 1, 3000 * 23 / 22.5),
        (1, "2024-01-06", 23, 23, 23, 23, 100, 2300, 1, 3000 * 23 / 22.5),
    ]
    raw = _raw(rows)
    clean = normalize_prices(raw, build_calendar(raw), cfg)
    ev = detect_events(clean, cfg)
    assert len(ev) == 1
    e = ev.iloc[0]
    assert e["ex_date"] == pd.Timestamp("2024-01-05")
    assert e["action_type"] == "bonus_split"
    assert e["factor"] == pytest.approx(4 / 3, abs=1e-6)          # snapped to 1.3333
    adj = apply_adjustments(clean, ev)
    a = adj.set_index("date")
    assert a.loc["2024-01-04", "close_adj"] == pytest.approx(22.5)  # 30 / 1.3333
    assert a.loc["2024-01-04", "volume_adj"] == pytest.approx(133.3333)
    assert a.loc["2024-01-05", "close_adj"] == 23                  # ex-day untouched
    assert a.loc["2024-01-05", "adj_event"]
    r = stock_returns(adj).set_index("date")["ret"]
    assert np.isnan(r.loc["2024-01-05"])                           # ex-day return excluded
    assert r.loc["2024-01-06"] == pytest.approx(0.0)


def test_rights_issue_classification(cfg):
    # shares x2.5 but price only falls 30% -> rights issue, factor = gap splice (not snapped)
    rows = [(1, f"2024-01-0{d}", 20, 20, 20, 20, 100, 2000, 1, 2000) for d in range(1, 5)]
    rows += [(1, "2024-01-05", 14, 14.2, 13.9, 14.1, 100, 1410, 1, 2000 * 2.5 * 14.1 / 20)]
    raw = _raw(rows)
    clean = normalize_prices(raw, build_calendar(raw), cfg)
    ev = detect_events(clean, cfg)
    assert len(ev) == 1 and ev.iloc[0]["action_type"] == "rights_issue"
    assert ev.iloc[0]["factor"] == pytest.approx(20 / 14)


def test_no_event_below_min_price_or_first_days(cfg):
    # 1.9 -> 2.1 is +10.5% from tick size on a cheap stock: must NOT be an event (min_price 3.0)
    rows = [(1, f"2024-01-0{d}", 1.9, 1.9, 1.9, 1.9, 100, 190, 1, np.nan) for d in range(1, 5)]
    rows += [(1, "2024-01-05", 2.3, 2.3, 2.3, 2.3, 100, 230, 1, np.nan)]
    raw = _raw(rows)
    clean = normalize_prices(raw, build_calendar(raw), cfg)
    assert detect_events(clean, cfg).empty


def test_equal_weight_index_hand_example(cfg):
    # Day1: base. Day2: A +10%, B -10%, C suspended -> EW ret = 0.0
    # Day3: A +10%, B +10%, C returns after suspension: C prev valid close 5 -> 5.5 = +10% -> EW = +10%
    rows = [
        (1, "2024-01-01", 10, 10, 10, 10, 1, 10, 1, np.nan),
        (1, "2024-01-02", 11, 11, 11, 11, 1, 11, 1, np.nan),
        (1, "2024-01-03", 12.1, 12.1, 12.1, 12.1, 1, 12.1, 1, np.nan),
        (2, "2024-01-01", 10, 10, 10, 10, 1, 10, 1, np.nan),
        (2, "2024-01-02", 9, 9, 9, 9, 1, 9, 1, np.nan),
        (2, "2024-01-03", 9.9, 9.9, 9.9, 9.9, 1, 9.9, 1, np.nan),
        (3, "2024-01-01", 5, 5, 5, 5, 1, 5, 1, np.nan),
        (3, "2024-01-02", 0, 0, 0, 5, 0, 0, 0, np.nan),
        (3, "2024-01-03", 5.5, 5.5, 5.5, 5.5, 1, 5.5, 1, np.nan),
    ]
    raw = _raw(rows)
    clean = normalize_prices(raw, build_calendar(raw), cfg)
    adj = apply_adjustments(clean, detect_events(clean, cfg))
    master = build_symbols_master(raw, cfg)
    ew = build_equal_weight(adj, master, cfg).set_index("date")
    assert ew.loc["2024-01-01", "close"] == pytest.approx(1000)       # first day: no prev close -> 0
    assert ew.loc["2024-01-02", "ew_ret"] == pytest.approx(0.0)
    assert ew.loc["2024-01-02", "n_stocks"] == 2
    assert ew.loc["2024-01-03", "ew_ret"] == pytest.approx(0.10)
    assert ew.loc["2024-01-03", "close"] == pytest.approx(1100)


def test_master_universe_flags(cfg):
    rows = [(1010, "2024-01-01", 1, 1, 1, 1, 1, 1, 1, np.nan), (4330, "2024-01-01", 1, 1, 1, 1, 1, 1, 1, np.nan)]
    raw = _raw(rows)
    m = build_symbols_master(raw, cfg).set_index("symbol")
    assert m.loc[1010, "in_universe"] and not m.loc[4330, "in_universe"]
    assert m.loc[1010, "sector"] == "Financials"
