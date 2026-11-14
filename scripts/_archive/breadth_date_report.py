# -*- coding: utf-8 -*-
"""breadth_date_report.py — acceptance/parity CLI (PROMPT B, read-only).

Answers: "what values did the breadth engine produce for date D?" and diffs them against the
client's reference api_samples/series.json (and today.json when D is its date).

Usage (run with the ENGINE venv python — it has pandas+pyarrow):
  .venv-of-engine/Scripts/python.exe backend/scripts/breadth_date_report.py \
      --dates 2026-09-29,2020-03-16 [--source both|parquet|samples] [--format table|json|csv] \
      [--out FILE] [--tol 0.01] [--engine-root PATH]

Exit codes: 0 all within tolerance | 1 any |Δ| > tol | 2 date/coverage error.
"""
import argparse
import io
import json
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
DEFAULT_ENGINE = REPO / "Rebh_Market_Breadth_Package" / "engine"

FEATURE_TABLES = ["participation", "advance_decline", "thrusts", "momentum", "sectors_summary",
                  "concentration", "divergences", "widget", "stages_weekly", "classic", "bpi",
                  "composite", "composite_percentiles"]


def load_parquet_values(engine_root: Path) -> dict:
    """key -> {date_str: float|None} across all feature tables (later tables win on dup keys? no: first wins, dups reported)."""
    out, dups = {}, []
    feat = engine_root / "storage" / "features"
    for name in FEATURE_TABLES:
        f = feat / f"{name}.parquet"
        if not f.exists():
            continue
        df = pd.read_parquet(f)
        if "date" not in df.columns:
            df = df.reset_index().rename(columns={df.index.name or "index": "date"})
        df["date"] = df["date"].astype(str).str[:10]
        for col in df.columns:
            if col == "date":
                continue
            if col in out:
                dups.append(col)
                continue
            s = df.set_index("date")[col]
            vals = {}
            for d_, v in s.items():
                if pd.isna(v):
                    vals[d_] = None
                elif isinstance(v, (int, float)) or hasattr(v, "item"):  # numeric (incl. numpy)
                    vals[d_] = float(v)
                else:
                    vals[d_] = str(v)  # categorical: regime, driver, bpi_status...
            out[col] = vals
    if dups:
        print(f"[note] duplicate series keys across tables (kept first): {sorted(set(dups))[:10]}", file=sys.stderr)
    return out


def load_samples_series(engine_root: Path, samples_root: Path | None = None) -> tuple:
    cands = [samples_root] if samples_root else []
    cands += [engine_root.parent / "api_samples", REPO / "Rebh_Market_Breadth_Package" / "api_samples"]
    p = next((c / "series.json" for c in cands if (c / "series.json").exists()), None)
    if not p:
        raise SystemExit("api_samples/series.json not found — pass --samples-root")
    d = json.loads(p.read_text(encoding="utf-8"))
    return d["dates"], d["series"]


def load_data_from_html(engine_root: Path) -> tuple:
    """Extract the embedded `const DATA = {...}` from the generated breadth_dashboard.html.
    This is the DISPLAY layer — exactly what api_samples/ is split from (same rounding/units)."""
    import re
    html = (engine_root / "storage" / "reports" / "breadth_dashboard.html").read_text(encoding="utf-8")
    m = re.search(r"const DATA=(\{.*?\});</script>", html, re.S) or re.search(r"const DATA=(\{.*?\});", html, re.S)
    if not m:
        raise SystemExit("cannot locate const DATA in generated dashboard")
    d = json.loads(m.group(1))
    return d["dates"], d["series"]


def resolve_date(dates, want):
    if want in dates:
        return want
    prev = [x for x in dates if x <= want]
    if not prev:
        return None
    got = max(prev)
    print(f"[note] {want} is not a trading day -> using previous {got}", file=sys.stderr)
    return got


def fmt(v):
    if v is None or v == "missing":
        return "null"
    if isinstance(v, str):
        return v
    return f"{v:.4f}".rstrip("0").rstrip(".")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", required=True, help="comma separated YYYY-MM-DD")
    ap.add_argument("--source", choices=["both", "parquet", "samples", "data"], default="data",
                    help="data = generated dashboard DATA vs api_samples (display-layer, exact); "
                         "both = raw parquet vs samples (expect display-rounding deltas)")
    ap.add_argument("--format", choices=["table", "json", "csv"], default="table")
    ap.add_argument("--out")
    ap.add_argument("--tol", type=float, default=0.01)
    ap.add_argument("--engine-root", default=str(DEFAULT_ENGINE))
    ap.add_argument("--samples-root", default=None)
    a = ap.parse_args()

    eng = Path(a.engine_root)
    if a.source in ("both", "parquet"):
        pq = load_parquet_values(eng)
    elif a.source == "data":
        ddates, ds = load_data_from_html(eng)
        pq = {k: {ddates[i]: v for i, v in enumerate(arr) if v is not None}
              for k, arr in ds.items() if isinstance(arr, list)}
    else:
        pq = {}
    sdates, samples = load_samples_series(eng, Path(a.samples_root) if a.samples_root else None)
    # resolution pool = samples dates (canonical trading calendar)
    dates = sdates if sdates else sorted({d for v in pq.values() for d in v})
    # compare ONLY contract keys (the sample's own keys) in display modes
    key_scope = set(samples) if (a.source == "data" and samples) else (set(pq) | set(samples))

    rows, fails = [], 0
    for want in [x.strip() for x in a.dates.split(",") if x.strip()]:
        d = resolve_date(dates, want)
        if d is None:
            print(f"ERROR: {want} before data coverage", file=sys.stderr)
            sys.exit(2)
        keys = sorted(key_scope)
        for k in keys:
            pv = pq.get(k, {}).get(d, "missing") if pq else None
            sv = samples.get(k, [None] * len(dates))[dates.index(d)] if samples else None
            if a.source == "data":
                # display layer: values pre-rounded by the same module that made api_samples
                same = (pv == sv) or (pv in (None, "missing") and sv is None)
                delta, verdict = (0.0 if same else None), ("OK" if same else "FAIL")
            elif a.source == "both":
                if isinstance(pv, str) or isinstance(sv, str):
                    delta, verdict = (0.0 if str(pv) == str(sv) else None), ("OK" if str(pv) == str(sv) else "FAIL")
                elif pv == "missing" or pv is None or sv is None:
                    delta = 0.0 if (pv in (None, "missing") and sv is None) else None
                    verdict = "OK(null)" if delta == 0.0 else "N/A"  # one-side-absent = coverage, not mismatch
                else:
                    delta = abs(pv - sv)
                    verdict = "OK" if delta <= a.tol else "FAIL"
            else:
                delta, verdict = 0.0, "OK"
            if verdict == "FAIL":
                fails += 1
            rows.append({"date": d, "key": k, "parquet": pv, "samples": sv,
                         "delta": None if delta is None else round(delta, 6), "verdict": verdict})

    buf = io.StringIO()
    if a.format == "json":
        json.dump(rows, buf, ensure_ascii=False, indent=1, default=str)
    elif a.format == "csv":
        buf.write("date,key,parquet,samples,delta,verdict\n")
        for r in rows:
            buf.write(f"{r['date']},{r['key']},{r['parquet']},{r['samples']},{r['delta']},{r['verdict']}\n")
    else:
        by_date = {}
        for r in rows:
            by_date.setdefault(r["date"], []).append(r)
        for d, rs in sorted(by_date.items()):
            buf.write(f"\n=== {d} — {len(rs)} series ===\n")
            bad = [r for r in rs if r["verdict"] == "FAIL"]
            na = [r for r in rs if r["verdict"] == "N/A"]
            buf.write(f"OK: {len(rs) - len(bad) - len(na)} | FAIL: {len(bad)} | N/A: {len(na)}\n")
            for r in bad[:40]:
                buf.write(f"  FAIL {r['key']:<28} ours={fmt(r['parquet'])} client={fmt(r['samples'])} Δ={r['delta']}\n")
            for r in na[:10]:
                buf.write(f"  N/A  {r['key']:<28} ours={fmt(r['parquet'])} client={fmt(r['samples'])}\n")
        text = buf.getvalue()
        print(text)
    out = buf.getvalue() if a.format != "table" else ""
    if a.out and out:
        Path(a.out).write_text(out, encoding="utf-8")
        print(f"written: {a.out}", file=sys.stderr)

    print(f"TOTAL: {len(rows)} checks, {fails} FAIL (tol={a.tol})", file=sys.stderr)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
