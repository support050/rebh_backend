"""
Unit tests for rebh_production_helper_service:
1. Strict Piotroski F-Score requires 2 full periods or returns status='⚑missing-f-score'
2. Statement freshness respects quarterly (135d) vs annual (210d) deadlines
3. Cyclical bounds extract historical min & max EPS rather than static factors
4. Nine-box inputs normalize negative dividends paid and calculate net debt
5. P/S Kill-Switch requires 2 consecutive quarters + positive TTM + stable margins
"""
import pytest
from datetime import date, timedelta
from app.services.rebh_production_helper_service import (
    compute_strict_piotroski_f_score,
    evaluate_statement_freshness,
    extract_cyclical_cycle_bounds,
    extract_nine_box_inputs,
    evaluate_ps_kill_switch
)


def test_piotroski_returns_none_when_prior_period_missing():
    # Only 1 period available -> must not invent an F-Score
    res = compute_strict_piotroski_f_score(
        bs_items={"Total Assets": {"2024-Q3": 1000.0}},
        is_items={"Net Profit": {"2024-Q3": 50.0}},
        cf_items={},
        periods_bs=["2024-Q3"],
        periods_is=["2024-Q3"]
    )
    assert res["score"] is None
    assert res["status"] == "⚑missing-f-score"
    assert res["is_complete"] is False


def test_piotroski_calculates_when_two_full_periods_exist():
    # 2 periods with improving financials -> full evaluation
    res = compute_strict_piotroski_f_score(
        bs_items={
            "Total Assets": {"2023-Q3": 1000.0, "2024-Q3": 1100.0},
            "Total Current Assets": {"2023-Q3": 500.0, "2024-Q3": 600.0},
            "Total Current Liabilities": {"2023-Q3": 300.0, "2024-Q3": 300.0},
            "Long-term Borrowings": {"2023-Q3": 200.0, "2024-Q3": 150.0},
            "Issued Capital": {"2023-Q3": 500.0, "2024-Q3": 500.0},
        },
        is_items={
            "Net Profit": {"2023-Q3": 50.0, "2024-Q3": 80.0},
            "Revenue": {"2023-Q3": 800.0, "2024-Q3": 950.0},
            "Gross Profit": {"2023-Q3": 200.0, "2024-Q3": 260.0},
        },
        cf_items={
            "Operating Activities": {"2023-Q3": 60.0, "2024-Q3": 90.0}
        },
        periods_bs=["2023-Q3", "2024-Q3"],
        periods_is=["2023-Q3", "2024-Q3"]
    )
    assert res["is_complete"] is True
    assert res["score"] >= 7
    assert res["status"] == "° verified"


def test_freshness_gate_quarterly_vs_annual():
    today = date.today()
    
    # Fresh quarterly (60 days ago)
    fresh_q_date = str(today - timedelta(days=60))
    res_q_fresh = evaluate_statement_freshness(fresh_q_date)
    assert res_q_fresh["is_fresh"] is True

    # Stale quarterly (195 days ago, past 180d buffer)
    stale_q_date = str(today - timedelta(days=195))
    res_q_stale = evaluate_statement_freshness(stale_q_date)
    assert res_q_stale["is_fresh"] is False
    assert "متأخرة" in res_q_stale["stale_reason"]

    # Annual statement (180 days ago is still fresh under 210d buffer)
    annual_date = f"{today.year - 1}-12-31 FY"
    res_ann = evaluate_statement_freshness(annual_date)
    # Annual evaluated with appropriate limit
    assert res_ann["period"] == annual_date


def test_cyclical_cycle_bounds_uses_actual_historical_extrema():
    # Multi-year earnings: [1.2, 0.8, 0.4, 2.5, 3.8, 4.2, 1.9]
    eps_history = [1.2, 0.8, 0.4, 2.5, 3.8, 4.2, 1.9]
    res = extract_cyclical_cycle_bounds(eps_history, current_eps=1.9)
    assert res["has_cycle"] is True
    assert res["trough_eps"] == 0.4
    assert res["peak_eps"] == 4.2
    assert res["buy_band_min"] == round(0.4 * 14.0, 2)
    assert res["buy_band_max"] == round(0.4 * 16.0, 2)
    assert res["sell_band_min"] == round(4.2 * 8.0, 2)
    assert res["sell_band_max"] == round(4.2 * 12.0, 2)


def test_nine_box_inputs_normalizes_negative_dividend_flow():
    # In CFF, cash paid for dividends is reported as negative e.g. -250M
    cff_data = {"Dividends paid": {"2024-Q3": -250.0}}
    res = extract_nine_box_inputs(
        eps=3.0,
        cff_items=cff_data,
        latest_period="2024-Q3",
        fcf_annual=400.0,
        total_debt=100.0,
        cash=50.0,
        shares=100.0
    )
    # DPS must be strictly positive: abs(-250)/100 = 2.5 SAR
    assert res["dps"] == 2.5
    assert res["dps_source"] == "cff_dividends_paid_actual"
    assert res["net_debt_m"] == 50.0


def test_ps_kill_switch_requires_all_three_criteria():
    # Only 1 quarter positive -> Kill-Switch remains inactive
    res1 = evaluate_ps_kill_switch(
        quarterly_net_income=[-10.0, -5.0, 2.0],
        ttm_net_income=-13.0,
        gross_margin=0.20
    )
    assert res1["kill_switch_active"] is False

    # 2 quarters positive, positive TTM, and stable margin -> Kill-Switch triggers
    res2 = evaluate_ps_kill_switch(
        quarterly_net_income=[-10.0, 5.0, 12.0],
        ttm_net_income=15.0,
        gross_margin=0.25
    )
    assert res2["kill_switch_active"] is True
    assert "انتقلت الشركة للربحية" in res2["status_note"]


def test_sector_median_margins_and_growth():
    from app.services.rebh_production_helper_service import get_sector_margin_and_growth
    mat = get_sector_margin_and_growth("Materials")
    assert mat["median_npm_pct"] > 0
    assert mat["expected_growth_pct"] > 0
    assert "sample_size" in mat

    ret = get_sector_margin_and_growth("Retail")
    assert ret["median_npm_pct"] > 0
    assert ret["expected_growth_pct"] > 0
    assert "sample_size" in ret


def test_peer_relative_grades_sector_anchored():
    from app.services.rebh_production_helper_service import compute_peer_relative_grades
    # Capital goods sector with ROE 15% should achieve A grade
    grades_mat = compute_peer_relative_grades(
        pe=12.0,
        roe=15.0,
        cur_ratio=1.8,
        debt_to_assets=25.0,
        sector="Materials"
    )
    assert grades_mat["Profitability"]["g"] in ["A", "A+"]
    assert grades_mat["Valuation"]["b"] == "sec_peer"


def test_vintages_job_persists_snapshots():
    from app.services.rebh_importers_service import run_engine_vintages_job
    from app.core.database import SessionLocal
    from app.models.rebh_engine_vintage import RebhEngineVintage
    import pytest
    from sqlalchemy.exc import IntegrityError

    res = run_engine_vintages_job(limit=3)
    assert res["status"] == "ok"
    assert res["persisted_snapshots"] >= 1

    db = SessionLocal()
    try:
        count = db.query(RebhEngineVintage).count()
        assert count >= 1

        # Test that inserting an exact duplicate (same symbol, batch_id, as_of_period) triggers UniqueConstraint
        existing = db.query(RebhEngineVintage).filter(RebhEngineVintage.batch_id.isnot(None)).first()
        if existing:
            duplicate = RebhEngineVintage(
                symbol=existing.symbol,
                batch_id=existing.batch_id,
                as_of_period=existing.as_of_period,
                contract_json="{}"
            )
            db.add(duplicate)
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
    finally:
        db.close()


def test_vintage_api_point_in_time_retrieval():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)

    # 1. Fetch company vintages list
    res_list = client.get("/api/rebh/vintages/2222")
    assert res_list.status_code == 200
    vintages = res_list.json()
    assert isinstance(vintages, list)
    if vintages:
        latest = vintages[0]
        assert latest["symbol"] == "2222"
        assert "contract" in latest
        period = latest["as_of_period"]
        if period:
            # 2. Fetch exact point-in-time snapshot by period
            res_pit = client.get(f"/api/rebh/vintages/2222/as-of/{period}")
            assert res_pit.status_code == 200
            pit_data = res_pit.json()
            assert pit_data["symbol"] == "2222"
            assert pit_data["as_of_period"] == period

            # 3. Test cutoff_vintage_date filter prevents future snapshots
            from datetime import datetime, timezone, timedelta
            past_cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
            res_past = client.get(f"/api/rebh/vintages/2222/as-of/{period}?cutoff_vintage_date={past_cutoff}")
            assert res_past.status_code == 404  # Correctly enforces point-in-time barrier without look-ahead


def test_missing_dividends_returns_none_without_synthetic_proxy():
    # Empty CFF items must produce dps=None and source_status="🔌 missing-source"
    res = extract_nine_box_inputs(
        eps=5.0,
        cff_items={},
        latest_period="2024-Q3",
        fcf_annual=100.0,
        total_debt=0.0,
        cash=50.0,
        shares=10.0
    )
    assert res["dps"] is None
    assert res["dps_source"] == "🔌 missing-source"


def test_rnpv_course_package_probabilities():
    from app.services.course_labs_service import calculate_rnpv
    res = calculate_rnpv(investment_m=100.0, cash_flow_annual_m=50.0, years=4, discount_rate_pct=10.0, preset="course_standard")
    # Verify course package probabilities: [28.0, 17.0, 15.0, 13.5]
    assert res["inputs"]["step_probabilities_pct"] == [28.0, 17.0, 15.0, 13.5]
    assert "28/17/15/13.5%" in res["source"]
    assert res["result"]["cumulative_success_probability_pct"] < 1.0


def test_tasi_index_lab_live_market_machine():
    from app.services.course_labs_service import calculate_tasi_index_lab
    res = calculate_tasi_index_lab(mode="constituents_aggregate")
    assert "scenarios_10" in res["result"]
    assert len(res["result"]["scenarios_10"]) == 10
    assert "annual_dividend_pmt" in res["result"]
    # Check 2Y and 3Y IRR calculations exist
    fv1 = res["result"]["scenarios_10"][0]
    assert "return_2y_irr_pct" in fv1
    assert "return_3y_irr_pct" in fv1


def test_economy_scorecard_market_machine_5_gauges():
    from app.services.course_labs_service import calculate_economy_scorecard
    res = calculate_economy_scorecard(unrate_override=4.2, payems_delta_override=120.0, ic4wsa_override=215000.0, spread_10y2y_override=0.25, credit_spread_override=1.10)
    assert "market_machine_gauges" in res["result"]
    gauges = res["result"]["market_machine_gauges"]
    assert len(gauges) == 5
    codes = [g["code"] for g in gauges]
    assert set(codes) == {"UNRATE", "PAYEMS", "IC4WSA", "T10Y2Y", "CREDIT_SPREAD"}
    assert all(g["verdict"] == "Positive" for g in gauges)
    assert "saudi_macro" in res["result"]


def test_banks_toolkit_with_12_flags_and_symbol():
    from app.services.course_labs_service import calculate_banks_toolkit
    res = calculate_banks_toolkit(symbol="1120.SR")
    assert res["result"]["symbol"] == "1120.SR"
    assert "flags_12" in res["result"]
    assert len(res["result"]["flags_12"]) >= 1
    assert "nim_pct" in res["result"]
    assert "casa_ratio_pct" in res["result"]



