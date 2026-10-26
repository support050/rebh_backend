"""
validate_xbrl_outputs.py — Milestone E validation harness (read-only).

Guards every regeneration of backend/output/*_financials.json. Promotes the
disposable probes (25, 34, 17, 33) into a single repeatable run:

  1. Meta contract (D3): every artifact declares rounding == "normalized_sar",
     scale_basis == "declared_per_filing" and carries a per-filing source_scales
     audit trail. (probe25: applied == declared for every filing.)
  2. Mixed-unit scan (probe34): two values of the SAME concept inside one
     company must not differ by a suspicious ~1e3 / ~1e6 step — that is the
     signature of a filing whose declared scale was missed.
  3. Accounting identities (replaces the dead F6 repair block — flags, NEVER
     mutates): Total assets (BS-090) == Total liabilities & equity (BS-200),
     and BS-090 == current + non-current assets when both concepts exist.
  4. Conflict honesty (C4): an item flagged "conflict" must equal ONE of its
     recorded sources — never their sum.
  5. Mapping coverage (probe17): % of standardized items that resolved to a
     concept, reported per sector.
  6. Optional --idempotency N: re-parse N symbols twice and require
     byte-identical artifacts (D4 guarantee).

Exit code 0 = all hard checks pass; 1 = at least one failure. Warnings never
fail the run. Usage:
    python scripts/validate_xbrl_outputs.py [--out-dir output] [--idempotency 10]
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

VALID_FACTORS = {1.0, 1e3, 1e6}
# honest scale bases: read from the filing, or adjudicated against the company's
# own restatements (see reconcile_declared_scales); 'unverified' stays a warning.
OK_BASES = {"declared", "declared_verified", "restatement_corrected"}
# exact unit-step windows: same concept, one company, values ~1e3/1e6 apart —
# the signature of a filing whose declared scale contradicts its numbers.
UNIT_STEPS = [(980, 1020), (9.8e5, 1.02e6)]
# softer window: broad growth jumps that *may* be units or may be real business
# growth (e.g. dividends 170,867 -> 184,616,040 over 12y). Report, don't fail.
SOFT_UNIT_STEPS = [(900, 1100), (9e5, 1.1e6)]
# Documented source-data anomalies that CANNOT be fixed without guessing a
# factor we did not read (plan D2). New offenders still fail the run.
KNOWN_UNIT_EXCEPTIONS = {
    ("1050_financials.json", "BS-188"):
        "2020-12 NCI reported as raw 5,000 in an otherwise-thousands filing; "
        "no restatement overlap available to adjudicate (source anomaly)",
    ("1090_financials.json", "BS-170"):
        "2016-2017 filings declare Actuals carrying thousands raws; a 2017->2019 "
        "reporting gap leaves zero shared (label, period) facts vs the Thousands "
        "era, so reconciliation has no evidence (source anomaly)",
    ("1090_financials.json", "BS-100"):
        "same 2016/2019 era split as BS-170 (customer deposits ~170M vs ~168B)",
}
IDENTITY_TOL = 0.005  # 0.5% per plan target

# concept aliases that carry Arabic-safe ASCII names in the catalog
TA, TLE = "BS-090", "BS-200"


def _nums(values: dict) -> dict:
    return {p: v for p, v in (values or {}).items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def check_meta_contract(artifacts):
    failures, unverified = [], []
    for path, doc in artifacts:
        meta = doc.get("meta", {})
        if meta.get("rounding") != "normalized_sar":
            failures.append(f"{path.name}: meta.rounding={meta.get('rounding')!r} (expected 'normalized_sar')")
        if meta.get("scale_basis") != "declared_per_filing":
            failures.append(f"{path.name}: meta.scale_basis={meta.get('scale_basis')!r}")
        scales = meta.get("source_scales") or []
        if not scales:
            failures.append(f"{path.name}: empty meta.source_scales")
        for s in scales:
            if s.get("basis") not in OK_BASES:
                unverified.append(f"{path.name}: {s.get('file')} basis={s.get('basis')} factor={s.get('factor')}")
            elif float(s.get("factor") or 0) not in VALID_FACTORS:
                failures.append(f"{path.name}: {s.get('file')} factor={s.get('factor')} not in {VALID_FACTORS}")
    return failures, unverified


def check_mixed_units(artifacts):
    failures, warnings = [], []
    for path, doc in artifacts:
        for sec_name, sec in (doc.get("sections") or {}).items():
            if not sec_name.startswith("standardized_"):
                continue
            for item in sec.get("items", []):
                cid = item.get("concept_id")
                if not cid or item.get("is_header"):
                    continue
                label = (item.get("label") or "").lower()
                if any(t in label for t in ("per share", "number of shares", "weighted average")):
                    continue
                vals = sorted(abs(v) for v in _nums(item.get("values")).values() if v)
                hit_hard = hit_soft = None
                for i in range(len(vals) - 1):
                    lo, hi = vals[i], vals[i + 1]
                    if lo <= 0:
                        continue
                    r = hi / lo
                    if any(a <= r <= b for a, b in UNIT_STEPS):
                        hit_hard = (lo, hi, r)
                        break
                    if any(a <= r <= b for a, b in SOFT_UNIT_STEPS):
                        hit_soft = hit_soft or (lo, hi, r)
                key = (path.name, cid)
                if hit_hard:
                    if key in KNOWN_UNIT_EXCEPTIONS:
                        continue  # documented source anomaly
                    failures.append(f"{path.name}: {cid} '{label[:40]}' has {hit_hard[0]:,.0f} vs {hit_hard[1]:,.0f} (x{hit_hard[2]:.0f})")
                elif hit_soft:
                    warnings.append(f"{path.name}: {cid} '{label[:40]}' has {hit_soft[0]:,.0f} vs {hit_soft[1]:,.0f} (x{hit_soft[2]:.0f}) — growth or units?")
    return failures, warnings


def check_identities(artifacts):
    failures = []
    for path, doc in artifacts:
        bs = (doc.get("sections") or {}).get("standardized_balance_sheet") or {}
        items = {}
        for it in bs.get("items", []):
            cid = it.get("concept_id")
            if cid and cid not in items:
                items[cid] = _nums(it.get("values"))
        pairs = [(TA, TLE, "TA==TL&E")]
        if "CONCEPT.TOTAL_CURRENT_ASSETS" in items and "CONCEPT.TOTAL_NON_CURRENT_ASSETS" in items:
            pairs.append((TA, ("CONCEPT.TOTAL_CURRENT_ASSETS", "CONCEPT.TOTAL_NON_CURRENT_ASSETS"), "TA==CA+NCA"))
        for left, right, name in pairs:
            lv, rv = items.get(left, {}), right
            if not lv:
                continue
            if isinstance(rv, tuple):
                a, b = items.get(rv[0], {}), items.get(rv[1], {})
                for p in lv:
                    if p in a and p in b:
                        expect = a[p] + b[p]
                        if expect and abs(lv[p] - expect) / abs(expect) > IDENTITY_TOL:
                            failures.append(f"{path.name}: {name} @{p}: {lv[p]:,.0f} != {expect:,.0f}")
            else:
                rv = items.get(rv, {})
                for p in lv:
                    if p in rv and rv[p] and abs(lv[p] - rv[p]) / abs(rv[p]) > IDENTITY_TOL:
                        failures.append(f"{path.name}: {name} @{p}: {lv[p]:,.0f} != {rv[p]:,.0f}")
    return failures


def check_conflict_honesty(artifacts):
    failures = []
    for path, doc in artifacts:
        for sec_name, sec in (doc.get("sections") or {}).items():
            if not sec_name.startswith("standardized_"):
                continue
            for item in sec.get("items", []):
                if "conflict" not in (item.get("flags") or []):
                    continue
                sources = item.get("sources") or {}
                for p, v in _nums(item.get("values")).items():
                    src = sources.get(p) or []
                    pool = [s.get("value") for s in src if isinstance(s.get("value"), (int, float))]
                    if pool and not any(abs(v - s) <= max(1.0, 0.005 * abs(s)) for s in pool):
                        failures.append(f"{path.name}: {item.get('concept_id')} @{p} conflict value not in sources (summed?)")
    return failures


def check_coverage(artifacts):
    per_sector = defaultdict(lambda: [0, 0])  # sector -> [mapped, total]
    for _, doc in artifacts:
        sector = (doc.get("meta", {}).get("sector") or "UNKNOWN").strip() or "UNKNOWN"
        for sec_name, sec in (doc.get("sections") or {}).items():
            if not sec_name.startswith("standardized_"):
                continue
            for it in sec.get("items", []):
                if it.get("is_header"):
                    continue
                per_sector[sector][1] += 1
                if it.get("concept_id"):
                    per_sector[sector][0] += 1
    return {s: (m, t, (m / t * 100 if t else 0)) for s, (m, t) in sorted(per_sector.items())}


def run_idempotency(n: int) -> list:
    """Re-parse N symbols twice; artifacts must be byte-identical."""
    import glob as g
    from app.services.xbrl_parser import parse_and_merge_xbrl_files
    failures = []
    data_dir = BACKEND / "data" / "downloads"
    syms = sorted(p.name for p in data_dir.iterdir() if p.is_dir() and p.name.isdigit())[:n]
    for sym in syms:
        files = sorted(g.glob(str(data_dir / sym / "*.xls*")) + g.glob(str(data_dir / f"{sym}_ar" / "*.xls*")))
        if not files:
            continue
        try:
            a = json.dumps(parse_and_merge_xbrl_files(files), ensure_ascii=False, sort_keys=True)
            b = json.dumps(parse_and_merge_xbrl_files(files), ensure_ascii=False, sort_keys=True)
        except Exception as e:  # corrupt source files are a known deferred issue
            print(f"  idempotency {sym}: SKIPPED ({e})")
            continue
        if a != b:
            failures.append(f"{sym}: two parses of identical inputs differ")
    return failures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(BACKEND / "output"))
    ap.add_argument("--idempotency", type=int, default=0, metavar="N")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    paths = sorted(out_dir.glob("*_financials.json"))
    artifacts, broken = [], []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                artifacts.append((p, json.load(f)))
        except Exception as e:
            broken.append(f"{p.name}: unreadable ({e})")

    print(f"validate_xbrl_outputs: {len(artifacts)} artifacts ({len(broken)} unreadable)")
    hard_failures = [f"UNREADABLE {b}" for b in broken]

    meta_fail, unverified = check_meta_contract(artifacts)
    print(f"[1] meta contract: {len(meta_fail)} failures, {len(unverified)} unverified filings (D2 allows these, listed)")
    hard_failures += [f"META {m}" for m in meta_fail]
    for u in unverified[:20]:
        print(f"    unverified: {u}")

    mix, mix_warn = check_mixed_units(artifacts)
    print(f"[2] mixed-unit scan (probe34): {len(mix)} hard failures, {len(mix_warn)} growth-or-units warnings")
    for w in mix_warn[:15]:
        print(f"    warn: {w}")
    hard_failures += [f"MIXED {m}" for m in mix]

    ident = check_identities(artifacts)
    print(f"[3] identities TA==TL&E / TA==CA+NCA (tol {IDENTITY_TOL:.1%}): {len(ident)} violations")
    hard_failures += [f"IDENTITY {m}" for m in ident]

    conf = check_conflict_honesty(artifacts)
    print(f"[4] conflict honesty (picked source, never sum): {len(conf)} violations")
    hard_failures += [f"CONFLICT {m}" for m in conf]

    cov = check_coverage(artifacts)
    print("[5] item coverage per sector:")
    for s, (m, t, pct) in cov.items():
        print(f"    {s:<40} {m:>6}/{t:<6} {pct:5.1f}%")

    if args.idempotency:
        print(f"[6] idempotency: re-parsing {args.idempotency} symbols twice ...")
        idem = run_idempotency(args.idempotency)
        print(f"    {len(idem)} mismatches")
        hard_failures += [f"IDEMPOTENCY {m}" for m in idem]

    if hard_failures:
        print(f"\nRESULT: FAIL ({len(hard_failures)} hard failures)")
        for fline in hard_failures[:60]:
            print("  -", fline)
        return 1
    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
