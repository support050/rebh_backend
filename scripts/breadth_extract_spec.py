# -*- coding: utf-8 -*-
"""breadth_extract_spec.py — PROMPT A0: panel-spec + TypeScript types extraction.

Reads the frozen client dashboard template + api_samples golden files and emits:
  * breadth-panel-spec.json  — per-page chart panel definitions (keys, zones,
    colors, thresholds, events, series) plus the theme palette and the panel
    field contract the Canvas `Stack` engine understands.
  * frontend/types/breadth.ts — interfaces for the 10 /api/rebh/breadth/*
    responses, inferred from api_samples (exact, not guessed) + a BreadthSeriesKey
    union of every series key.

Nothing here touches the engine or DB. It is a build-time codegen aid so the
frontend LLM prompt ships pixel-faithful specs without prior project context.

Usage: backend\\venv\\Scripts\\python.exe -X utf8 scripts/breadth_extract_spec.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
TEMPLATE = REPO / "Rebh_Market_Breadth_Package" / "dashboard" / "pro_dashboard_template.html"
SAMPLES = REPO / "Rebh_Market_Breadth_Package" / "api_samples"
OUT_SPEC = REPO / "breadth-panel-spec.json"
OUT_TYPES = REPO / "frontend" / "types" / "breadth.ts"


# ── balanced scanner that respects string/backtick literals ──────────────────
def match_close(s: str, i: int) -> int:
    """Given s[i] is an opener ({[(), return index of its matching closer."""
    pairs = {"{": "}", "[": "]", "(": ")"}
    openers, stack = set(pairs), []
    q = None
    while i < len(s):
        c = s[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if c == q:
                q = None
        elif c in "\"'`":
            q = c
        elif c in openers:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return i
        i += 1
    raise ValueError("unbalanced literal")


def split_top(s: str, sep: str = ",") -> list[str]:
    """Split on `sep` at bracket-depth 0, ignoring separators inside strings."""
    out, buf, depth, q = [], [], 0, None
    i = 0
    while i < len(s):
        c = s[i]
        if q:
            buf.append(c)
            if c == "\\":
                buf.append(s[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c in "\"'`":
            q = c
            buf.append(c)
        elif c in "{[(":
            depth += 1
            buf.append(c)
        elif c in ")]}":
            depth -= 1
            buf.append(c)
        elif c == sep and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


NUM = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$")


def parse_value(raw: str):
    """Convert a JS literal fragment into JSON-friendly python; flag expressions."""
    v = raw.strip()
    if not v:
        return None
    if v == "true":
        return True
    if v == "false":
        return False
    if v == "null":
        return None
    if NUM.match(v):
        return float(v) if ("." in v or "e" in v or "E" in v) else int(v)
    if v[0] == "{":
        return parse_object(v[1:v.rindex("}")])
    if v[0] == "[":
        inner = v[1:v.rindex("]")]
        return [parse_value(p) for p in split_top(inner) if p.strip()]
    if v[0] in "\"'":
        return v.strip("\"'")
    if v[0] == "`":
        return {"__expr__": v.strip("`")}
    # identifier / member access / function call -> dynamic runtime value
    return {"__expr__": v}


def parse_object(inner: str) -> dict:
    out = {}
    for part in split_top(inner):
        part = part.strip()
        if not part:
            continue
        # split at first top-level ':'
        ci = _find_colon(part)
        if ci < 0:
            continue
        key = part[:ci].strip().strip("\"'")
        out[key] = parse_value(part[ci + 1:])
    return out


def _find_colon(part: str) -> int:
    depth, q = 0, None
    for i, c in enumerate(part):
        if q:
            if c == q:
                q = None
            continue
        if c in "\"'`":
            q = c
        elif c in "{[(":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ":" and depth == 0:
            return i
    return -1


# ── template extraction ──────────────────────────────────────────────────────
def extract_css_vars(txt: str) -> dict:
    def grab(pat):
        m = re.search(pat, txt, re.S)
        if not m:
            return {}
        block = m.group(1)
        return {k.strip(): v.strip() for k, v in
                (p.split(":", 1) for p in split_top(block, ";") if ":" in p)}
    return {
        "light": grab(r":root\{(.*?)\}"),
        "dark": grab(r':root\[data-theme="dark"\]\{(.*?)\}'),
    }


def extract_maps(txt: str):
    reg = parse_object(_call_arg(txt, "const REG="))
    icon = parse_object(_call_arg(txt, "const REGICON="))
    return reg, icon


def _call_arg(txt: str, decl: str) -> str:
    i = txt.index(decl) + len(decl)
    j = txt.index("{", i)
    k = match_close(txt, j)
    return txt[j + 1:k]


def extract_pages(txt: str) -> list:
    m = re.search(r"const PAGES=\[(.*?)\];", txt, re.S)
    rows = []
    for part in split_top(m.group(1)):
        arr = parse_value(part)
        if isinstance(arr, list) and len(arr) >= 2:
            rows.append({"id": arr[0], "name": arr[1]})
    return rows


IDX_BASE = {
    "title": "TASI (candles when zoomed in; line otherwise)",
    "height": 260,
    "series": [
        {"key": "tasi", "type": "candle", "color": "--text", "label": "TASI"},
        {"key": "tasi", "type": "line", "color": "--blue", "label": "TASI close", "width": 1.2},
    ],
    "regimeStrip": True,
}


def parse_panels_array(txt: str, start: int) -> tuple[str, list]:
    """start points at the '(' of new Stack(...). Returns (selector, panels)."""
    j = txt.index(",", start)  # selector, then panels array
    selector = txt[start + 1:j].strip()
    k = txt.index("[", j)
    arr_end = match_close(txt, k)
    inner = txt[k + 1:arr_end]
    panels = []
    for part in split_top(inner):
        part = part.strip()
        if not part:
            continue
        if part.startswith("IDX("):
            if "{" in part:
                ob = part.index("{")
                oe = match_close(part, ob)
                override = parse_object(part[ob + 1:oe])
            else:
                override = {}              # IDX() with no overrides
            merged = dict(IDX_BASE)
            merged.update(override)
            merged["_via"] = "IDX"
            panels.append(merged)
        elif part[0] == "{":
            panels.append(parse_value(part))
        else:
            panels.append({"__expr__": part})   # dynamic/var-driven panel def
    return selector, panels


def extract_stacks(txt: str) -> list:
    out = []
    for m in re.finditer(r"new Stack\(", txt):
        before = txt[:m.start()]
        container = re.split(r"[\r\n]", before.strip())[-1].strip()
        sel, panels = parse_panels_array(txt, m.end() - 1)
        # best-effort page attribution: nearest preceding p-<id> selector
        pg = re.findall(r"#p-([a-z]+)", txt[:m.start()])
        out.append({"selector": sel, "page": pg[-1] if pg else None,
                    "assign": (container if "=" in container.split("new Stack")[0] else None),
                    "panels": panels})
    return out


# ── series key inventory ──────────────────────────────────────────────────────
def series_keys(txt: str):
    """Return (api_keys, extra_refs): the 125 real /series keys and any
    additional keys the template references but computes in JS (widget %, dv_*,
    bpi_state_num, tasi_* overlay) — kept out of the API-facing union."""
    sfile = json.loads((SAMPLES / "series.json").read_text(encoding="utf-8"))
    api = set(sfile["series"])
    refs = set(re.findall(r"\bS\.([a-zA-Z_][a-zA-Z0-9_]*)\b", txt))
    refs |= set(re.findall(r"key:\s*'([a-zA-Z0-9_]+)'", txt))
    extra = sorted(refs - api)
    return sorted(api), extra


# ── TypeScript generation from api_samples ────────────────────────────────────
def load(name):
    return json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def is_record(d: dict) -> bool:
    if len(d) > 12:
        return True
    return any(not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", k) for k in d)


def infer(value, name, lines, top=False):
    if isinstance(value, dict):
        if not value:
            return "Record<string, unknown>"
        if not top and is_record(value):
            vt = infer(next(iter(value.values())), name + "Value", lines)
            return f"Record<string, {vt}>"
        if name not in [l.split(" ")[1] for l in lines if l.startswith("export interface")]:
            fields = []
            for k, v in value.items():
                fields.append((k, infer(v, _pascal(name) + _pascal(k), lines)))
            body = "\n".join(f"  {k}: {t};" for k, t in fields)
            lines.append(f"export interface {_pascal(name)} {{\n{body}\n}}\n")
        return _pascal(name)
    if isinstance(value, list):
        if not value:
            return "unknown[]"
        seen = {infer(v, name + "Item", lines) for v in value[:20]}
        t = " | ".join(sorted(seen)) if len(seen) > 1 else next(iter(seen))
        return f"{t}[]" if t.count("|") == 0 else f"({t})[]"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return "string"


def _pascal(s: str) -> str:
    parts = re.split(r"[^a-zA-Z0-9]+", s)
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or "T"


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def gen_types(keys: list, extra: list) -> str:
    lines: list[str] = []
    lines.append("// AUTO-GENERATED by scripts/breadth_extract_spec.py — do not edit by hand.")
    lines.append("// Shapes inferred from Rebh_Market_Breadth_Package/api_samples (client golden files).")
    lines.append("// Each interface matches one /api/rebh/breadth/* response exactly.\n")
    lines.append("export type BreadthSeriesKey =\n" +
                 "\n  | ".join([f"'{k}'" for k in keys]) + ";")
    if extra:
        lines.append("// Series the reference dashboard computes in-browser (NOT in /series):")
        lines.append("export type BreadthDerivedKey =\n" +
                     "\n  | ".join([f"'{k}'" for k in extra]) + ";")
    lines.append("\nexport type Regime = 'Strong' | 'Healthy' | 'Neutral' | 'Weak' | 'Washout';")
    lines.append("export type SeriesPoint = number | string | null;\n")

    # endpoint responses — top level forced to named interfaces
    endpoint_files = [
        ("today", "BreadthToday", load("today")),
        ("events", "BreadthEvents", load("events")),
        ("signals", "BreadthSignals", load("signals")),
        ("sectors", "BreadthSectors", load("sectors")),
        ("stocks", "BreadthStocks", load("stocks")),
        ("stockbee", "BreadthStockbee", load("stockbee_names")),
        ("meta", "BreadthMeta", load("meta")),
    ]
    for _, iface, data in endpoint_files:
        if isinstance(data, dict) and data and all(_IDENT.match(k) for k in data):
            infer(data, iface, lines, top=True)
        else:                                  # top level is a genuine map (dates/symbols)
            vt = infer(next(iter(data.values())), iface + "Value", lines) if data else "unknown"
            lines.append(f"export type {iface} = Record<string, {vt}>;\n")
    # series block — exact hand-typed contract (values are points, can be null/str)
    lines.append("export interface BreadthSeriesResponse {")
    lines.append("  dates: string[];")
    lines.append("  series: Record<BreadthSeriesKey, SeriesPoint[]>;")
    lines.append("}\n")
    # ohlc block
    oh = load("ohlc")
    infer(oh, "BreadthOhlc", lines, top=True)

    # dedupe interfaces by name
    out, seen = [], set()
    for ln in lines:
        mm = re.match(r"export interface (\w+)", ln)
        if mm:
            if mm.group(1) in seen:
                continue
            seen.add(mm.group(1))
        out.append(ln)
    return "\n".join(out) + "\n"


def main() -> int:
    txt = TEMPLATE.read_text(encoding="utf-8")
    reg, icon = extract_maps(txt)
    api_keys, extra_keys = series_keys(txt)
    spec = {
        "_meta": {
            "source_template": str(TEMPLATE.relative_to(REPO)),
            "engine": "Canvas 2D `class Stack` — MUST be ported as-is (D6: no Recharts/Chart.js)",
            "panel_field_contract": {
                "title": "string — panel header",
                "height": "number px (default 150)",
                "mini": "bool — compact margins, no header",
                "series": "SeriesDef[] — drawn in order",
                "ylim": "[lo,hi] — fixed y range (else auto-padded)",
                "zero": "bool — include y=0 in range",
                "rebase": "bool — index series to 100 at window start",
                "zones": "[{from,to,color}] — shaded y bands (CSS var name)",
                "hlines": "[{y,color?,label?,dash?}] — horizontal reference lines",
                "events": "[{key,color,pos?('top'),dot?}] — DATA.events[key] markers",
                "episodes": "bool — shade DATA.events.canary bands",
                "regimeStrip": "bool — 5px regime-colored strip at panel bottom",
            },
            "series_def_fields": {
                "key": "BreadthSeriesKey (indexes DATA.series[key])",
                "color": "CSS var name (e.g. --blue)",
                "label": "legend text (falls back to key)",
                "type": "'line'|'area'|'hist'|'bar'|'candle' (default line)",
                "width": "line width px (default 1.5)",
                "dec": "decimal places in tooltip",
                "up": "hist/bar positive color (default --up)",
                "down": "hist/bar negative color (default --down)",
                "colorBy": "series key driving bar sign color (>0 up else down)",
            },
        },
        "theme": extract_css_vars(txt),
        "regime_colors": reg,
        "regime_icons": icon,
        "pages": extract_pages(txt),
        "stacks": extract_stacks(txt),
        "series_keys": api_keys,
        "derived_series_keys": extra_keys,
    }
    OUT_SPEC.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    OUT_TYPES.write_text(gen_types(api_keys, extra_keys), encoding="utf-8")
    print(f"[spec ] wrote {OUT_SPEC.relative_to(REPO)}")
    print(f"        pages={len(spec['pages'])} stacks={len(spec['stacks'])} "
          f"api_series={len(api_keys)} derived={len(extra_keys)}")
    print(f"[types] wrote {OUT_TYPES.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
