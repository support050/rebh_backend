"""Company name -> TASI symbol resolution for the market-report tables.

`substantial_shareholders` is the one market-report table whose scraper cannot give
us a symbol: the Tadawul "Major Shareholders" page groups rows under a single-cell
company header that contains the company NAME only
(`market_reports_scraper.scrape_substantial_shareholders`), so every one of the 482
live rows has symbol NULL while net_short / foreign_headroom / buybacks / sbl are
100 % symbolled.

Why the two sources suggested in the brief do not work, measured on the live DB:
  * `companies` — 271 rows, name_en NOT NULL = 29, and all 29 are placeholders
    ("Company 2084", even "Company --symbol") written by
    `official_filings.process_ingestion()` when it auto-creates a company.
    name_ar is 100 % NULL. Useless for matching.
  * `static_stock_info` — has no name column at all (only symbol + 4 numeric fields).

What does work, and is used here:
  * backend/output/companies_list.json  — full legal names ("Riyad Bank")
  * backend/company_symbols.csv         — short/exchange names ("ALBABTAIN")
  * prices.company_name                 — maintained by the pipeline for 293 symbols

Two hard rules, because this lands on a financial screen:
  1. A name is only resolved when it maps to exactly ONE symbol across every source.
     Any key claimed by two different symbols is dropped and reported as ambiguous.
  2. No fuzzy / prefix / best-guess matching. Unmatched names stay NULL on purpose;
     the backfill script prints them so a human can extend the source files.
"""
from __future__ import annotations

import csv
import json
import os
import re
import unicodedata
from typing import Dict, List, Optional, Set, Tuple

# Corporate noise words that carry no identity. "Al"/"El" as separate tokens are
# dropped too, but never inside a word (Almarsoodi stays intact).
_SUFFIXES = {
    "CO", "COMPANY", "CORP", "CORPORATION", "QSC", "QSBS", "PJSC", "LLC", "LTD",
    "SA", "HOLDING", "HOLDINGS", "HOLDING COMPANY", "GROUP", "THE", "FOR",
    "AL", "EL", "AND", "OF", "IN", "SAR", "SARAR", "ARABIA", "SAUDI",
}

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CACHE: Optional[Tuple[Dict[str, str], Set[str]]] = None


def normalise_name(name: Optional[str]) -> str:
    """Case/space/punctuation-folded, suffix-stripped key for a company name."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("&", " and ")
    s = re.sub(r"[^A-Za-z0-9\u0600-\u06FF]+", " ", s).upper()
    tokens = [t for t in s.split() if t and t not in _SUFFIXES]
    return " ".join(tokens)


def normalise_symbol(raw: Optional[str]) -> Optional[str]:
    """Canonical join form of a symbol: strip exchange prefixes/suffixes.

    TADAWUL:4001 / 4001.SR / " 4001 " -> "4001". Everything in these tables is a
    bare 4-digit TASI code already (verified against prices.symbol), so today this
    is an identity transform for well-formed input — its job is to keep junk out of
    the join column if a scraper ever starts emitting qualified tickers.
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    s = re.sub(r"^[A-Z]+[:/\\.\-]", "", s)      # TADAWUL: / 4001
    s = re.sub(r"[.\-](SR|SS|SE|UNITECH|EQITES)$", "", s)
    s = s.strip()
    return s or None


def _pairs_from_json(path: str) -> List[Tuple[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []

    def walk(node):
        if isinstance(node, dict):
            sym = node.get("symbol") or node.get("Symbol")
            nm = (node.get("company_name") or node.get("name_en")
                  or node.get("name") or node.get("Company") or node.get("Name"))
            if sym and nm:
                out.append((str(nm), str(sym)))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out


def _pairs_from_csv(path: str) -> List[Tuple[str, str]]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("Symbol") or row.get("symbol") or "").strip()
            nm = (row.get("Company") or row.get("company") or row.get("name") or "").strip()
            if sym and nm:
                out.append((nm, sym))
    return out


def _pairs_from_prices() -> List[Tuple[str, str]]:
    """symbol/company_name as the pipeline maintains it. Never fatal if unavailable."""
    try:
        from app.core.database import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            rows = db.execute(text(
                "SELECT DISTINCT ON (symbol) symbol, company_name FROM prices "
                "WHERE company_name IS NOT NULL ORDER BY symbol, date DESC")).fetchall()
        return [(r[1], r[0]) for r in rows if r[0] and r[1]]
    except Exception:
        return []


def _all_source_pairs() -> List[Tuple[str, str]]:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))          # backend/
    workspace = os.path.dirname(root)
    sources = [
        _pairs_from_json(os.path.join(root, "output", "companies_list.json")),
        _pairs_from_csv(os.path.join(root, "company_symbols.csv")),
        _pairs_from_csv(os.path.join(workspace, "company_symbols.csv")),
        _pairs_from_prices(),
    ]
    return [p for group in sources for p in group]


def build_lookup(force_refresh: bool = False) -> Tuple[Dict[str, str], Set[str]]:
    """(unambiguous name-key -> symbol, set of ambiguous keys)."""
    global _CACHE
    if _CACHE is not None and not force_refresh:
        return _CACHE

    claims: Dict[str, Set[str]] = {}
    placeholder = re.compile(r"^COMPANY\s*[0-9\-]*$", re.I)
    for name, symbol in _all_source_pairs():
        key = normalise_name(name)
        sym = normalise_symbol(symbol)
        if not key or not sym:
            continue
        if placeholder.match(key):        # "Company 2084" style auto-created rows
            continue
        claims.setdefault(key, set()).add(sym)

    clean = {k: v.pop() for k, v in claims.items() if len(v) == 1}
    ambiguous = {k for k, v in claims.items() if len(v) > 1}
    _CACHE = (clean, ambiguous)
    return _CACHE


def resolve_name(company_name: Optional[str]) -> Optional[str]:
    """Exact normalised match only. None = not confirmed, leave the column NULL."""
    key = normalise_name(company_name)
    if not key:
        return None
    lookup, _ = build_lookup()
    return lookup.get(key)


def ambiguous_keys() -> Set[str]:
    _, amb = build_lookup()
    return amb


def known_name_keys() -> List[str]:
    lookup, _ = build_lookup()
    return sorted(lookup)
