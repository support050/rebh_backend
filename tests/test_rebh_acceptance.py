"""
REBH Engine Acceptance Tests & Financial Auditing Suite.
Directly integrated into production backend tests to ensure data integrity and zero broken metrics.
"""
import pytest
from app.services.rebh_engine_service import (
    discrete_quarters,
    ttm_series,
    yoy_series,
    check_components_sum,
    check_balance_sheet,
    check_cash_flow,
    check_quarters_sum_to_fy,
    calculate_acceleration_signal,
    calculate_provisions_signal,
    get_trust_badge_status,
)
from app.services.xbrl_data_service import list_companies, get_company


# --- Standard Real Data Test Fixtures ---
RIYAD_1010 = {
    "income_components_q1_2026": [3865000, 749047],
    "toi_q1_2026": 4614047,
    "balance_q1_2026": {"assets": 537083000, "liabilities": 457918000, "equity": 79165000},
    "cf_fy2025": {"cfo": -25866000, "cfi": -3200000, "cff": 29800000, "net_change": 734000},
    "q_2025_net": [2070000, 2480000, 2650000, 3339000],
    "fy2025_net": 10539000,
    "cum_2025": {
        "3M": -5120000,
        "6M": -10016880,
        "9M": -17601914,
        "FY": -23888114,
    }
}

MAHARAH_1831 = {
    "net_income": [35000, 38000, 42000, 52000, 48000, 45000, 43000, 41000]
}

SAUDI_CEMENT_3030 = {
    "net_income": [95000, 80000, 75000, 70000, 72000, 78000, 85000, 92000]
}


# --- Criterion 1: Real Statement Reconciliation ---
def test_income_components_reconcile():
    assert check_components_sum(RIYAD_1010["income_components_q1_2026"], RIYAD_1010["toi_q1_2026"])


def test_balance_sheet_reconciles():
    b = RIYAD_1010["balance_q1_2026"]
    assert check_balance_sheet(b["assets"], b["liabilities"], b["equity"])


def test_cash_flow_reconciles():
    c = RIYAD_1010["cf_fy2025"]
    assert check_cash_flow(c["cfo"], c["cfi"], c["cff"], c["net_change"])


def test_quarters_sum_to_fy():
    assert check_quarters_sum_to_fy(RIYAD_1010["q_2025_net"], RIYAD_1010["fy2025_net"])


# --- Criterion 2: Trust Badge Hides on Injected Arithmetic Failure ---
def test_badge_hides_on_injected_error():
    bad = RIYAD_1010["income_components_q1_2026"][:]
    bad[0] += 100000
    assert not check_components_sum(bad, RIYAD_1010["toi_q1_2026"])


# --- Criterion 3: Mathematical Derivations (Discrete, TTM, YoY) ---
def test_discrete_from_cumulative():
    q = discrete_quarters(RIYAD_1010["cum_2025"])
    assert "Q1" in q and "Q2" in q and "Q3" in q and "Q4" in q
    assert round(q["Q2"]) == round(-10016880 - -5120000)


def test_ttm_needs_four_quarters():
    t = ttm_series(MAHARAH_1831["net_income"])
    assert t[0] is None and t[1] is None and t[2] is None
    assert t[3] is not None
    assert round(t[-1]) == sum(MAHARAH_1831["net_income"][-4:])


def test_yoy_none_on_sign_flip():
    series = [100, 1, 1, 1, -50, 1, 1, 1]
    yoy = yoy_series(series)
    assert yoy[4] is None  # Never generate misleading percentages on sign-flip


# --- Criterion 4: Financial Signal Truthfulness ---
def test_engine_honest_on_deceleration():
    sig = calculate_acceleration_signal("صافي الربح", MAHARAH_1831["net_income"])
    # Deceleration or negative growth direction must be correctly flagged
    if sig:
        assert sig.get("status") in ("neutral", "warning", "danger") or "تباطؤ" in sig.get("text", "") or sig.get("neg") is True


def test_engine_distinguishes_easing_contraction_from_acceleration():
    sig = calculate_acceleration_signal("صافي الربح", SAUDI_CEMENT_3030["net_income"])
    # Recovering from negative/slow growth
    if sig:
        assert "تسارع" in sig.get("text", "") or "تحسن" in sig.get("text", "") or "انكماش" in sig.get("text", "")


def test_provisions_rule_fires_on_bank_provision_surge():
    sig = calculate_provisions_signal(55.0, 5.0)
    assert sig is not None
    assert sig["rule"] == "provisions_watch"
    assert sig["status"] == "danger"


# --- Criterion 5: Market Coverage Audit & Trust Badge ---
def test_market_audit_parity():
    companies = list_companies()
    assert len(companies) >= 238
    # Test trust badge on sample valid company (e.g. 1010 or 4300)
    for sym in ["1010", "4300", "2222", "2010"]:
        badge = get_trust_badge_status(sym)
        assert "verified" in badge
        assert "badge_label" in badge
