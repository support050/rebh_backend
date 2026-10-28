# -*- coding: utf-8 -*-
"""eps_growth_service.py — TML "M": quarterly EPS growth (YoY) from compiled XBRL artifacts.

Phase 1 of the Guru Stock Scans plan (2026-10-27). Reads the SAME artifacts the
valuation pages read — backend/output/*_financials.json — and extracts the
standardized basic-EPS line per company, per period. Verified coverage 2026-10-27:
all 257 files carry the EPS line; 243 have >= 4 YoY quarter pairs.

Quarter derivation ladder (STRICT — see the zero-substitution policy):
  1. standalone quarter as filed            2025-04_2025-06
  2. Q2 = H1  - Q1   (2025-01_2025-06 minus 2025-01_2025-03)
  3. Q3 = 9M  - H1   (2025-01_2025-09 minus 2025-01_2025-06)
  4. Q4 = FY  - 9M   (2025-01_2025-12 minus 2025-01_2025-09)
Anything else (e.g. FY without 9M -> Q4) is NOT invented: the quarter is simply
absent from the series. A company whose files only ever carry FY windows falls
back to basis="annual" (FY-vs-FY YoY) so the consumer can decide whether to
trust it; the classic TML gate is latest-quarter YoY >= threshold, so a missing
prev quarter yields prev_yoy=None, never 0.

Quality gates inherited from the parser: an EPS item flagged "out_of_range"
(a real scale anomaly) excludes the symbol outright — a suspect figure never
silently enters a scan column. A "conflict" flag is different: it is the parser
refusing to pick between two disclosed EPS concepts (total vs continuing-ops),
so it is RESOLVED to the headline total EPS (see _resolve_eps_values), not
dropped. Excluded symbols are recorded in `excluded` with a reason.

Manual run from backend/:
  ..\\venv\\Scripts\\python.exe -X utf8 app\\services\\eps_growth_service.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]          # .../backend
DEFAULT_OUTPUT_DIR = BACKEND_DIR / "output"

# standardized_income_statement label emitted by xbrl_parser (IS-160 family)
_EPS_LABELS = {"basic earnings per share (eps)", "basic earnings per share", "eps"}

_PERIOD_RANGE = re.compile(r"^(\d{4})-(\d{2})_(\d{4})-(\d{2})$")


def _months_span(sy: int, sm: int, ey: int, em: int) -> int:
    return (ey - sy) * 12 + (em - sm) + 1


def _num(v):
    """Item value -> float, unwrapping the {member: value} dict form."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for x in v.values():
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                return float(x)
    return None


def _find_eps_item(items: list[dict]) -> dict | None:
    best = None
    for it in items:
        lbl = str(it.get("label", "")).lower().strip()
        if lbl in _EPS_LABELS or "earnings per share" in lbl:
            # prefer an explicitly BASIC line when both basic and diluted exist
            if "basic" in lbl:
                return it
            best = best or it
    return best


def _resolve_eps_values(item: dict) -> dict:
    """Per-period basic-EPS map, disambiguating the parser's 'conflict' flag.

    The parser flags an EPS line 'conflict' when two DIFFERENT disclosed concepts
    roll up to IS-160 for the same period — almost always `total basic EPS` vs
    `basic EPS from continuing operations`. That is a concept choice, not corrupt
    data, and TML/IBD want the HEADLINE (total) figure. So for a conflicted period
    we take the 'total' source when present (else the only source available) and
    keep the symbol; a period with no recoverable value is simply dropped, never
    invented. Non-conflicted periods already carry the resolved value in `values`.
    """
    values = dict(item.get("values", {}))
    sources = item.get("sources") or {}
    for pk, entries in sources.items():
        if not isinstance(entries, list) or not entries:
            continue
        total = [e for e in entries if "total" in str(e.get("label", "")).lower()]
        pick = (total or entries)[0].get("value")
        if isinstance(pick, (int, float)) and not isinstance(pick, bool):
            values[pk] = float(pick)
    return values


def build_quarter_series(values: dict) -> dict:
    """{period_key: raw_value} -> {(year, end_month): eps} standalone + derived.

    Returns (quarters, has_any_window) — quarters only ever contains months in
    {3, 6, 9, 12} end positions.
    """
    parsed: dict[tuple, float] = {}
    for pk, v in values.items():
        m = _PERIOD_RANGE.match(str(pk))
        if not m:
            continue
        sy, sm, ey, em = (int(g) for g in m.groups())
        num = _num(v)
        if num is None:
            continue
        parsed[(sy, sm, ey, em)] = num

    q: dict[tuple, float] = {}
    fy: dict[tuple, float] = {}      # (year,) -> FY value, for the annual fallback
    for (sy, sm, ey, em), num in parsed.items():
        span = _months_span(sy, sm, ey, em)
        if span == 3 and sm in (1, 4, 7, 10):
            q[(ey, em)] = num                       # standalone quarter as filed
        elif span == 12 and sm == 1:
            fy[(ey,)] = num

    # derived quarters: subtract the adjacent shorter YTD window sharing the end year
    for (sy, sm, ey, em), num in parsed.items():
        span = _months_span(sy, sm, ey, em)
        if sy != ey or sm != 1:
            continue                                 # only Jan-start YTD windows derive
        if span == 6:                                # H1
            q1 = parsed.get((ey, 1, ey, 3))
            if q1 is not None and (ey, 6) not in q:
                q[(ey, 6)] = num - q1
        elif span == 9:                              # 9M
            h1 = parsed.get((ey, 1, ey, 6))
            if h1 is not None and (ey, 9) not in q:
                q[(ey, 9)] = num - h1
        elif span == 12:                             # FY
            nine = parsed.get((ey, 1, ey, 9))
            if nine is not None and (ey, 12) not in q:
                q[(ey, 12)] = num - nine
    return q, fy


def _yoy_pct(cur: float, prev) -> float | None:
    if prev is None or abs(prev) < 1e-9:
        return None
    return round((cur - prev) / abs(prev) * 100.0, 2)


def _shift_quarter(year: int, end_month: int, back: int = 1) -> tuple:
    """Previous quarter slot (end_month - 3*back, wrapping into earlier years)."""
    y = year
    m = end_month - 3 * back
    while m <= 0:
        m += 12
        y -= 1
    return (y, m)


def growth_from_quarters(q: dict) -> dict | None:
    """Quarter series -> {latest_q, latest_eps, latest_yoy_pct, prev_yoy_pct, pairs_available}."""
    if not q:
        return None
    keys = sorted(q)                                  # (year, end_month) chronological
    latest = keys[-1]
    prev_slot = _shift_quarter(*latest)
    # pairs: every quarter that has its own YoY twin in the series
    pairs = sum(1 for (y, m), v in q.items()
                if (y - 1, m) in q and abs(q[(y - 1, m)]) > 1e-9)
    ly = _yoy_pct(q[latest], q.get((latest[0] - 1, latest[1])))
    # "previous quarter" for the classic 2-consecutive-quarters TML test: the
    # immediately preceding quarter slot; its YoY twin is 4 slots further back.
    pp = None
    if prev_slot in q:
        # prev_slot YoY twin = same calendar quarter one year earlier
        twin = q.get((prev_slot[0] - 1, prev_slot[1]))
        pp = _yoy_pct(q[prev_slot], twin)
    return {"latest_q": f"{latest[0]}-{latest[1]:02d}", "latest_eps": round(q[latest], 4),
            "latest_yoy_pct": ly, "prev_q_yoy_pct": pp, "pairs_available": pairs}


def load_eps_growth_map(output_dir: Path | str = DEFAULT_OUTPUT_DIR) -> dict:
    """{symbol: {basis, latest_q, latest_yoy_pct, prev_q_yoy_pct, pairs_available}} + exclusions.

    basis: "quarterly" (standalone/derived quarters present) or "annual" (FY-only
    files). A symbol with no EPS line at all, or with no YoY-comparable period,
    is not emitted (it simply never passes the TML gate).
    """
    out = Path(output_dir)
    files = sorted(out.glob("*_financials.json"))
    growth: dict[str, dict] = {}
    excluded: dict[str, str] = {}
    for fp in files:
        symbol = fp.name[: -len("_financials.json")]
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as exc:                       # unreadable artifact = excluded loudly
            excluded[symbol] = f"unreadable:{exc.__class__.__name__}"
            continue
        sec = data.get("sections", {}).get("standardized_income_statement")
        item = _find_eps_item(sec.get("items", [])) if sec else None
        if item is None:
            excluded[symbol] = "no_eps_line"
            continue
        flags = set(item.get("flags") or [])
        if "out_of_range" in flags:                    # real scale anomaly -> honor guardrail
            excluded[symbol] = "out_of_range"
            continue
        # a remaining 'conflict' is the total-vs-continuing concept choice, resolved
        # to the headline (total) EPS below, NOT an exclusion.
        q, fy = build_quarter_series(_resolve_eps_values(item))
        rec = growth_from_quarters(q)
        if rec is not None and rec["latest_yoy_pct"] is not None:
            rec["basis"] = "quarterly"
            growth[symbol] = rec
            continue
        # annual fallback: FY vs FY
        if len(fy) >= 2:
            keys = sorted(fy)
            latest, prev = keys[-1], keys[-2]
            ly = _yoy_pct(fy[latest], fy[prev]) if latest[0] - prev[0] == 1 else None
            if ly is not None:
                growth[symbol] = {"basis": "annual", "latest_q": f"{latest[0]}-12",
                                  "latest_eps": round(fy[latest], 4),
                                  "latest_yoy_pct": ly, "prev_q_yoy_pct": None,
                                  "pairs_available": 1}
                continue
        excluded[symbol] = "no_yoy_pair"
    return {"growth": growth, "excluded": excluded, "files_scanned": len(files)}


def _main() -> None:
    import sys
    res = load_eps_growth_map()
    g, ex = res["growth"], res["excluded"]
    q = sum(1 for v in g.values() if v["basis"] == "quarterly")
    ge25 = sum(1 for v in g.values() if v["latest_yoy_pct"] is not None and v["latest_yoy_pct"] >= 25)
    print(json.dumps({
        "files_scanned": res["files_scanned"],
        "growth_available": len(g),
        "basis_quarterly": q,
        "basis_annual": len(g) - q,
        "latest_yoy_ge_25pct": ge25,
        "excluded": len(ex),
        "exclusion_reasons": {r: sum(1 for x in ex.values() if x == r) for r in set(ex.values())},
    }, indent=1))
    sample = next((k for k in g if k == "1010"), None) or (next(iter(g)) if g else None)
    if sample:
        print(f"sample {sample}: " + json.dumps(g[sample]))
    sys.exit(0)


if __name__ == "__main__":
    _main()
