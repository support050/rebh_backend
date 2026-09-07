"""
Pytest Suite — Phase 4: Sukuk & Bonds Import Tests
====================================================
Tests parser functions and DB upsert logic using saved fixture data.
All tests use in-memory fixtures — do NOT require live network access.

Run: pytest backend/tests/test_sukuk_import.py -v
"""
import sys
import os
import pytest
from decimal import Decimal
from datetime import date

# Make sure backend app is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# Import parser helpers from the importer script
from scripts.Sukuk_and_Bonds import (  # noqa: F401  (import alias used below)
    _parse_decimal,
    _parse_date,
    _classify_bond_type,
    _normalize_item,
)

# ─── Fixtures ─────────────────────────────────────────────────────────────────

FIXTURE_GOVT_SUKUK = {
    "symbol": "5389",
    "issuerName": "Government of Saudi Arabia",
    "parentCompnaySymbol": "",
    "bondType": "G",
    "couponRate": "4.7500",
    "ytm": "4.6200",
    "issueDateStr": "2021-01-15",
    "maturityDateStr": "2031-01-15",
    "outstandingAmountModified": "5,000,000,000",
    "currency": "SAR",
    "sectorName": "Government",
}

FIXTURE_CORP_SUKUK = {
    "symbol": "2281",
    "issuerName": "Saudi Aramco Base Oil Company",
    "parentCompnaySymbol": "2222",
    "bondType": "C",
    "couponRate": "3.2500",
    # No ytm field — should NOT infer YTM from coupon
    "maturityDateStr": "2028-06-30",
    "outstandingAmountModified": "1,500,000,000",
    "currency": "SAR",
    "sectorName": "Energy",
}

FIXTURE_AMBIGUOUS = {
    "symbol": "9999",
    "issuerName": "Unknown Corp",
    "parentCompnaySymbol": None,
    "bondType": None,   # ambiguous — must default to 'C'
    "couponRate": "5.50%",
    "maturityDateStr": "2030-12-31",
    "outstandingAmountModified": None,
    "currency": "",
    "sectorName": None,
}

FIXTURE_INVALID = {
    # Missing symbol — should be rejected
    "symbol": "",
    "issuerName": "No Symbol Corp",
    "couponRate": "6.00",
}

# ─── _parse_decimal ────────────────────────────────────────────────────────────

class TestParseDecimal:
    def test_plain_number(self):
        assert _parse_decimal("4.75") == Decimal("4.75")

    def test_with_percent_sign(self):
        assert _parse_decimal("5.50%") == Decimal("5.50")

    def test_with_commas(self):
        assert _parse_decimal("1,500,000,000") == Decimal("1500000000")

    def test_none_returns_none(self):
        assert _parse_decimal(None) is None

    def test_dash_returns_none(self):
        assert _parse_decimal("-") is None

    def test_na_returns_none(self):
        assert _parse_decimal("N/A") is None

    def test_empty_returns_none(self):
        assert _parse_decimal("") is None

    def test_integer(self):
        assert _parse_decimal(5) == Decimal("5")

    def test_float(self):
        result = _parse_decimal(4.75)
        assert result is not None
        assert float(result) == pytest.approx(4.75, abs=0.001)

    def test_arabic_percent_sign(self):
        assert _parse_decimal("5.65٪") == Decimal("5.65")


# ─── _parse_date ───────────────────────────────────────────────────────────────

class TestParseDate:
    def test_iso_format(self):
        assert _parse_date("2031-01-15") == date(2031, 1, 15)

    def test_slash_dmy(self):
        assert _parse_date("15/01/2031") == date(2031, 1, 15)

    def test_slash_mdy(self):
        assert _parse_date("01/15/2031") == date(2031, 1, 15)

    def test_unix_ms(self):
        # 2028-06-30 ≈ 1845273600000 ms from epoch
        result = _parse_date("1845273600000")
        assert result is not None
        assert result.year == 2028

    def test_year_only(self):
        result = _parse_date("2024")
        assert result is not None
        assert result.year == 2024
        assert result.month == 12
        assert result.day == 31

    def test_none_returns_none(self):
        assert _parse_date(None) is None

    def test_dash_returns_none(self):
        assert _parse_date("-") is None

    def test_empty_returns_none(self):
        assert _parse_date("") is None


# ─── _classify_bond_type ──────────────────────────────────────────────────────

class TestClassifyBondType:
    def test_g_is_government(self):
        assert _classify_bond_type("G") == "G"

    def test_gov_string(self):
        assert _classify_bond_type("Government") == "G"

    def test_sovereign_sukuk(self):
        assert _classify_bond_type("Sovereign Sukuk") == "G"

    def test_c_is_corporate(self):
        assert _classify_bond_type("C") == "C"

    def test_corporate_string(self):
        assert _classify_bond_type("Corporate") == "C"

    def test_none_defaults_to_c(self):
        assert _classify_bond_type(None) == "C"

    def test_empty_defaults_to_c(self):
        assert _classify_bond_type("") == "C"

    def test_case_insensitive(self):
        assert _classify_bond_type("corp") == "C"
        assert _classify_bond_type("government") == "G"


# ─── _normalize_item ──────────────────────────────────────────────────────────

class TestNormalizeItem:
    SOURCE_URL = "https://test.example.com/sukuk"

    def test_govt_sukuk_full(self):
        norm = _normalize_item(FIXTURE_GOVT_SUKUK, source_url=self.SOURCE_URL)
        assert norm is not None
        assert norm["symbol"] == "5389"
        assert norm["bond_type"] == "G"
        assert norm["coupon_rate"] == Decimal("4.7500")
        # YTM must come from ytm field, not couponRate
        assert norm["yield_to_maturity"] == Decimal("4.6200")
        assert norm["issue_date"] == date(2021, 1, 15)
        assert norm["maturity_date"] == date(2031, 1, 15)
        assert norm["outstanding_amount"] == Decimal("5000000000")
        assert norm["currency"] == "SAR"
        assert norm["source_url"] == self.SOURCE_URL
        assert norm["is_active"] is True

    def test_corporate_sukuk_no_ytm(self):
        """Corporate sukuk without ytm field must NOT derive YTM from coupon."""
        norm = _normalize_item(FIXTURE_CORP_SUKUK, source_url=self.SOURCE_URL)
        assert norm is not None
        assert norm["bond_type"] == "C"
        assert norm["parent_company_symbol"] == "2222"
        assert norm["coupon_rate"] == Decimal("3.2500")
        # No ytm in fixture — must be None, NOT copied from coupon
        assert norm["yield_to_maturity"] is None, (
            "YTM must be None when source does not provide it — "
            "never infer YTM from coupon_rate"
        )

    def test_ambiguous_bond_type_defaults_to_c(self):
        norm = _normalize_item(FIXTURE_AMBIGUOUS, source_url=self.SOURCE_URL)
        assert norm is not None
        assert norm["bond_type"] == "C"

    def test_coupon_with_percent_stripped(self):
        norm = _normalize_item(FIXTURE_AMBIGUOUS, source_url=self.SOURCE_URL)
        assert norm is not None
        assert norm["coupon_rate"] == Decimal("5.50")

    def test_null_outstanding_amount_is_none(self):
        norm = _normalize_item(FIXTURE_AMBIGUOUS, source_url=self.SOURCE_URL)
        assert norm is not None
        assert norm["outstanding_amount"] is None

    def test_empty_currency_defaults_to_sar(self):
        norm = _normalize_item(FIXTURE_AMBIGUOUS, source_url=self.SOURCE_URL)
        assert norm is not None
        assert norm["currency"] == "SAR"

    def test_missing_symbol_returns_none(self):
        norm = _normalize_item(FIXTURE_INVALID, source_url=self.SOURCE_URL)
        assert norm is None, "Items with empty symbol must be rejected"

    def test_as_of_is_date_string(self):
        norm = _normalize_item(FIXTURE_GOVT_SUKUK, source_url=self.SOURCE_URL)
        assert norm is not None
        # as_of must be a valid date string like "2026-09-04"
        import re
        assert re.match(r"\d{4}-\d{2}-\d{2}", norm["as_of"]), (
            f"as_of must be YYYY-MM-DD format, got: {norm['as_of']}"
        )


# ─── Build-Up R Integration ───────────────────────────────────────────────────

class TestBuildUpSukukYield:
    """
    Integration tests for get_buildup_sukuk_yield.
    These tests use the fallback path only (no DB required).
    """

    def test_fallback_returns_valid_structure(self):
        """
        Without a DB connection, the function must return the SAMA fallback
        with is_fallback=True and a numeric yield.
        """
        # Import here to avoid import errors at collection time
        try:
            from scripts.Sukuk_and_Bonds import get_buildup_sukuk_yield
        except Exception:
            pytest.skip("DB or import not available in test environment")

        # Use a symbol that will never exist in a test DB
        result = get_buildup_sukuk_yield("XXXXXXX")
        assert isinstance(result, dict)
        assert "yield_pct" in result
        assert isinstance(result["yield_pct"], float)
        assert result["yield_pct"] > 0

    def test_fallback_is_marked_as_fallback(self):
        try:
            from scripts.Sukuk_and_Bonds import get_buildup_sukuk_yield
        except Exception:
            pytest.skip("DB or import not available in test environment")

        result = get_buildup_sukuk_yield("XXXXXXX")
        # Fallback must be explicitly marked
        assert result.get("is_fallback") is True, (
            "Fallback sukuk yield must set is_fallback=True — never show as live"
        )
