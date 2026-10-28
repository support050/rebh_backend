# -*- coding: utf-8 -*-
"""Phase 1 (Guru Scans) — eps_growth_service tests.

TML "M" = quarterly basic-EPS growth (YoY) mined from the compiled XBRL
artifacts. Pure-unit checks for the quarter-derivation ladder, the total-vs-
continuing conflict resolution and the out_of_range guardrail, plus one
integration probe over a real output/*.json that skips gracefully when the
artifacts are not on disk. Run from backend/:  pytest tests/test_eps_growth_service.py
ASCII-only source (Windows tooling); Arabic-free by design.
"""
import json
from pathlib import Path

import pytest

from app.services.eps_growth_service import (
    build_quarter_series,
    growth_from_quarters,
    _resolve_eps_values,
    _find_eps_item,
    load_eps_growth_map,
    DEFAULT_OUTPUT_DIR,
)


# ── quarter derivation ladder ─────────────────────────────────────────────

def test_standalone_quarters_are_taken_as_filed():
    values = {"2025-01_2025-03": 0.60, "2025-04_2025-06": 0.70,
              "2025-07_2025-09": 0.80}
    q, fy = build_quarter_series(values)
    assert q[(2025, 3)] == 0.60 and q[(2025, 6)] == 0.70 and q[(2025, 9)] == 0.80
    assert fy == {}


def test_q2_q3_q4_derived_from_adjacent_ytd_windows():
    # only YTD windows disclosed -> Q2=H1-Q1, Q3=9M-H1, Q4=FY-9M
    values = {"2025-01_2025-03": 0.50, "2025-01_2025-06": 1.10,
              "2025-01_2025-09": 1.70, "2025-01_2025-12": 2.20}
    q, fy = build_quarter_series(values)
    assert round(q[(2025, 6)], 6) == 0.60      # H1 - Q1
    assert round(q[(2025, 9)], 6) == 0.60      # 9M - H1
    assert round(q[(2025, 12)], 6) == 0.50     # FY - 9M
    assert fy[(2025,)] == 2.20                 # FY kept for the annual fallback


def test_fy_without_9m_never_invents_q4():
    # zero-substitution: no 9M -> Q4 is simply absent, not guessed
    values = {"2025-01_2025-12": 3.00}
    q, fy = build_quarter_series(values)
    assert (2025, 12) not in q
    assert fy[(2025,)] == 3.00


# ── YoY growth math ───────────────────────────────────────────────────────

def test_latest_and_prev_quarter_yoy():
    q = {(2024, 3): 0.50, (2024, 6): 0.60,
         (2025, 3): 0.70, (2025, 6): 0.66}
    rec = growth_from_quarters(q)
    assert rec["latest_q"] == "2025-06"
    assert rec["latest_yoy_pct"] == pytest.approx(10.0)     # 0.66 vs 0.60
    assert rec["prev_q_yoy_pct"] == pytest.approx(40.0)     # 0.70 vs 0.50
    assert rec["pairs_available"] == 2


def test_missing_prev_quarter_is_none_not_zero():
    q = {(2024, 3): 0.50, (2025, 3): 0.75}     # only Q1s, no Q4 2024 -> prev slot absent
    rec = growth_from_quarters(q)
    assert rec["latest_yoy_pct"] == pytest.approx(50.0)
    assert rec["prev_q_yoy_pct"] is None


# ── conflict resolution (total vs continuing-ops) ─────────────────────────

def test_conflict_resolves_to_total_eps():
    item = {
        "label": "Basic Earnings per Share (EPS)",
        "flags": ["conflict"],
        "values": {"2021-01_2021-03": 0.48},          # parser had picked continuing-ops
        "sources": {"2021-01_2021-03": [
            {"label": "basic earnings (loss) per share from continuing operations", "value": 0.48},
            {"label": "total basic earnings (loss) per share", "value": 0.49},
        ]},
    }
    resolved = _resolve_eps_values(item)
    assert resolved["2021-01_2021-03"] == pytest.approx(0.49)   # headline total wins


def test_find_eps_item_prefers_basic_line():
    items = [{"label": "Diluted Earnings per Share (EPS)"},
             {"label": "Basic Earnings per Share (EPS)"}]
    assert _find_eps_item(items)["label"].startswith("Basic")


# ── integration over a real artifact ──────────────────────────────────────

@pytest.mark.skipif(not DEFAULT_OUTPUT_DIR.exists()
                    or not any(DEFAULT_OUTPUT_DIR.glob("*_financials.json")),
                    reason="compiled XBRL artifacts not present on disk")
def test_1010_growth_matches_manual_calc():
    fp = DEFAULT_OUTPUT_DIR / "1010_financials.json"
    if not fp.exists():
        pytest.skip("1010 artifact not present")
    data = json.loads(fp.read_text(encoding="utf-8"))
    sec = data["sections"]["standardized_income_statement"]
    item = _find_eps_item(sec["items"])
    q, _ = build_quarter_series(_resolve_eps_values(item))
    rec = growth_from_quarters(q)
    # every disclosed quarter must be positive-valued and YoY computable
    assert rec["latest_yoy_pct"] is not None
    assert rec["pairs_available"] >= 4


@pytest.mark.skipif(not DEFAULT_OUTPUT_DIR.exists()
                    or not any(DEFAULT_OUTPUT_DIR.glob("*_financials.json")),
                    reason="compiled XBRL artifacts not present on disk")
def test_load_map_is_honest_and_broad():
    res = load_eps_growth_map()
    # 257 files scanned; the great majority yield a usable growth figure
    assert res["files_scanned"] == 257
    assert len(res["growth"]) >= 200
    # no emitted record has a null latest_yoy (that's the TML gate)
    assert all(v["latest_yoy_pct"] is not None for v in res["growth"].values())
    # out_of_range symbols are excluded, never silently included
    for sym, reason in res["excluded"].items():
        assert reason in {"out_of_range", "no_yoy_pair", "no_eps_line"} or reason.startswith("unreadable")
