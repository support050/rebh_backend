import pytest
from app.services import course_labs_service


def test_tasi_index_lab():
    res = course_labs_service.calculate_tasi_index_lab(current_pe=13.6, bond_yield_pct=4.75)
    assert res["status"] == "° verified"
    assert res["result"]["current_pe"] == 13.6
    assert res["result"]["fair_pe_bond_rule"] == round(1.0 / (0.0475 * 1.5), 2)
    assert "pe_15" in res["result"]["scenarios"]


def test_beneish_m_score():
    # Normal values
    normal = course_labs_service.calculate_beneish_m_score()
    assert normal["result"]["is_manipulation_risk"] is False
    assert normal["status"] == "° verified"
    
    # Manipulator values
    flagged = course_labs_service.calculate_beneish_m_score(dsri=2.5, gmi=2.0, tata=0.25)
    assert flagged["result"]["is_manipulation_risk"] is True
    assert len(flagged["warnings"]) > 0


def test_rnpv_clinical_phases():
    res = course_labs_service.calculate_rnpv(
        investment_m=100.0,
        cash_flow_annual_m=120.0,
        years=3,
        probabilities_of_success_pct=[59.5, 35.5, 62.0],
        discount_rate_pct=10.0
    )
    assert res["status"] == "° verified"
    assert res["result"]["plain_npv_m"] > 0
    # With cumulative probabilities, rNPV should reflect significant risk haircut
    assert "phase_breakdown" in res["result"]
    assert len(res["result"]["phase_breakdown"]) == 3


def test_cut_cut_solver():
    res = course_labs_service.calculate_cut_cut(peak_eps=4.0, current_eps=1.0, years_to_recover=4)
    assert res["status"] == "° verified"
    assert res["result"]["recovery_growth_pct"] == pytest.approx(41.42, 0.1)


def test_fair_pb_and_dcf_growth():
    fair_pb = course_labs_service.calculate_fair_pb(roe_pct=16.0, required_return_pct=8.0)
    assert fair_pb["result"]["fair_pb"] == 2.0
    
    dcf = course_labs_service.calculate_dcf_growth(price=50.0, eps=3.0, r_pct=8.0)
    assert dcf["status"] == "° verified"
    assert dcf["result"]["implied_growth_pct"] is not None


def test_banks_toolkit():
    res = course_labs_service.calculate_banks_toolkit(
        nii_m=1500.0,
        earning_assets_m=50000.0,
        provisions_m=200.0,
        total_loans_m=45000.0,
        total_deposits_m=48000.0,
        casa_deposits_m=30000.0,
        operating_revenue_m=2200.0
    )
    assert res["result"]["nim_pct"] == pytest.approx(3.0, 0.01)
    assert res["result"]["casa_pct"] == pytest.approx(62.5, 0.1)
    assert res["result"]["is_ldr_danger"] is False


def test_peter_lynch_and_governance():
    lynch = course_labs_service.calculate_peter_lynch_category(revenue_growth_pct=25.0, pe_ratio=15.0)
    assert "Fast Growers" in lynch["result"]["category"]
    assert lynch["result"]["is_fair_peg"] is True
    
    gov = course_labs_service.calculate_governance_scorecard(
        audit_opinion_clean=False,
        board_independence_pct=25.0
    )
    assert gov["result"]["governance_score"] < 60
    assert len(gov["warnings"]) >= 2
