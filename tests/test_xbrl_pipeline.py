"""
XBRL pipeline regression tests (Milestone E).

Pytest promotion of the 34 disposable probes: pure-unit checks for the
period-ordering fix (B1/F5), declared-scale normalization (D1/D2), the
catalog-driven resolve v2 cascade (C2) and artifact-level invariants (C3/C4)
over the regenerated output/*.json. Arabic literals use \\u escapes so the
source file stays ASCII-clean on Windows tooling.

Run from backend/:  pytest tests/test_xbrl_pipeline.py
Artifact tests skip gracefully when output/*.json are not present.
"""
import json
from pathlib import Path

import pytest

from app.services.xbrl_parser import (
    period_sort_key,
    sort_period_keys,
    declared_scale_factor,
    reconcile_declared_scales,
    _is_non_monetary,
)
from app.services import xbrl_catalog

BACKEND = Path(__file__).resolve().parent.parent
OUTPUT = BACKEND / "output"


# ── B1 / F5: chronological period ordering ────────────────────────────────

def test_period_keys_sort_chronologically():
    keys = ["2025-04_2025-12", "2024-01_2024-12", "2026-01_2026-03", "2023-07"]
    assert sort_period_keys(keys) == ["2023-07", "2024-01_2024-12", "2025-04_2025-12", "2026-01_2026-03"]


def test_latest_slot_prefers_fy_over_short_window_same_end():
    # same end month -> longer span ranks last (FY beats the Q window)
    keys = ["2025-10_2025-12", "2025-01_2025-12"]
    assert sort_period_keys(keys)[-1] == "2025-01_2025-12"


def test_unparseable_keys_sort_to_end():
    assert sort_period_keys(["garbage", "2024-06"])[-1] == "garbage"


# ── D1 / D2: declared-scale tokens ────────────────────────────────────────

@pytest.mark.parametrize("token, factor", [
    ("Actuals", 1.0),
    ("actual", 1.0),
    ("\u0641\u0639\u0644\u064a", 1.0),                                    # فعلي
    ("\u0627\u0644\u0623\u0631\u0642\u0627\u0645 \u0627\u0644\u0641\u0639\u0644\u064a\u0629", 1.0),  # الأرقام الفعلية (2306 filings; once mis-scaled x1000)
    ("Thousands", 1e3),
    ("\u0628\u0627\u0644\u0622\u0644\u0627\u0641", 1e3),                    # بالآلاف
    ("\u0623\u0644\u0641", 1e3),                                          # ألف (legacy 2015 AR)
    ("Millions", 1e6),
    ("\u0628\u0627\u0644\u0645\u0644\u0627\u064a\u064a\u0646", 1e6),      # بالملايين
])
def test_declared_scale_factor_tokens(token, factor):
    assert declared_scale_factor(token) == factor


@pytest.mark.parametrize("token", [None, "", "nan", "   ", "weekly", "\u0627\u0644\u062a\u0642\u0631\u064a\u0628"])
def test_declared_scale_factor_unknown_is_none_never_guessed(token):
    # D2: unknown token -> None -> parser applies factor 1 with basis "unverified".
    # The bare label word '\u0627\u0644\u062a\u0642\u0631\u064a\u0628' (rounding) must not be guessed, and neither must any
    # '\u0627\u0644\u0641'-containing word that is not an actuals/thousands token (2310 mixed-unit bug).
    assert declared_scale_factor(token) is None


def test_actuals_arabic_stem_wins_over_thousands_letters():
    # '\u0627\u0644\u0641\u0639\u0644\u064a\u0629' contains the letters \u0627+\u0644+\u0641 but it is part of the ACTUALS phrase.
    assert declared_scale_factor("\u0627\u0644\u0641\u0639\u0644\u064a\u0629") == 1.0


def test_rounding_label_marks_cover_ar_variants():
    from app.services.xbrl_parser import ROUNDING_LABEL_MARKS
    hay = "\u0645\u0633\u062a\u0648\u0649 \u0627\u0644\u062a\u0642\u0631\u064a\u0628 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645"  # مستوى التقريب المستخدم
    assert any(m in hay for m in ROUNDING_LABEL_MARKS)                       # modern AR header
    assert any(m in "\u0628\u0648\u0627\u0642\u0639" for m in ROUNDING_LABEL_MARKS)  # بواقع (legacy)


@pytest.mark.parametrize("label", [
    "Basic earnings per share",
    "\u0631\u0628\u062d\u064a\u0629 \u0627\u0644\u0633\u0647\u0645 \u0627\u0644\u0623\u0633\u0627\u0633\u064a\u0629",  # ربحية السهم الأساسية
    "Number of shares outstanding",
    "\u0639\u062f\u062f \u0627\u0644\u0623\u0633\u0647\u0645",                                            # عدد الأسهم
    "Dividend per share",
])
def test_non_monetary_labels_are_never_scaled(label):
    assert _is_non_monetary(label) is True


def test_monetary_labels_are_scalable():
    assert _is_non_monetary("Total assets") is False
    assert _is_non_monetary("\u0625\u064a\u062c\u0627\u0631\u0627\u062a \u0645\u062d\u0644\u0648\u0644\u0629") is False  # إيجارات محلوله


# ── D2 hardening: restatement reconciliation of mis-declared filings ──────

def _fake_result(fname, factor, values):
    return {
        "meta": {"source_file": fname, "scale_factor": factor, "scale_basis": "declared"},
        "sections": {"balance_sheet": {"items": [
            {"label": lab, "is_header": False, "values": vals} for lab, vals in values.items()
        ]}},
    }


def test_reconcile_corrects_misdeclared_actuals_2310_pattern():
    # majority Thousands files carry 5e9-scale results; minority 'Actuals' file
    # restates the SAME facts at 5e6 (raw thousands) -> declaration contradicted.
    anchors = [
        _fake_result(f"a{i}.xls", 1000.0, {"total assets": {"2020-12": 5e9}, "share capital": {"2020-12": 2e9}, "inventories": {"2020-12": 1e9}})
        for i in range(2)
    ]
    minority = _fake_result("m.xls", 1.0, {"total assets": {"2020-12": 5e6}, "share capital": {"2020-12": 2e6}, "inventories": {"2020-12": 1e6}})
    reconcile_declared_scales(anchors + [minority])
    assert minority["meta"]["scale_basis"] == "restatement_corrected"
    assert minority["meta"]["scale_factor"] == 1000.0
    assert minority["sections"]["balance_sheet"]["items"][0]["values"]["2020-12"] == 5e9


def test_reconcile_leaves_genuine_actuals_alone_1211_pattern():
    # minority 'Actuals' file already agrees in real SAR (ratio ~1) -> verified.
    anchors = [
        _fake_result(f"a{i}.xls", 1000.0, {"total assets": {"2020-12": 5e9}, "share capital": {"2020-12": 2e9}, "inventories": {"2020-12": 1e9}})
        for i in range(2)
    ]
    minority = _fake_result("m.xls", 1.0, {"total assets": {"2020-12": 5e9}, "share capital": {"2020-12": 2e9}, "inventories": {"2020-12": 1e9}})
    reconcile_declared_scales(anchors + [minority])
    assert minority["meta"]["scale_basis"] == "declared_verified"
    assert minority["meta"]["scale_factor"] == 1.0


def test_reconcile_without_overlap_keeps_declaration():
    # no shared (label, period) facts -> no evidence -> declaration untouched (D2).
    anchor = _fake_result("a.xls", 1000.0, {"total assets": {"2019-12": 5e9}})
    minority = _fake_result("m.xls", 1.0, {"total assets": {"2016-12": 5e6}})
    reconcile_declared_scales([anchor, minority])
    assert minority["meta"]["scale_basis"] == "declared"
    assert minority["meta"]["scale_factor"] == 1.0


# ── C2: catalog-driven resolve v2 ─────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def catalog_loaded():
    xbrl_catalog.load(reload=True)
    assert xbrl_catalog.CONCEPTS, "concepts.json must be populated"


def test_exact_synonym_resolves_with_tier():
    out = xbrl_catalog.resolve("Total assets", statement="balance_sheet")
    assert out is not None and out[0] == "BS-090"
    assert out[2] in ("exact", "normalized")


def test_blocklisted_label_stays_unresolved():
    # "Total" alone is generic poison (blocklist) — must never auto-bind
    assert xbrl_catalog.resolve("Total") is None


def test_fuzzy_match_is_proposal_only_never_applied():
    mangled = "Totall assetss of the entitty group"  # >=0.92-ish but not registered
    out = xbrl_catalog.resolve(mangled)
    assert out is None or out[2] != "fuzzy"          # never returned as a match


def test_takaful_shareholders_only_never_binds_bs200():
    label = ("total insurance/ takaful operations liabilities, surplus (deficit) "
             "and shareholders liabilities and equity")
    out = xbrl_catalog.resolve(label, statement="balance_sheet", sector="INSURANCE")
    assert out is not None and out[0] == "BS-200"
    sh = xbrl_catalog.resolve("total shareholders liabilities and equity",
                              statement="balance_sheet", sector="INSURANCE")
    assert sh is not None and sh[0] == "CONCEPT.TL_E_SHAREHOLDERS"


def test_aggregation_policy_defaults_to_one_of():
    assert xbrl_catalog.aggregation_of("BS-200") == "ONE_OF"


# ── Artifact-level invariants (C3/C4/D3) over regenerated output/*.json ────

def _artifact(sym):
    fp = OUTPUT / f"{sym}_financials.json"
    if not fp.exists():
        pytest.skip(f"{fp.name} not generated yet")
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("sym", ["8010", "8020", "1040", "1211", "4001", "1010"])
def test_artifact_meta_contract(sym):
    meta = _artifact(sym).get("meta", {})
    assert meta.get("rounding") == "normalized_sar"
    assert meta.get("scale_basis") == "declared_per_filing"
    assert meta.get("source_scales"), "per-filing scale audit trail required"
    for s in meta["source_scales"]:
        assert s["factor"] in (1, 1.0, 1000, 1000.0, 1000000, 1000000.0)


@pytest.mark.parametrize("sym", ["8010", "8020", "1040", "1211", "4001", "1010", "2370"])
def test_balance_sheet_identity_ta_eq_tle(sym):
    doc = _artifact(sym)
    bs = doc["sections"].get("standardized_balance_sheet", {})
    items = {it["concept_id"]: it for it in bs.get("items", []) if it.get("concept_id")}
    ta = {p: v for p, v in items.get("BS-090", {}).get("values", {}).items() if isinstance(v, (int, float))}
    tle = {p: v for p, v in items.get("BS-200", {}).get("values", {}).items() if isinstance(v, (int, float))}
    shared = set(ta) & set(tle)
    assert shared, f"{sym}: no overlapping TA/TL&E periods"
    for p in shared:
        assert abs(ta[p] - tle[p]) <= max(1.0, 0.005 * abs(tle[p])), f"{sym} @{p}: TA {ta[p]} != TL&E {tle[p]}"


@pytest.mark.parametrize("sym", ["1040", "2370", "8010"])
def test_conflict_items_pick_a_source_never_sum(sym):
    """F1 regression: values inflated by double-counting (2370, 1040) must be gone;
    any flagged conflict value must equal one of its recorded sources."""
    doc = _artifact(sym)
    for name, sec in doc.get("sections", {}).items():
        if not name.startswith("standardized_"):
            continue
        for it in sec.get("items", []):
            if "conflict" not in (it.get("flags") or []):
                continue
            for p, v in it.get("values", {}).items():
                if not isinstance(v, (int, float)):
                    continue
                pool = [s["value"] for s in (it.get("sources") or {}).get(p, [])
                        if isinstance(s.get("value"), (int, float))]
                # sources is recorded only for CONTESTED periods; uncontested
                # periods legitimately have no audit list.
                if not pool:
                    continue
                assert any(abs(v - s) <= max(1.0, 0.005 * abs(s)) for s in pool), \
                    f"{sym} {it.get('concept_id')} @{p}: conflict value not drawn from sources"


def test_insurer_shareholders_only_poison_isolated():
    """8010 2022-09: the 3,521,216-thousand shareholders-only subtotal must NOT be
    the BS-200 grand total (old F1 mis-bind identified in the audit report)."""
    doc = _artifact("8010")
    bs = doc["sections"].get("standardized_balance_sheet", {})
    items = {it["concept_id"]: it for it in bs.get("items", []) if it.get("concept_id")}
    tle = items.get("BS-200", {}).get("values", {})
    poison = 3_521_216_000  # shareholders-only subtotal, actual SAR
    for p, v in tle.items():
        if isinstance(v, (int, float)):
            assert abs(v - poison) > 0.05 * poison, f"BS-200 @{p} bound to shareholders-only subtotal"
