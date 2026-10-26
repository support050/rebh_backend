"""Regression guard: the REBH engine's twelve-month profit numerator.

Two defects lived in `rebh_unified_engine.calculate_full_company_payload` and
both moved every ROE-anchored letter (Profitability, the Growth proxy, the
financial-sector Safety rule):

  E1. ROE/ROA divided the LATEST reported income-statement window by full
      equity. Between filing cycles that window is a 3-month cumulative stub, so
      the ratio was a quarter of a year over a whole balance sheet — Al Othaim
      (4001) read 4.5% where the trailing year gives 18.7%.

  E2. Once a twelve-month numerator is built by de-cumulating cumulative
      windows, a filing whose annual row is corrupt poisons the derived Q4
      (annual - 9M). Saudi Exchange 2060 published full-year revenue BELOW its
      own nine-month revenue, producing a phantom -2.49bn quarter and an ROE of
      -39% against a disclosed +5.5%.

A shrinking annual REVENUE is arithmetically impossible under cumulative YTD
reporting, so the engine refuses the ratio rather than pick the least-bad guess.
Net profit is not tested for corruption: a Q4 loss is legitimate.
"""
import pytest

from app.services.rebh_unified_engine import (
    _inconsistent_fiscal_year,
    _period_months,
    _twelve_month_profit,
)


# --- window labels -----------------------------------------------------------

@pytest.mark.parametrize("label,expected", [
    ("2025-01_2025-12", 12),
    ("2025-01_2025-09", 9),
    ("2025-04_2025-06", 3),
    ("2025-10_2025-12", 3),
    ("2025-01_2026-01", 13),
])
def test_period_months_reads_cumulative_spans(label, expected):
    assert _period_months(label) == expected


@pytest.mark.parametrize("label", ["", None, "2025-12-31", "2025-01_2025", "abc_def"])
def test_period_months_returns_none_for_non_windows(label):
    assert _period_months(label) is None


# --- E1: a year-long numerator, never an extrapolation -----------------------

NET_2025 = {
    "2025-01_2025-03": 60_000_000.0,
    "2025-01_2025-06": 120_000_000.0,
    "2025-01_2025-09": 180_000_000.0,
    "2025-01_2025-12": 240_000_000.0,
    "2026-01_2026-03": 70_000_000.0,   # the freshest window is a stub
}
REV_2025 = {
    "2025-01_2025-03": 500_000_000.0,
    "2025-01_2025-06": 1_000_000_000.0,
    "2025-01_2025-09": 1_500_000_000.0,
    "2025-01_2025-12": 2_000_000_000.0,
    "2026-01_2026-03": 520_000_000.0,
}


def test_complete_trailing_year_wins_over_the_latest_stub():
    val, basis = _twelve_month_profit(NET_2025, REV_2025, 190_000_000.0, True, 70_000_000.0)
    assert basis == "ttm_4q"
    assert val == 190_000_000.0


def test_missing_trailing_year_falls_back_to_the_reported_annual_window():
    val, basis = _twelve_month_profit(NET_2025, REV_2025, 70_000_000.0, False, 70_000_000.0)
    assert basis == "annual_fy"
    assert val == 240_000_000.0, "the disclosed FY figure, not a multiplied stub"


def test_no_annual_figure_is_reported_as_partial_window_never_annualised():
    net = {"2026-01_2026-03": 70_000_000.0}
    rev = {"2026-01_2026-03": 520_000_000.0}
    val, basis = _twelve_month_profit(net, rev, 70_000_000.0, False, 70_000_000.0)
    assert basis == "partial_window"
    assert val == 70_000_000.0, "4x-ing a quarter would invent earnings"


def test_annual_candidate_is_the_latest_fiscal_year_not_a_discrete_quarter():
    net = {
        "2024-01_2024-12": 100_000_000.0,
        "2025-01_2025-12": 240_000_000.0,
        "2025-10_2025-12": 60_000_000.0,   # 3-month span, must not be picked
    }
    val, basis = _twelve_month_profit(net, None, 240_000_000.0, False, 60_000_000.0)
    assert basis == "annual_fy"
    assert val == 240_000_000.0


# --- E2: refuse a numerator built on an impossible filing --------------------

# Al Saudi Exchange 2060 shape: FY revenue smaller than its own 9M revenue and
# no explicit Q4 window, so the engine had to derive Q4 = FY - 9M (negative).
CORRUPT_REV = {
    "2025-01_2025-03": 800_000_000.0,
    "2025-01_2025-06": 1_700_000_000.0,
    "2025-01_2025-09": 2_531_227_000.0,
    "2025-01_2025-12": 2_488_722_000.0,
}
CORRUPT_NET = {
    "2025-01_2025-09": 1_022_577_000.0,
    "2025-01_2025-12": -1_466_168_000.0,
}


def test_impossible_annual_window_is_detected_on_the_flow_that_cannot_shrink():
    assert _inconsistent_fiscal_year(CORRUPT_REV) == "2025"


def test_net_profit_is_never_used_as_a_corruption_detector():
    # Same falling net profit, but revenue is consistent -> a real Q4 loss, so
    # the ratio must still be published (the guard tests revenue only).
    val, basis = _twelve_month_profit(CORRUPT_NET, REV_2025, 190_000_000.0, True, 40_000_000.0)
    assert basis == "ttm_4q"
    assert val == 190_000_000.0


def test_consistent_cumulative_series_passes():
    assert _inconsistent_fiscal_year(REV_2025) is None
    assert _inconsistent_fiscal_year(NET_2025) is None
    assert _inconsistent_fiscal_year({}) is None
    assert _inconsistent_fiscal_year(None) is None


def test_negative_full_year_revenue_is_flagged():
    vals = {
        "2025-01_2025-09": 119_179_405.0,
        "2025-01_2025-12": -1_249_708.0,   # 2140 shape: impossible turnover
    }
    assert _inconsistent_fiscal_year(vals) == "2025"
    val, basis = _twelve_month_profit(CORRUPT_NET, vals, 1.0, True, 40_000_000.0)
    assert val is None
    assert basis == "rejected-corrupt-fy-2025-revenue"


def test_deeper_full_year_loss_is_not_treated_as_corruption():
    # Helper rule is about a flow that only grows; the engine only applies it to
    # revenue, so a genuine impairment year keeps its ROE.
    net = {"2025-01_2025-09": 1_000_000.0, "2025-01_2025-12": -5_000_000.0}
    rev = {"2025-01_2025-09": 9_000_000.0, "2025-01_2025-12": 12_000_000.0}
    val, basis = _twelve_month_profit(net, rev, -5_000_000.0, False, -5_000_000.0)
    assert basis == "annual_fy"
    assert val == -5_000_000.0


def test_ratio_is_refused_when_the_only_q4_is_derived_from_a_broken_annual_row():
    val, basis = _twelve_month_profit(
        CORRUPT_NET, CORRUPT_REV, -3_765_929_000.0, True, 40_000_000.0
    )
    assert val is None, "a phantom negative quarter must not become ROE"
    assert basis.startswith("rejected-corrupt-fy-2025")


def test_explicitly_disclosed_q4_survives_a_broken_annual_row():
    net = dict(CORRUPT_NET)
    net["2025-10_2025-12"] = 120_000_000.0   # the company DID publish Q4
    val, basis = _twelve_month_profit(net, CORRUPT_REV, 300_000_000.0, True, 120_000_000.0)
    assert basis == "ttm_4q"
    assert val == 300_000_000.0


def test_provenance_basis_is_a_single_disclosed_string():
    _, basis = _twelve_month_profit(CORRUPT_NET, CORRUPT_REV, -1.0, True, 40_000_000.0)
    assert isinstance(basis, str) and basis
    assert basis in {
        "ttm_4q", "annual_fy", "partial_window",
    } or basis.startswith("rejected-corrupt-fy-")
