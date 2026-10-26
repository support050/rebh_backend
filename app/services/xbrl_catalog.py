"""
xbrl_catalog.py
===============
Milestone C1/C2 — runtime loader for the XBRL concept registry.

The authoritative data lives in ``app/data/xbrl_catalog/*.json`` (generated once
by ``scripts/build_xbrl_catalog.py`` from the legacy hard-coded seed and then
hand-edited as data). This module loads those files and exposes:

  * CONCEPTS            concept_id -> concept row
  * synonym lookup      raw label -> (concept_id, direction, tier)
  * blocklist           labels that must never auto-bind
  * proposals           fuzzy >= FUZZY_THRESHOLD candidates, captured for a
                        human to promote into synonyms.json (never auto-applied)

``xbrl_mapping.resolve_mapping`` delegates here. The legacy PARAM_MAPPING /
STANDARD_TEMPLATE structures remain in ``xbrl_mapping`` only as the build seed
and as a fallback when the JSON catalogue is absent (e.g. a fresh checkout that
has not run the build script yet).
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

CATALOG_DIR = Path(__file__).resolve().parent.parent / "data" / "xbrl_catalog"

FUZZY_THRESHOLD = 0.92

_SWAP_PHRASES = (
    ("associates and joint ventures", "joint ventures and associates"),
    ("joint ventures and associates", "associates and joint ventures"),
)


# ── string normalisation (shared with the parser) ────────────────────────────

def normalize_label(label):
    if not label:
        return ""
    s = str(label).replace("\xa0", " ").lower().strip()
    s = re.sub(r"\s*\[abstract\]", "", s, flags=re.I)
    s = re.sub(r"\s*\[line items\]", "", s, flags=re.I)
    s = re.sub(r"\s*\[member\]", "", s, flags=re.I)
    s = s.replace(",", " ")
    s = re.sub(r"\s*/\s*", " / ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def token_key(label):
    tokens = sorted(t for t in normalize_label(label).replace("/", " ").split() if t)
    return " ".join(tokens)


# ── loading ──────────────────────────────────────────────────────────────────

def _read(name, default):
    p = CATALOG_DIR / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


CONCEPTS: dict = {}
SYNONYM_ROWS: list = []
_BLOCKLIST_RAW: set = set()
NORM_MAP: dict = {}        # normalized label -> (concept_id, direction)
TOKEN_MAP: dict = {}       # token key -> (concept_id, direction)
_BLOCKLIST_KEYS: set = set()
_PROPOSALS: dict = {}       # label -> {concept_id, score, tier}
_LOADED = False


def _add_blocklist_key(keys, label):
    raw = str(label).lower().strip()
    keys.add(raw)
    keys.add(normalize_label(label))
    keys.add(token_key(label))


def load(reload: bool = False):
    global _LOADED
    if _LOADED and not reload:
        return
    CONCEPTS.clear(); SYNONYM_ROWS.clear(); _BLOCKLIST_RAW.clear()
    NORM_MAP.clear(); TOKEN_MAP.clear(); _BLOCKLIST_KEYS.clear()

    CONCEPTS.update(_read("concepts.json", {}))
    rows = _read("synonyms.json", [])
    SYNONYM_ROWS.extend(rows)
    for label in _read("blocklist.json", []):
        _BLOCKLIST_RAW.add(str(label).lower().strip())

    for label in _BLOCKLIST_RAW:
        _add_blocklist_key(_BLOCKLIST_KEYS, label)

    ambiguous = set()
    for row in rows:
        label = row.get("raw_label", "")
        if is_blocklisted(label):
            continue
        cid = row.get("concept_id")
        direction = int(row.get("direction", 1))
        nk = normalize_label(label)
        if nk and nk not in NORM_MAP:
            NORM_MAP[nk] = (cid, direction)
        tk = token_key(label)
        if not tk:
            continue
        if tk in TOKEN_MAP and TOKEN_MAP[tk][0] != cid:
            ambiguous.add(tk)
        elif tk not in TOKEN_MAP:
            TOKEN_MAP[tk] = (cid, direction)
    for tk in ambiguous:
        TOKEN_MAP.pop(tk, None)

    _LOADED = True


def is_blocklisted(label) -> bool:
    if not label:
        return False
    raw = str(label).lower().strip()
    if raw in _BLOCKLIST_KEYS or raw in _BLOCKLIST_RAW:
        return True
    return normalize_label(label) in _BLOCKLIST_KEYS or token_key(label) in _BLOCKLIST_KEYS


# ── concept accessors ─────────────────────────────────────────────────────────

def get_concept(concept_id):
    return CONCEPTS.get(concept_id)


def concept_ids_for_statement(statement):
    return [cid for cid, c in CONCEPTS.items() if c.get("statement") == statement]


def aggregation_of(concept_id):
    return CONCEPTS.get(concept_id, {}).get("aggregation", "ONE_OF")


def unit_class_of(concept_id):
    return CONCEPTS.get(concept_id, {}).get("unit_class", "monetary")


def is_monetary(concept_id):
    return unit_class_of(concept_id) == "monetary"


def parent_labels_of(concept_id):
    return {normalize_label(p) for p in CONCEPTS.get(concept_id, {}).get("parent_labels", [])}


def canonical_norm_label(concept_id):
    """Normalised canonical English label, used by the ONE_OF conflict picker."""
    return normalize_label(CONCEPTS.get(concept_id, {}).get("label_en", ""))



def _valid_for_statement(concept_id, statement):
    if not statement:
        return True
    st = CONCEPTS.get(concept_id, {}).get("statement")
    return st is None or st == statement


# ── proposals (fuzzy candidates, captured for human review — never auto-applied)

def _record_proposal(label, concept_id, score):
    key = normalize_label(label)
    prev = _PROPOSALS.get(key)
    if prev is None or score > prev["score"]:
        _PROPOSALS[key] = {
            "raw_label": str(label).strip(),
            "concept_id": concept_id,
            "score": round(float(score), 4),
            "tier": "fuzzy",
            "reviewed": False,
        }


def get_proposals():
    return sorted(_PROPOSALS.values(), key=lambda p: -p["score"])


def flush_proposals(path=None):
    """Merge in-memory fuzzy proposals into proposals.json (dedup by label)."""
    path = Path(path) if path else (CATALOG_DIR / "proposals.json")
    load()
    existing = {}
    if path.exists():
        try:
            for row in json.loads(path.read_text(encoding="utf-8")):
                existing[normalize_label(row.get("raw_label", ""))] = row
        except Exception:
            existing = {}
    for key, prop in _PROPOSALS.items():
        # never downgrade a human-reviewed proposal; keep the best-scored candidate
        cur = existing.get(key)
        if cur and cur.get("reviewed"):
            continue
        if cur and cur.get("score", 0) >= prop["score"]:
            continue
        existing[key] = prop
    out = sorted(existing.values(), key=lambda p: (-p.get("score", 0), p.get("raw_label", "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(out)


# ── resolve_mapping v2 core (called by xbrl_mapping.resolve_mapping) ──────────

def resolve(label, statement=None, sector=None):
    """Return (concept_id, direction, tier) or None.

    Cascade: blocklist -> exact -> normalized -> token/swap -> fuzzy(proposal).
    Fuzzy >= FUZZY_THRESHOLD is *recorded as a proposal* and returns None: it is
    never auto-applied (auto-applied fuzzy binds are exactly what produced the
    BS-200 mis-bind and the `Total` collisions).
    """
    load()
    if not label or is_blocklisted(label):
        return None

    raw = str(label).lower().strip()
    norm = normalize_label(label)
    if not norm:
        return None

    def _ok(hit):
        cid, direction = hit
        return _valid_for_statement(cid, statement)

    # 1/2. exact + normalized against the synonym index
    hit = NORM_MAP.get(raw) or NORM_MAP.get(norm)
    if hit and _ok(hit):
        return (hit[0], hit[1], "exact")

    # 3. swap phrases (associates/joint-ventures word-order)
    for a, b in _SWAP_PHRASES:
        if a in norm:
            swapped = norm.replace(a, b)
            hit = NORM_MAP.get(swapped)
            if hit and _ok(hit):
                return (hit[0], hit[1], "token")

    # 4. token-set (order-insensitive) match
    tk = token_key(label)
    hit = TOKEN_MAP.get(tk)
    if hit and _ok(hit):
        return (hit[0], hit[1], "token")

    # 5. fuzzy -> proposal only (never auto-applied)
    best = None
    best_ratio = 0.0
    for nk, (cid, _direction) in NORM_MAP.items():
        if not _valid_for_statement(cid, statement):
            continue
        if abs(len(nk) - len(norm)) > max(8, int(len(norm) * 0.25)):
            continue
        ratio = SequenceMatcher(None, norm, nk).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = cid
    if best is not None and best_ratio >= FUZZY_THRESHOLD:
        _record_proposal(label, best, best_ratio)
        return None
    return None
