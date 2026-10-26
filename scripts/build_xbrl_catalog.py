"""
build_xbrl_catalog.py
=====================
Milestone C1 — materialise the XBRL *concept registry* as data-as-code.

Reads the legacy hard-coded structures that still live in
``app.services.xbrl_mapping`` (STANDARD_TEMPLATE, PARAM_MAPPING,
MAPPING_BLOCKLIST) and emits three reviewable JSON files under
``app/data/xbrl_catalog/``:

  concepts.json   one row per canonical concept. The 65 STANDARD_TEMPLATE
                  codes (BS-xxx / IS-xxx / CF-xxx) are kept as-is so every
                  downstream engine / consumer keeps working unchanged; the
                  new sector-aware aggregates use opaque ``CONCEPT.*`` ids.
  synonyms.json   the mechanically-converted PARAM_MAPPING label -> concept
                  table (tier=exact, reviewed=true) plus the insurance / bank
                  sector-profile additions and the recurring-gap seeds.
  blocklist.json  labels that must NEVER auto-bind (ambiguous "total", diluted
                  EPS variants, reserve components, ...).
  proposals.json  fuzzy >= FUZZY_THRESHOLD candidates land here for a human to
                  promote; regenerated empty by this script so a build never
                  silently wipes curated proposals it did not create.

Adding a synonym going forward = editing synonyms.json (data), never code.

Run:  cd backend && python scripts/build_xbrl_catalog.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.xbrl_mapping import (
    STANDARD_TEMPLATE,
    PARAM_MAPPING,
    MAPPING_BLOCKLIST,
)

CATALOG_DIR = Path(__file__).resolve().parent.parent / "app" / "data" / "xbrl_catalog"

# ── unit classes (replaces the hard-coded NON_MONETARY_LABELS heuristic) ──────
UNIT_CLASS_OVERRIDES = {
    "IS-160": "per_share",   # basic EPS
    "IS-170": "count",       # weighted-average number of shares
}

# ── aggregation policy (C4). ONE_OF is the default for every total / subtotal. ─
# PARENT_CHILD: the concept may be reported both as an aggregate and via sub
# lines; take the largest registered parent. SUMMABLE is reserved for explicitly
# marked non-total components and is NOT used by default anywhere.
AGGREGATION_OVERRIDES = {
    "IS-010": "PARENT_CHILD",   # Revenue / Turnover
    "IS-050": "PARENT_CHILD",   # General and administrative expenses
}

# _PARENT_LABELS from the parser, split onto the concepts they aggregate.
PARENT_LABELS = {
    "IS-010": [
        "total revenue", "revenue", "total revenues", "total income",
        "net revenue", "net revenues", "gross premiums written",
        "insurance revenue",
    ],
    "IS-050": [
        "general and administrative expenses",
        "general and administration expenses",
    ],
}

SECTOR_OVERRIDES = {
    # sector-tagged for documentation; the insurance/bank synonyms below carry
    # self-identifying labels so a sector guard is not required at match time.
    "CONCEPT.TL_E_SHAREHOLDERS": "INSURANCE",
    "CONCEPT.TAKAFUL_SHAREHOLDERS_ASSETS": "INSURANCE",
    "CONCEPT.TAKAFUL_SHAREHOLDERS_LIABILITIES": "INSURANCE",
    "CONCEPT.TAKAFUL_OPS_LIABILITIES": "INSURANCE",
    "CONCEPT.TAKAFUL_OPS_LIAB_SURPLUS": "INSURANCE",
    "CONCEPT.TAKAFUL_OPS_ASSETS": "INSURANCE",
}

# ── new CONCEPT.* rows (sector aggregates the generic template cannot express) ─
NEW_CONCEPTS = {
    "CONCEPT.TL_E_SHAREHOLDERS": {
        "label_en": "Total shareholders liabilities and equity",
        "label_ar": "\u0625\u062c\u0645\u0627\u0644\u064a \u0645\u0637\u0644\u0648\u0628\u0627\u062a \u0648\u062d\u0642\u0648\u0642 \u0627\u0644\u0645\u0633\u0627\u0647\u0645\u064a\u0646",
        "statement": "balance_sheet", "is_subtotal": True, "unit_class": "monetary",
        "aggregation": "ONE_OF",
    },
    "CONCEPT.TAKAFUL_SHAREHOLDERS_ASSETS": {
        "label_en": "Total shareholders assets",
        "label_ar": "\u0625\u062c\u0645\u0627\u0644\u064a \u0623\u0635\u0648\u0644 \u0627\u0644\u0645\u0633\u0627\u0647\u0645\u064a\u0646",
        "statement": "balance_sheet", "is_subtotal": True, "unit_class": "monetary",
        "aggregation": "ONE_OF",
    },
    "CONCEPT.TAKAFUL_SHAREHOLDERS_LIABILITIES": {
        "label_en": "Total shareholders liabilities",
        "label_ar": "\u0625\u062c\u0645\u0627\u0644\u064a \u0645\u0637\u0644\u0648\u0628\u0627\u062a \u0627\u0644\u0645\u0633\u0627\u0647\u0645\u064a\u0646",
        "statement": "balance_sheet", "is_subtotal": True, "unit_class": "monetary",
        "aggregation": "ONE_OF",
    },
    "CONCEPT.TAKAFUL_OPS_LIABILITIES": {
        "label_en": "Total insurance/takaful operations liabilities",
        "label_ar": "\u0625\u062c\u0645\u0627\u0644\u064a \u0645\u0637\u0644\u0648\u0628\u0627\u062a \u0639\u0645\u0644\u064a\u0627\u062a \u0627\u0644\u062a\u0623\u0645\u0646",
        "statement": "balance_sheet", "is_subtotal": True, "unit_class": "monetary",
        "aggregation": "ONE_OF",
    },
    "CONCEPT.TAKAFUL_OPS_LIAB_SURPLUS": {
        "label_en": "Total insurance/takaful operations liabilities and surplus (deficit)",
        "label_ar": "\u0625\u062c\u0645\u0627\u0644\u064a \u0645\u0637\u0644\u0648\u0628\u0627\u062a \u0639\u0645\u0644\u064a\u0627\u062a \u0627\u0644\u062a\u0623\u0645\u0646 \u0648\u0627\u0644\u0641\u0636\u0644",
        "statement": "balance_sheet", "is_subtotal": True, "unit_class": "monetary",
        "aggregation": "ONE_OF",
    },
    "CONCEPT.TAKAFUL_OPS_ASSETS": {
        "label_en": "Total insurance/takaful operations assets",
        "label_ar": "\u0625\u062c\u0645\u0627\u0644\u064a \u0623\u0635\u0648\u0644 \u0639\u0645\u0644\u064a\u0627\u062a \u0627\u0644\u062a\u0623\u0645\u0646",
        "statement": "balance_sheet", "is_subtotal": True, "unit_class": "monetary",
        "aggregation": "ONE_OF",
    },
    "CONCEPT.TOTAL_NON_CURRENT_ASSETS": {
        "label_en": "Total non-current assets",
        "label_ar": "\u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u0623\u0635\u0648\u0644 \u063a\u064a\u0631 \u0627\u0644\u0645\u062a\u062f\u0627\u0648\u0644\u0629",
        "statement": "balance_sheet", "is_subtotal": True, "unit_class": "monetary",
        "aggregation": "ONE_OF",
    },
    "CONCEPT.TOTAL_NON_CURRENT_LIABILITIES": {
        "label_en": "Total non-current liabilities",
        "label_ar": "\u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u0645\u0637\u0644\u0648\u0628\u0627\u062a \u063a\u064a\u0631 \u0627\u0644\u0645\u062a\u062f\u0627\u0648\u0644\u0629",
        "statement": "balance_sheet", "is_subtotal": True, "unit_class": "monetary",
        "aggregation": "ONE_OF",
    },
    "CONCEPT.TOTAL_OPERATING_EXPENSES": {
        "label_en": "Total operating expenses",
        "label_ar": "\u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u0645\u0635\u0627\u0631\u064a\u0641 \u0627\u0644\u062a\u0634\u063a\u064a\u0644\u064a\u0629",
        "statement": "income_statement", "is_subtotal": True, "unit_class": "monetary",
        "aggregation": "ONE_OF",
    },
}

# ── synonym edits applied on top of the mechanical PARAM_MAPPING conversion ───
# (label -> (concept_id, direction)). Overrides repoint an existing binding;
# additions create new ones. Sector-specific labels are self-identifying so
# sector guard is left at ALL to avoid fragile sector-string matching.
SYNONYM_OVERRIDES = {
    # F1 insurance: the shareholders-only subtotal must NOT poison BS-200 (grand total).
    "total shareholders liabilities and equity": ("CONCEPT.TL_E_SHAREHOLDERS", 1),
    # Bank CF: the pre-FX cash movement is a cash-flow net-change, not a BS cash balance.
    "increase (decrease) in cash and cash equivalents before effect of exchange rate changes": ("CF-140", 1),
}
SYNONYM_ADDITIONS = {
    # Insurance grand total liabilities-and-equity (equals Total Assets). Two real
    # wordings occur in the corpus (545x + 21x); the tail is
    # "...and shareholders liabilities and equity", NOT "...and shareholders equity".
    "total insurance/ takaful operations liabilities, surplus (deficit) and shareholders liabilities and equity": ("BS-200", 1),
    "total insurance operations' liabilities, surplus (deficit) and shareholders' liabilities and equity": ("BS-200", 1),
    # Insurer operations-side totals -> dedicated concepts (these are NOT the
    # group grand totals, so they must not bind BS-090 / BS-160).
    "total insurance/ takaful operations assets": ("CONCEPT.TAKAFUL_OPS_ASSETS", 1),
    "total shareholders assets": ("CONCEPT.TAKAFUL_SHAREHOLDERS_ASSETS", 1),
    "total shareholders liabilities": ("CONCEPT.TAKAFUL_SHAREHOLDERS_LIABILITIES", 1),
    "total insurance/ takaful operations liabilities": ("CONCEPT.TAKAFUL_OPS_LIABILITIES", 1),
    "total insurance/ takaful operations liabilities and surplus (deficit)": ("CONCEPT.TAKAFUL_OPS_LIAB_SURPLUS", 1),
    # Bank CF aggregate (was blocklisted).
    "net cash flows from (used in) operations": ("CF-060", 1),
    # EPS-from-continuing (was blocklisted) -> basic EPS concept.
    "basic earnings (loss) per share from continuing operations": ("IS-160", 1),
    # Equity-attributable-to-parent variants (were blocklisted) -> BS-185.
    "equity attributable to owners of parent": ("BS-185", 1),
    "total equity attributable to equity holders of company": ("BS-185", 1),
    "total equity attributable to equity holders of parent company": ("BS-185", 1),
    # Non-current subtotals (were blocklisted to protect BS-090/BS-160) -> own concepts.
    "total non-current assets": ("CONCEPT.TOTAL_NON_CURRENT_ASSETS", 1),
    "total non current assets": ("CONCEPT.TOTAL_NON_CURRENT_ASSETS", 1),
    "total non-current liabilities": ("CONCEPT.TOTAL_NON_CURRENT_LIABILITIES", 1),
    "total non current liabilities": ("CONCEPT.TOTAL_NON_CURRENT_LIABILITIES", 1),
    # Recurring aggregate -> own concept (do not force onto IS-050).
    "total operating expenses": ("CONCEPT.TOTAL_OPERATING_EXPENSES", 1),
}

# Blocklist labels promoted out of the deny-list because they now have an
# explicit, reviewed synonym (or a dedicated concept) above.
BLOCKLIST_REMOVALS = {
    "net cash flows from (used in) operations",
    "basic earnings (loss) per share from continuing operations",
    "equity attributable to owners of parent",
    "total equity attributable to equity holders of company",
    "total equity attributable to equity holders of parent company",
    "total non-current assets",
    "total non current assets",
    "total non-current liabilities",
    "total non current liabilities",
}


def build_concepts():
    concepts = {}
    for code, info in STANDARD_TEMPLATE.items():
        concepts[code] = {
            "aliases": [code],
            "label_en": info["line_en"],
            "label_ar": info["line_ar"],
            "statement": info["statement"],
            "sector": "ALL",
            "unit_class": UNIT_CLASS_OVERRIDES.get(code, "monetary"),
            "is_subtotal": info["is_subtotal"],
            "aggregation": AGGREGATION_OVERRIDES.get(code, "ONE_OF"),
            "direction": 1,
            "parent_labels": PARENT_LABELS.get(code, []),
        }
    for cid, meta in NEW_CONCEPTS.items():
        concepts[cid] = {
            "aliases": [cid],
            "label_en": meta["label_en"],
            "label_ar": meta["label_ar"],
            "statement": meta["statement"],
            "sector": SECTOR_OVERRIDES.get(cid, "ALL"),
            "unit_class": meta["unit_class"],
            "is_subtotal": meta["is_subtotal"],
            "aggregation": meta["aggregation"],
            "direction": 1,
            "parent_labels": [],
        }
    return concepts


def build_synonyms(concepts):
    rows = {}  # raw_label -> row (dedup on label)
    for raw_label, (code, direction) in PARAM_MAPPING.items():
        rows[raw_label] = {
            "raw_label": raw_label,
            "concept_id": code,
            "direction": direction,
            "sector": "ALL",
            "statement": concepts.get(code, {}).get("statement"),
            "tier": "exact",
            "reviewed": True,
        }
    for label, (code, direction) in list(SYNONYM_OVERRIDES.items()) + list(SYNONYM_ADDITIONS.items()):
        rows[label] = {
            "raw_label": label,
            "concept_id": code,
            "direction": direction,
            "sector": "ALL",
            "statement": concepts.get(code, {}).get("statement"),
            "tier": "exact",
            "reviewed": True,
        }
    return sorted(rows.values(), key=lambda r: r["raw_label"])


def build_blocklist():
    return sorted(l for l in MAPPING_BLOCKLIST if l not in BLOCKLIST_REMOVALS)


def main():
    concepts = build_concepts()
    synonyms = build_synonyms(concepts)
    blocklist = build_blocklist()

    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    (CATALOG_DIR / "concepts.json").write_text(
        json.dumps(concepts, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (CATALOG_DIR / "synonyms.json").write_text(
        json.dumps(synonyms, ensure_ascii=False, indent=2), encoding="utf-8")
    (CATALOG_DIR / "blocklist.json").write_text(
        json.dumps(blocklist, ensure_ascii=False, indent=2), encoding="utf-8")

    proposals_path = CATALOG_DIR / "proposals.json"
    if not proposals_path.exists():
        proposals_path.write_text("[]", encoding="utf-8")

    print(f"concepts : {len(concepts)}  (template={len(STANDARD_TEMPLATE)}, new={len(NEW_CONCEPTS)})")
    print(f"synonyms : {len(synonyms)}  (seed={len(PARAM_MAPPING)})")
    print(f"blocklist: {len(blocklist)}  (removed {len(BLOCKLIST_REMOVALS)})")
    print(f"proposals: {len(proposals_path.read_text(encoding='utf-8')) > 2 and 'kept' or 'empty'}")
    print(f"-> {CATALOG_DIR}")


if __name__ == "__main__":
    main()
