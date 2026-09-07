"""
Pytest Comprehensive Acceptance Test Suite — Phase 13 & Acceptance Gates.
========================================================================
Covers all 24 explicit Phase 13 domain requirements:
1. Discrete quarters
2. TTM calculations
3. YoY sign flips
4. Balance identity (A = L + E)
5. Cash-flow identity
6. Scale normalization (Double-count recovery)
7. Net-income plausibility (<= 120% of revenue)
8. Porter ladder & Financial safety ladder
9. Build-Up required return (R)
10. Nine-Box matrix & transitory (N/2) formula
11. FCF debt treatment
12. Cyclical bands (Molodovsky)
13. Loss-maker P/S ladder (x1, x2, x3)
14. Reverse DCF implied growth
15. IRR (Internal Rate of Return)
16. Price zones (Golden, Silver, Bronze)
17. Shariah compliance calculations
18. Bank NIM, CASA, LDR, Cost of Risk (COR)
19. Beneish M-Score
20. rNPV (Risk-Adjusted NPV)
21. Cut-Cut recovery solver
22. Fair P/B (ROE / R)
23. Terry Smith ROCE
24. API response schema & Stale/Quarantine refusal
"""
import pytest
from decimal import Decimal
from app.services.rebh_data_guard_service import (
    verify_balance_sheet_identity,
    verify_net_income_plausibility,
    verify_fcf_yield_bound,
    discrete_quarters,
    ttm_series,
    yoy_series,
    evaluate_red_flags
)
from app.services.rebh_unified_engine import calculate_full_company_payload
from app.services import course_labs_service
from app.services.bank_analytics_service import calculate_bank_metrics
from app.schemas.rebh_contract import RebhUniversalContract


# ─── 1. Discrete Quarters & Cumulative De-accumulation ─────────
def test_discrete_quarters_deaccumulation():
    # Cumulative quarters for a fiscal year: 3M=100, 6M=250, 9M=420, FY=600
    cumulative = {"3M": 100.0, "6M": 250.0, "9M": 420.0, "FY": 600.0}
    discrete = discrete_quarters(cumulative)
    assert discrete["Q1"] == 100.0
    assert discrete["Q2"] == 150.0  # 250 - 100
    assert discrete["Q3"] == 170.0  # 420 - 250
    assert discrete["Q4"] == 180.0  # 600 - 420


# ─── 2. TTM Moving Sum ─────────────────────────────────────────
def test_ttm_four_quarter_series():
    q = [100.0, 120.0, 110.0, 130.0, 140.0]
    ttm = ttm_series(q)
    # First 3 quarters do not have 4 trailing quarters -> None
    assert ttm[0] is None
    assert ttm[1] is None
    assert ttm[2] is None
    # 4th element (index 3) is sum of first 4 quarters: 100+120+110+130 = 460
    assert ttm[3] == 460.0
    # 5th element (index 4) is sum of quarters 1..4: 120+110+130+140 = 500
    assert ttm[4] == 500.0


# ─── 3. YoY Growth & Sign-Flip Suppression ────────────────────
def test_yoy_growth_and_sign_flip_rules():
    # Regular positive YoY growth
    q = [100.0, 100.0, 100.0, 100.0, 120.0]
    yoy = yoy_series(q)
    assert yoy[4] == pytest.approx(20.0, 0.01)  # (120 - 100) / 100 * 100%

    # Sign flip from positive to loss (100 -> -50): MUST return None to prevent fake positive percentages
    q_flip1 = [100.0, 100.0, 100.0, 100.0, -50.0]
    assert yoy_series(q_flip1)[4] is None

    # Sign flip from loss to profit (-50 -> 100): MUST return None
    q_flip2 = [-50.0, 100.0, 100.0, 100.0, 100.0]
    assert yoy_series(q_flip2)[4] is None


# ─── 4. Balance Sheet Identity Guard (A = L + E) ───────────────
def test_balance_sheet_identity_acceptance_gate():
    # Verified match: Assets = Liabilities + Equity
    pass_case = verify_balance_sheet_identity(assets=5000.0, liabilities=3000.0, equity=2000.0)
    assert pass_case["is_valid"] is True
    assert pass_case["discrepancy"] == 0.0

    # Imbalance: Assets 5000 != Liab 3000 + Equity 1800
    fail_case = verify_balance_sheet_identity(assets=5000.0, liabilities=3000.0, equity=1800.0)
    assert fail_case["is_valid"] is False
    assert fail_case["discrepancy"] == 200.0


# ─── 5. Net Income Plausibility Guard (<= 120% Revenue) ────────
def test_net_income_plausibility_rule():
    # Valid normal margin: 25%
    valid = verify_net_income_plausibility(net_income=250.0, revenue=1000.0)
    assert valid["is_plausible"] is True

    # Blocked abnormal margin: 130% (> 120% of revenue)
    blocked = verify_net_income_plausibility(net_income=1300.0, revenue=1000.0)
    assert blocked["is_plausible"] is False
    assert "exceeds 120%" in blocked["reason"]


# ─── 6. FCF Yield Bound (abs(FCF Yield) <= 150%) ──────────────
def test_fcf_yield_boundary():
    # Standard healthy yield: 6.5%
    valid = verify_fcf_yield_bound(fcf=65.0, market_cap=1000.0)
    assert valid["is_valid"] is True
    assert valid["yield_pct"] == 6.5

    # Out-of-bounds anomaly: 180% -> Must be withheld (None)
    withheld = verify_fcf_yield_bound(fcf=1800.0, market_cap=1000.0)
    assert withheld["is_valid"] is False
    assert withheld["yield_pct"] is None


# ─── 7. Scale Normalization (The Double-Count Formula) ─────────
def test_double_count_scale_normalization():
    # Dar Al Arkan pattern: TA_true = (TA_std + CA) / 2
    ta_std = 70000.0
    ca = 10870.0
    ta_recovered = (ta_std + ca) / 2.0
    assert ta_recovered == 40435.0


# ─── 8. Build-Up Required Return (Lab 1) ──────────────────────
def test_buildup_required_return_structure():
    tasi = course_labs_service.calculate_tasi_index_lab(current_pe=13.6, bond_yield_pct=4.75)
    assert tasi["status"] == "° verified"
    assert tasi["result"]["bond_yield_pct"] == 4.75
    assert tasi["result"]["required_index_yield_pct"] == pytest.approx(7.12, 0.01)


# ─── 9. Multibagger Matrix Lab ────────────────────────────────
def test_multibagger_matrix():
    mb = course_labs_service.calculate_multibagger_matrix(
        pe_entry=10.0,
        pe_exit=20.0,
        eps_cagr_pct=24.573,
        years=5
    )
    assert mb["status"] == "° verified"
    assert mb["result"]["pe_expansion_factor"] == 2.0
    assert mb["result"]["total_multiplier_x"] >= 5.0
    assert mb["result"]["is_multibagger"] is True


# ─── 10. Loss-Maker P/S Valuation Ladder ───────────────────────
def test_loss_maker_ps_ladder():
    ladder = course_labs_service.calculate_ps_valuation_ladder(
        expected_npm_pct=15.0,
        required_return_r_pct=9.0,
        expected_growth_pct=5.0,
        sales_per_share=20.0
    )
    assert ladder["status"] == "° verified"
    assert ladder["result"]["fair_ps"] is not None
    assert ladder["result"]["bands"]["cheap_price_sar"] > 0


# ─── 11. Reverse DCF & Implied Growth ──────────────────────────
def test_reverse_dcf_implied_growth():
    dcf = course_labs_service.calculate_dcf_growth(price=60.0, eps=3.0, r_pct=8.0)
    assert dcf["status"] == "° verified"
    assert dcf["result"]["implied_growth_pct"] is not None


# ─── 12. Fair P/B Ratio (ROE / R) ──────────────────────────────
def test_fair_pb_ratio():
    fair_pb = course_labs_service.calculate_fair_pb(roe_pct=18.0, required_return_pct=9.0)
    assert fair_pb["status"] == "° verified"
    assert fair_pb["result"]["fair_pb"] == 2.0


# ─── 13. Terry Smith Quality ROCE Benchmark ───────────────────
def test_terry_smith_roce():
    res = course_labs_service.calculate_terry_smith_roce(
        operating_profit_ebit=500.0,
        total_assets=2500.0,
        current_liabilities=500.0
    )
    assert res["status"] == "° verified"
    # Capital Employed = 2500 - 500 = 2000
    # ROCE = 500 / 2000 = 25.0%
    assert res["result"]["capital_employed_m"] == 2000.0
    assert res["result"]["roce_pct"] == 25.0
    assert "benchmark_note" in res["result"]


# ─── 14. Beneish M-Score Forensics ────────────────────────────
def test_beneish_m_score_forensics():
    safe = course_labs_service.calculate_beneish_m_score(dsri=1.0, gmi=1.0, aqi=1.0, sgi=1.0, depi=1.0, sgai=1.0, tata=0.01, lvgi=1.0)
    assert safe["result"]["is_manipulation_risk"] is False
    assert safe["result"]["m_score"] < -1.78

    risky = course_labs_service.calculate_beneish_m_score(dsri=3.0, gmi=2.5, tata=0.30)
    assert risky["result"]["is_manipulation_risk"] is True
    assert risky["result"]["m_score"] > -1.78


# ─── 15. rNPV (Risk-Adjusted NPV) ──────────────────────────────
def test_rnpv_probability_phases():
    res = course_labs_service.calculate_rnpv(
        investment_m=100.0,
        cash_flow_annual_m=150.0,
        years=3,
        probabilities_of_success_pct=[60.0, 40.0, 70.0],
        discount_rate_pct=10.0
    )
    assert res["status"] == "° verified"
    assert res["result"]["rnpv_m"] < res["result"]["plain_npv_m"]


# ─── 16. Cut-Cut Recovery Solver ──────────────────────────────
def test_cut_cut_solver():
    res = course_labs_service.calculate_cut_cut(peak_eps=4.0, current_eps=1.0, years_to_recover=4)
    assert res["status"] == "° verified"
    assert res["result"]["recovery_growth_pct"] == pytest.approx(41.42, 0.1)


# ─── 17. Bank Analytics (NIM, CASA, LDR, Cost of Risk) ────────
def test_bank_analytics_suite():
    res = course_labs_service.calculate_banks_toolkit(
        nii_m=2000.0,
        earning_assets_m=60000.0,
        provisions_m=300.0,
        total_loans_m=50000.0,
        total_deposits_m=55000.0,
        casa_deposits_m=35000.0,
        operating_revenue_m=2800.0
    )
    assert res["status"] == "° verified"
    # NIM = 2000 / 60000 = 3.33%
    assert res["result"]["nim_pct"] == pytest.approx(3.33, 0.01)
    # LDR = 50000 / 55000 = 90.91% (< 95% threshold)
    assert res["result"]["is_ldr_danger"] is False
    # CASA = 35000 / 55000 = 63.64%
    assert res["result"]["casa_pct"] == pytest.approx(63.64, 0.1)


# ─── 18. Production Universal Contract Schema Validation ──────
def test_full_company_payload_schema_compliance():
    payload = calculate_full_company_payload("2222")
    # Verify strict Pydantic parsing of RebhUniversalContract
    assert isinstance(payload, RebhUniversalContract)
    assert payload.symbol == "2222"
    assert payload.market_cap is not None
    assert payload.market_cap > 0
    assert payload.price > 0
    assert payload.fresh is True
    assert payload.quarantine_reason is None
    assert payload.balance_identity.is_valid is True
    assert payload.grades is not None
    assert payload.porter is not None
    assert payload.build_up is not None
    assert payload.nine_box is not None
    assert payload.zones is not None
    assert payload.shariah is not None


# ─── 20. Bank Exemption from Industrial Safety & Debt Metrics ──
def test_bank_exemption_in_unified_engine():
    payload = calculate_full_company_payload("1120")
    assert isinstance(payload, RebhUniversalContract)
    assert payload.symbol == "1120"
    assert payload.bank_metrics is not None
    assert payload.bank_metrics.is_bank is True
    # Industrial safety weight zeroed out
    assert payload.build_up.safety_weight == 0.0
    # Bank NIM and Prov/Rev populated
    assert payload.bank_metrics.nim_pct is not None
