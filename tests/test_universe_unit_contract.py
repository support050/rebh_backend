"""
Universe unit-contract regression tests (2026-10-18).

Guards the four defects fixed in app/services/market_universe_service.py so they
can never come back:

  U1  magnitude guessing — a market-cap anchor (0.05 / 0.00005 ratios) plus an
      abs()-threshold branch in _norm_period_val re-scaled values that were
      ALREADY normalized. The only conversion allowed now is a fixed / SAR_TO_M,
      gated on meta.rounding == 'normalized_sar'
      (backend/docs/XBRL_OUTPUT_POLICY.md).
  U2  NCAV mixed a millions-scaled current-asset with raw-SAR total-assets /
      equity terms — a 1e6 error presented as an economic figure.
  U3  ROE / ROA divided SAR-millions earnings by raw-SAR equity, so most of the
      market read exactly 0.0% and the Profitability ladder was dead.
  U4  dividends (and CFO/FCF) annualised a 3/6/9-month cumulative YTD stub by
      12/months, inventing distributions that were never paid.

Behavioural tests cover the pure dividend picker (no DB, no network); the
arithmetic-shape tests assert the source contract; the artifact test checks the
metadata contract every consumer relies on and skips when output/*.json are
absent.

Run from backend/:  pytest tests/test_universe_unit_contract.py
"""
from datetime import date
from pathlib import Path
import json
import re

import pytest

from app.services import market_universe_service as mus
from app.services.market_universe_service import (
    DIVIDEND_FRESH_MONTHS,
    _detect_period_months,
    _months_since_period_end,
    _pick_dividends_paid,
)

BACKEND = Path(__file__).resolve().parent.parent
SERVICE_SRC = (BACKEND / "app" / "services" / "market_universe_service.py").read_text(
    encoding="utf-8")
OUTPUT = BACKEND / "output"

TODAY = date.today()


# --- period helpers built relative to today so the file never rots ----------

def _annual_label(year: int) -> str:
    return f"{year}-01_{year}-12"


def _stub_label(months_back: int = 0) -> str:
    """3-month window ending this month (age 0 -> always fresh)."""
    end_m = TODAY.month - months_back
    end_y = TODAY.year
    while end_m <= 0:
        end_m += 12
        end_y -= 1
    start_m, start_y = end_m, end_y
    for _ in range(2):
        start_m -= 1
        if start_m == 0:
            start_m, start_y = 12, start_y - 1
    return f"{start_y}-{start_m:02d}_{end_y}-{end_m:02d}"


FRESH_ANNUAL = _annual_label(TODAY.year - 1)      # ends Dec of last year: <= 12 months old
FRESH_STUB = _stub_label()                        # ends this month
STALE_ANNUAL = _annual_label(TODAY.year - 3)      # >= 24 months old

DIV = "Dividends Paid"


# --- U1: no magnitude guessing, metadata-driven scale -----------------------

def test_legacy_magnitude_guessers_are_gone():
    """The removed guessers must not be resurrected in this consumer."""
    assert "0.00005" not in SERVICE_SRC, "the market-cap anchor ratio is back"
    for banned in ("abs(v) > 1", "abs(value) >", "guess_unit", "infer_scale",
                   "mc_anchor", "MAGNITUDE_THRESHOLD"):
        assert banned not in SERVICE_SRC, f"legacy magnitude helper {banned!r} is back"
    # A magnitude test may never choose a unit: the surviving abs() use is the
    # dividend sign/zero check inside _pick_dividends_paid.
    assert not re.search(r"if\s+abs\([^)]*\)\s*[<>]", SERVICE_SRC), \
        "a magnitude-conditional branch is back"


def test_period_normaliser_uses_only_the_fixed_divisor():
    """_norm_period_val must be a plain SAR -> SAR-millions division."""
    body = re.search(r"def _norm_period_val\(.*?\n(.*?)\n\n", SERVICE_SRC, re.S)
    assert body, "_norm_period_val disappeared"
    lines = [ln.strip() for ln in body.group(1).splitlines() if ln.strip()]
    returns = [ln for ln in lines if ln.startswith("return")]
    assert returns == ["return None if v is None else v / SAR_TO_M"], returns


def test_scale_conversion_is_one_fixed_divisor():
    assert "SAR_TO_M = 1_000_000.0" in SERVICE_SRC


def test_unit_basis_is_declared_by_metadata_not_magnitude():
    assert 'rounding == "normalized_sar"' in SERVICE_SRC
    assert '"declared_per_filing"' in SERVICE_SRC
    # a contract violation is disclosed, never silently re-anchored
    assert '"\u2691scale-unverified"' in SERVICE_SRC
    assert "XBRL_OUTPUT_POLICY.md" in SERVICE_SRC


# --- U2 / U3: single-unit arithmetic --------------------------------------

def test_ncav_uses_one_unit_for_every_term():
    assert re.search(
        r"ncav = round\(\(ca_raw - ta_sar \+ te_sar\) / SAR_TO_M", SERVICE_SRC), \
        "NCAV must be built from raw-SAR terms and divided once"


def test_roe_and_roa_convert_before_the_ratio():
    assert re.search(r"roe = round\(\(ttm_net \* SAR_TO_M\) / te_sar", SERVICE_SRC)
    assert re.search(r"roa = round\(\(ttm_net \* SAR_TO_M\) / ta_sar", SERVICE_SRC)


# --- U4: dividends are taken as disclosed --------------------------------

def test_fresh_annual_beats_a_shorter_window():
    items = {DIV: {FRESH_ANNUAL: -500_000_000.0, FRESH_STUB: -120_000_000.0}}
    periods = [FRESH_ANNUAL, FRESH_STUB]
    assert _pick_dividends_paid(items, periods) == (-500_000_000.0, FRESH_ANNUAL, "annual")


def test_ytd_window_is_used_verbatim_never_annualised():
    """No annual statement -> the YTD figure is returned as disclosed (a lower
    bound). Multiplying it by 12/months was defect U4."""
    items = {DIV: {FRESH_STUB: -1_195_777_000.0}}
    val, period, basis = _pick_dividends_paid(items, [FRESH_STUB])
    assert (basis, period) == ("ytd-partial", FRESH_STUB)
    assert val == -1_195_777_000.0, "the disclosed payment must not be scaled up"


def test_stale_disclosure_is_reported_and_labelled():
    """An old distribution stays auditable but is flagged 'stale' so the caller
    can withhold the yield instead of serving it as a current rate."""
    items = {DIV: {STALE_ANNUAL: -900_000_000.0}}
    val, period, basis = _pick_dividends_paid(items, [STALE_ANNUAL])
    assert basis == "stale"
    assert (val, period) == (-900_000_000.0, STALE_ANNUAL)


def test_fresh_ytd_wins_over_a_stale_annual():
    items = {DIV: {STALE_ANNUAL: -900_000_000.0, FRESH_STUB: -200_000_000.0}}
    val, period, basis = _pick_dividends_paid(items, [STALE_ANNUAL, FRESH_STUB])
    assert (val, period, basis) == (-200_000_000.0, FRESH_STUB, "ytd-partial")


def test_zero_and_minority_only_labels_are_not_dividends():
    """A disclosed 0.0 is 'no distribution', and a minority-interest line is not
    a shareholder payout — neither may become a fake yield."""
    items = {DIV: {FRESH_ANNUAL: 0.0}}
    assert _pick_dividends_paid(items, [FRESH_ANNUAL]) == (None, None, "none")

    minority = {"Dividends paid to non-controlling interest": {FRESH_ANNUAL: -7_000_000.0}}
    assert _pick_dividends_paid(minority, [FRESH_ANNUAL]) == (None, None, "none")


def test_missing_cashflow_section_returns_none_basis():
    assert _pick_dividends_paid({}, []) == (None, None, "none")


def test_dividend_provenance_is_published_not_implied():
    for key in ('"div_paid"', '"div_yield"', '"div_basis"', '"div_period"', '"scale_basis"'):
        assert key in SERVICE_SRC, f"{key} must stay in the served payload"
    for flag in ("\u2691dividend-ytd-window", "\u2691dividend-stale",
                 "\u2691cashflow-stub-annualised"):
        assert flag in SERVICE_SRC, f"{flag} disclosure flag is missing"


# --- period helpers --------------------------------------------------------

def test_detect_period_months():
    assert _detect_period_months("2025-01_2025-12") == 12
    assert _detect_period_months("2026-01_2026-03") == 3
    assert _detect_period_months("2026-01_2026-06") == 6
    assert _detect_period_months("") is None


def test_months_since_period_end():
    assert _months_since_period_end(None) is None
    assert _months_since_period_end("garbage") is None
    assert _months_since_period_end("2025-01_2025-12") == \
        (TODAY.year - 2025) * 12 + (TODAY.month - 12)
    assert _months_since_period_end(FRESH_STUB) == 0


def test_freshness_window_is_eighteen_months():
    """One filing cycle plus slack: beyond it a payout is history, not a rate."""
    assert DIVIDEND_FRESH_MONTHS == 18


# --- the artifact contract every consumer depends on ----------------------

def _artifacts():
    if not OUTPUT.exists():
        return []
    return sorted(OUTPUT.glob("*_financials.json"))


@pytest.mark.skipif(not _artifacts(), reason="output/*.json artifacts not present")
def test_published_artifacts_declare_normalized_sar():
    """meta.rounding drives the consumer's single divisor; a file that stops
    declaring 'normalized_sar' must fail here, not silently re-anchor units."""
    offenders = []
    for path in _artifacts():
        with path.open(encoding="utf-8") as fh:
            meta = (json.load(fh) or {}).get("meta") or {}
        if meta.get("rounding") != "normalized_sar":
            offenders.append((path.name, meta.get("rounding")))
    assert not offenders, f"artifacts violating the unit contract: {offenders[:10]}"
