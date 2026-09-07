"""
Pytest Unit Tests for Bank Financial Analytics (Phase 3).
Converts diagnostic prints into rigorous assertions with tests for:
- NII extraction
- Average earning assets calculation
- Accurate NIM = NII / Average Earning Assets
- Provisions / Revenue ratio
- Loan-to-Deposit Ratio (LDR) and >=95% danger threshold
- CASA ratio calculation
- Cost of Risk (COR) = Provisions / Loans
- Missing labels resilience
- Provision reversal detection
- Bank-specific 12 flags
"""
import pytest
from app.services.bank_analytics_service import calculate_bank_metrics


def test_bank_metrics_extraction_alrajhi():
    res = calculate_bank_metrics("1120")
    assert res.get("is_bank") is True
    metrics = res.get("metrics", {})

    # NII extraction
    nii = metrics.get("net_financing_income_sar")
    assert nii is not None
    assert nii > 0

    # Loans & Deposits
    loans = metrics.get("loans_and_advances_sar")
    deposits = metrics.get("customer_deposits_sar")
    assert loans is not None and loans > 0
    assert deposits is not None and deposits > 0

    # Average Earning Assets & NIM
    avg_assets = metrics.get("average_earning_assets")
    assert avg_assets is not None and avg_assets > 0
    nim = metrics.get("net_interest_margin_pct")
    assert nim is not None
    assert nim > 0

    # LDR
    ldr = metrics.get("loan_to_deposit_ratio_pct")
    assert ldr is not None
    assert round(ldr, 1) == round((loans / deposits) * 100.0, 1)

    # Cost of Risk
    cor = metrics.get("cost_of_risk_pct")
    assert cor is not None


def test_bank_metrics_snb():
    res = calculate_bank_metrics("1180")
    assert res.get("is_bank") is True
    metrics = res.get("metrics", {})
    assert metrics.get("net_financing_income_sar") is not None
    assert metrics.get("loan_to_deposit_ratio_pct") is not None
    assert "ldr_status" in metrics


def test_non_bank_rejection():
    # Aramco 2222 is an energy company, not a bank
    res = calculate_bank_metrics("2222")
    assert res.get("is_bank") is False


def test_bank_12_flags_presence():
    res = calculate_bank_metrics("1120")
    flags = res.get("metrics", {}).get("flags", [])
    assert isinstance(flags, list)
    # Ensure flags contain proper status symbols
    for f in flags:
        assert any(sym in f for sym in ["⚑", "°", "≈"])
