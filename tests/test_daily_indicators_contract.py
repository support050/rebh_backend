"""
Pytest Suite — Daily Financial Indicators return contract (offline)
===================================================================
Guards the 2026-10-25 hardening of
backend/app/scrapers/daily_financial_indicators_scraper.py.

Before that fix ``run_scraper_and_save_to_db()`` ended in a bare ``return True``
and ``save_to_db()`` swallowed its own DB error, so a scrape that served zero
rows — or whose write rolled back — was still recorded as a green
``scraper:tasi_components`` job with 0 rows (the 2026-10-13
substantial-shareholders bug class).

Nothing here touches the database or the network: the Selenium scrape and the
DB write are both monkeypatched, and only the pure return contract is asserted.

Run: pytest backend/tests/test_daily_indicators_contract.py -v
"""
import sys
import os

import pytest

# Make sure backend app is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import app.scrapers.daily_financial_indicators_scraper as dfi
from app.services.job_tracking import _dict_failure, _infer_rows


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fake_rows(n: int, start_symbol: int = 1000) -> list:
    """n company rows in the shape the page parser produces (see the captured
    daily_financial_indicators_companies_2026-08-25.csv: 270 rows, 14 columns)."""
    return [
        {"Symbol": str(start_symbol + i), "Company": f"CO{i}",
         "Close Price": 10.0 + i, "Market Cap": 100.0, "Market Cap %": 0.01,
         "EPS": 1.5, "P/E Ratio": 12.0}
        for i in range(n)
    ]


def _patch(monkeypatch, companies, save_db):
    """Replace the scrape and the write; the module-level names are what
    run_scraper_and_save_to_db() looks up at call time."""
    monkeypatch.setattr(
        dfi, "scrape_daily_financial_indicators",
        lambda headless=True: (companies, [], None, "2026-10-25"),
    )
    monkeypatch.setattr(dfi, "save_to_db", save_db)


# ─── The floor is measured, not guessed ──────────────────────────────────────

class TestExpectedFloor:

    def test_floor_is_positive_and_below_the_measured_snapshot(self):
        # The 2026-08-25 capture held 270 company rows, so the floor that decides
        # "partial page" must sit under that and well above zero.
        assert 0 < dfi.MIN_COMPANIES_EXPECTED <= 270


# ─── Empty scrape = failed job, not a green one ──────────────────────────────

class TestEmptyScrapeIsFailure:

    def test_zero_rows_reports_ok_false(self, monkeypatch):
        _patch(monkeypatch, [], lambda comps: (0, 0))
        result = dfi.run_scraper_and_save_to_db()

        assert result["ok"] is False
        assert result["rows"] == 0
        assert result["companies"] == 0

    def test_failure_carries_a_stage_and_reason(self, monkeypatch):
        _patch(monkeypatch, [], lambda comps: (0, 0))
        result = dfi.run_scraper_and_save_to_db()

        failures = result["stage_failures"]
        assert failures and failures[0]["stage"] == "daily_financial_indicators"
        assert "0 company rows" in failures[0]["error"]

    def test_save_to_db_is_never_called_on_an_empty_page(self, monkeypatch):
        calls = []
        _patch(monkeypatch, [], lambda comps: calls.append(comps) or (0, 0))
        dfi.run_scraper_and_save_to_db()
        assert calls == []

    def test_truthiness_is_not_health(self, monkeypatch):
        """Why every caller had to stop using bool().

        A non-empty dict is truthy in Python, so the old ``ok = bool(...)`` read
        an honest failure as success.
        """
        _patch(monkeypatch, [], lambda comps: (0, 0))
        result = dfi.run_scraper_and_save_to_db()

        assert bool(result) is True          # truthy …
        assert result.get("ok") is False     # … but the job failed


# ─── job_tracking must see the failure through the dict shape ────────────────

class TestJobTrackingIntegration:

    def test_dict_failure_returns_the_stage_text(self, monkeypatch):
        _patch(monkeypatch, [], lambda comps: (0, 0))
        result = dfi.run_scraper_and_save_to_db()

        msg = _dict_failure(result)
        assert msg and "daily_financial_indicators" in msg

    def test_dict_failure_returns_none_on_success(self, monkeypatch):
        _patch(monkeypatch, _fake_rows(dfi.MIN_COMPANIES_EXPECTED + 1),
               lambda comps: (1, 2))
        result = dfi.run_scraper_and_save_to_db()

        assert _dict_failure(result) is None

    def test_infer_rows_reads_the_rows_key(self, monkeypatch):
        _patch(monkeypatch, _fake_rows(dfi.MIN_COMPANIES_EXPECTED + 1),
               lambda comps: (3, 4))
        result = dfi.run_scraper_and_save_to_db()

        assert _infer_rows(result) == 7


# ─── A rolled-back write can no longer be green ──────────────────────────────

class TestWriteErrorIsFailure:

    def test_save_to_db_exception_becomes_ok_false(self, monkeypatch):
        def boom(comps):
            raise RuntimeError("UniqueViolation on tasi_components")

        _patch(monkeypatch, _fake_rows(dfi.MIN_COMPANIES_EXPECTED + 1), boom)
        result = dfi.run_scraper_and_save_to_db()

        assert result["ok"] is False
        assert result["rows"] == 0
        assert "UniqueViolation" in result["stage_failures"][0]["error"]

    def test_save_to_db_propagates_db_error(self, monkeypatch):
        # The swallowing `except … logger.error` is gone: without the raise the
        # caller could not tell a failed write from an empty market.
        class _FakeQuery:
            def all(self):
                return []

        class _FakeDB:
            def query(self, model):
                return _FakeQuery()

            def add(self, obj):
                pass

            def commit(self):
                raise RuntimeError("connection refused")

            def rollback(self):
                pass

            def close(self):
                pass

        import app.core.database as database
        monkeypatch.setattr(database, "SessionLocal", lambda: _FakeDB())

        with pytest.raises(RuntimeError):
            dfi.save_to_db(_fake_rows(1))


# ─── Rows arrived but wrote nothing ──────────────────────────────────────────

class TestNoWritableRowsIsFailure:

    def test_rows_without_symbol_report_failure(self, monkeypatch):
        rows = [{"Company": "NO SYMBOL", "Close Price": 1.0}]
        _patch(monkeypatch, rows, lambda comps: (0, 0))
        result = dfi.run_scraper_and_save_to_db()

        assert result["ok"] is False
        assert result["companies"] == 1
        assert "Symbol" in result["stage_failures"][0]["error"]


# ─── Partial page: saved, but never silently fine ────────────────────────────

class TestPartialPage:

    def test_below_floor_is_flagged_partial(self, monkeypatch):
        _patch(monkeypatch, _fake_rows(20), lambda comps: (0, 20))
        result = dfi.run_scraper_and_save_to_db()

        assert result["ok"] is True          # data is still today's real values
        assert result["partial"] is True
        assert result["companies"] == 20

    def test_at_or_above_floor_is_not_partial(self, monkeypatch):
        n = dfi.MIN_COMPANIES_EXPECTED
        _patch(monkeypatch, _fake_rows(n), lambda comps: (0, n))
        result = dfi.run_scraper_and_save_to_db()

        assert result["ok"] is True
        assert result["partial"] is False


# ─── The success contract (Gate #2: {"ok": ..., "rows": ...}) ────────────────

class TestSuccessContract:

    def test_success_shape(self, monkeypatch):
        rows = _fake_rows(dfi.MIN_COMPANIES_EXPECTED + 1)
        _patch(monkeypatch, rows, lambda comps: (5, 260))
        result = dfi.run_scraper_and_save_to_db()

        assert result["ok"] is True
        assert result["rows"] == 265
        assert result["added"] == 5
        assert result["updated"] == 260
        assert result["companies"] == len(rows)
        assert result["report_date"] == "2026-10-25"
        assert result["partial"] is False
        assert "stage_failures" not in result
