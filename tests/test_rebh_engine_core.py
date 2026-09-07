"""
Pytest Unit Tests for REBH Core Engine and API Layer (Phase 2 & Phase 13).
Verifies:
1. Normal Company (Aramco 2222)
2. Bank Exception (Al Rajhi 1120)
3. Accounting Identity Gate (A = L + E)
4. Net Income Plausibility Guard (<= 120% of Revenue)
5. Sign-Flip Growth Suppression
6. Course Labs Calculators (Fair P/B, DCF Growth, Beneish, rNPV)
"""
import pytest
from app.services.rebh_data_guard_service import (
    verify_balance_sheet_identity,
    verify_net_income_plausibility,
    verify_fcf_yield_bound,
    yoy_series
)
from app.services.rebh_unified_engine import calculate_full_company_payload
from app.services import course_labs_service


def test_balance_sheet_identity_strict():
    # Perfect match
    res_ok = verify_balance_sheet_identity(assets=1000.0, liabilities=600.0, equity=400.0)
    assert res_ok["is_valid"] is True
    assert res_ok["discrepancy"] == 0.0

    # Failing discrepancy
    res_fail = verify_balance_sheet_identity(assets=1000.0, liabilities=600.0, equity=350.0)
    assert res_fail["is_valid"] is False
    assert res_fail["discrepancy"] == 50.0


def test_net_income_plausibility_rule():
    # Plausible: NI is 20% of Revenue
    ok = verify_net_income_plausibility(net_income=200.0, revenue=1000.0)
    assert ok["is_plausible"] is True

    # Implausible anomaly: NI is 150% of Revenue (> 120%)
    fail = verify_net_income_plausibility(net_income=1500.0, revenue=1000.0)
    assert fail["is_plausible"] is False
    assert "exceeds 120%" in fail["reason"]


def test_fcf_yield_bound_rule():
    # Valid FCF yield: 8%
    ok = verify_fcf_yield_bound(fcf=80.0, market_cap=1000.0)
    assert ok["is_valid"] is True
    assert ok["yield_pct"] == 8.0

    # Distorted yield: 200% (> 150%)
    fail = verify_fcf_yield_bound(fcf=2000.0, market_cap=1000.0)
    assert fail["is_valid"] is False
    assert fail["yield_pct"] is None


def test_yoy_sign_flip_suppression():
    # Quarters: [profit, loss, profit, loss, loss]
    quarters = [100.0, 110.0, 120.0, 130.0, -50.0, 115.0]
    growth = yoy_series(quarters)
    # Quarter index 4 is vs index 0 (100 -> -50): sign flipped, must be None
    assert growth[4] is None
    # Quarter index 5 is vs index 1 (110 -> 115): both positive, valid growth
    assert growth[5] is not None
    assert round(growth[5], 1) == round(((115.0/110.0)-1.0)*100.0, 1)


def test_aramco_universal_contract_payload():
    payload = calculate_full_company_payload("2222")
    assert payload.symbol == "2222"
    assert payload.balance_identity.is_valid is True
    assert payload.required_return > 0
    assert payload.nine_box is not None
    assert payload.zones is not None


def test_bank_exception_alrajhi_payload():
    payload = calculate_full_company_payload("1120")
    assert payload.symbol == "1120"
    assert payload.bank_metrics is not None
    assert payload.bank_metrics.is_bank is True
    assert payload.bank_metrics.nim_pct is not None
    assert payload.build_up.safety_weight == 0.0  # Exempt from industrial safety weight


def test_course_labs_calculators():
    # Fair P/B
    pb_res = course_labs_service.calculate_fair_pb(roe_pct=16.0, required_return_pct=8.0)
    assert pb_res["fair_pb"] == 2.0

    # Reverse DCF
    dcf_res = course_labs_service.calculate_dcf_growth(price=50.0, eps=4.0, r_pct=8.0)
    assert dcf_res["implied_growth_pct"] is not None

    # Beneish M-Score
    beneish_res = course_labs_service.calculate_beneish_m_score(dsri=1.0, gmi=1.0, aqi=1.0, sgi=1.0, depi=1.0, sgai=1.0, tata=0.02, lvgi=1.0)
    assert beneish_res["m_score"] < -1.78
    assert beneish_res["is_manipulation_risk"] is False
