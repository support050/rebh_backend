"""
xbrl_parser.py  v3
==================
Changes vs v2:
  - equity_changes: full matrix parser (period × component)
  - filing_info: exposed as structured company_info dict
  - parse_number: handles string-formatted numbers with commas
"""
import argparse, json, os, re, statistics, sys
from collections import Counter
from pathlib import Path
import pandas as pd

try:
    from app.services.xbrl_mapping import STANDARD_TEMPLATE, resolve_mapping
    from app.services import xbrl_catalog
except ImportError:
    from xbrl_mapping import STANDARD_TEMPLATE, resolve_mapping
    import xbrl_catalog

UNMAPPED_HEADER_EN = "Other / Unmapped"
UNMAPPED_HEADER_AR = "أخرى / غير موحدة"

CANONICAL = {
    # "100010": "filing_info",
    # "200100": "auditors_report",
    "200110": "balance_sheet",
    "300100": "balance_sheet",
    "300110": "income_statement",
    "300200": "balance_sheet",
    "300300": "income_statement",
    "300400": "income_statement",
    "300500": "other_comprehensive_income",
    "300600": "equity_changes",
    "300700": "cash_flow",
    "300800": "cash_flow",
}

SNAPSHOT_SECTIONS = {"balance_sheet", "filing_info", "auditors_report"}

# Date labels in both English and Arabic variants
START_DATE_LABELS = {"Start Date", "بداية الفترة", "تاريخ بداية الفترة للتقرير"}
END_DATE_LABELS = {"End Date", "نهاية الفترة", "تاريخ نهاية الفترة للتقرير"}
DATE_LABELS = START_DATE_LABELS | END_DATE_LABELS

# ── Declared-scale normalization (Milestone D / F2, F3) ───────────────────────
# Every filing declares its own rounding (Actuals / Thousands / Millions, in
# English and Arabic). Monetary values are converted to canonical actual SAR
# AT PARSE TIME, per file, BEFORE merge, so a company whose history mixes
# Thousands and Actuals periods is normalized period-by-period — each value
# carries the factor of the filing it came from. This replaces the post-hoc
# market-cap anchor guesser (the 1211 "Actuals x1000 -> 117 trillion" failure).
# An unknown / missing token => factor 1 with an "unverified" basis; we NEVER
# assume a factor we did not read from the filing metadata.
# 'rounding' / 'التقريب' (modern filings) plus 'بواقع' — the bare unit header used
# by the 2015-2016 AR corpus (e.g. 2260_ar: ['بواقع', 'ألف']).
ROUNDING_LABEL_MARKS = ("rounding", "\u0627\u0644\u062a\u0642\u0631\u064a\u0628", "\u0628\u0648\u0627\u0642\u0639")

# Tokens that mark a line as non-monetary (per-share / share-count / ratio). These
# are never scaled — they are already SAR-per-share regardless of the bulk scale.
NON_MONETARY_TOKENS = (
    "per share", "per saham", "\u0631\u0628\u062d\u064a\u0629 \u0627\u0644\u0633\u0647\u0645",  # ربحية السهم
    "\u0627\u0644\u0633\u0647\u0645",  # السهم
    "\u0644\u0643\u0644 \u0633\u0647\u0645",  # لكل سهم
    "number of shares", "shares outstanding", "weighted average",
    "\u0639\u062f\u062f \u0627\u0644\u0627\u0633\u0647\u0645",  # عدد الاسهم
    "\u0639\u062f\u062f \u0627\u0644\u0623\u0633\u0647\u0645",  # عدد الأسهم
    "dividend per",
)


def _is_non_monetary(label):
    low = (label or "").lower()
    return any(tok in low for tok in NON_MONETARY_TOKENS)


def declared_scale_factor(token):
    """Map a filing's declared rounding token to an actual-SAR multiplier."""
    t = (token or "").strip().lower()
    if not t or t == "nan":
        return None
    # Actuals FIRST: the AR phrase '\u0627\u0644\u0623\u0631\u0642\u0627\u0645 \u0627\u0644\u0641\u0639\u0644\u064a\u0629' (actual numbers) contains the
    # letters of '\u0623\u0644\u0641'/'\u0627\u0644\u0641' inside '\u0627\u0644\u0641\u0639\u0644\u064a\u0629', so a naive thousands substring
    # would mis-scale \u00d71000 a filing that declares actual numbers.
    if "actual" in t or "\u0641\u0639\u0644\u064a" in t:  # '\u0641\u0639\u0644\u064a' occurs only in the actuals phrase
        return 1.0
    if "million" in t or "\u0645\u0644\u064a\u0648\u0646" in t or "\u0645\u0644\u0627\u064a\u064a\u0646" in t:  # \u0645\u0644\u064a\u0648\u0646 / \u0645\u0644\u0627\u064a\u064a\u0646
        return 1e6
    if "thousand" in t:
        return 1e3
    # Arabic thousands must match as whole words (\u0622\u0644\u0627\u0641 / \u0623\u0644\u0641, or bare \u0627\u0644\u0641 as its own
    # cell): never as a substring, or '\u0627\u0644\u0641\u0639\u0644\u064a\u0629'-style words leak in (2310 mixed-unit bug).
    for w in re.split(r"\s+", t):
        if "\u0622\u0644\u0627\u0641" in w or "\u0623\u0644\u0641" in w or w == "\u0627\u0644\u0641":
            return 1e3
    return None


def apply_declared_scale(parsed, factor):
    """Multiply every monetary numeric in a single-file parse result by `factor`."""
    if not factor or factor == 1.0:
        return parsed
    for sec in parsed.get("sections", {}).values():
        for item in sec.get("items", []):
            if _is_non_monetary(item.get("label")):
                continue
            vals = item.get("values")
            if not isinstance(vals, dict):
                continue
            for p, v in list(vals.items()):
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    vals[p] = v * factor
                elif isinstance(v, dict):
                    for comp, cv in list(v.items()):
                        if isinstance(cv, bool):
                            continue
                        if isinstance(cv, (int, float)):
                            v[comp] = cv * factor
    return parsed


def _shared_scale_ratios(sec, other_sec):
    """anchor/scaled value ratios for identical (label, period) monetary facts
    shared by two filings — restatements of the prior period make this common."""
    ratios = []
    o_items = {}
    for it in other_sec.get("items", []):
        o_items.setdefault((it.get("label") or "").strip().lower(), it)
    for it in sec.get("items", []):
        lab = (it.get("label") or "").strip().lower()
        o = o_items.get(lab)
        if o is None or _is_non_monetary(lab):
            continue
        for p, v in (it.get("values") or {}).items():
            ov = (o.get("values") or {}).get(p)
            if isinstance(v, bool) or isinstance(ov, bool):
                continue
            if isinstance(v, (int, float)) and isinstance(ov, (int, float)) and v:
                ratios.append(ov / v)
    return ratios


def reconcile_declared_scales(results):
    """D2 hardening: a filing's declared rounding can be WRONG at the source —
    e.g. 2310 ships 'Actuals' while reusing thousands-magnitude numbers, which
    the mixed-unit scan (probe34) caught. Each company's filings restate the
    prior period, so minority-factor files are cross-checked against majority
    files on shared (label, period) facts:
      median ratio ~ majority/minority factor -> the minority DECLARATION is
        wrong (raw numbers agree with the majority); correct it and stamp
        scale_basis='restatement_corrected'.
      median ratio ~ 1                       -> declarations genuinely agree;
        stamp scale_basis='declared_verified'.
      otherwise / no overlap                 -> keep the declaration untouched
        (honest 'declared'); the validation harness still flags it.
    This never guesses in isolation — it only acts on direct contradictions
    inside the same company's own restatements.
    """
    counts = Counter((r["meta"].get("scale_factor") or 1.0) for r in results)
    if len(counts) < 2:
        return results
    majority = counts.most_common(1)[0][0]
    for r in results:
        m = r["meta"]
        f = m.get("scale_factor") or 1.0
        if f == majority:
            continue
        g = majority
        G = g / f
        ratios = []
        for other in results:
            if other is r or (other["meta"].get("scale_factor") or 1.0) != g:
                continue
            for sec_name, sec in r.get("sections", {}).items():
                osec = other["sections"].get(sec_name)
                if osec:
                    ratios += _shared_scale_ratios(sec, osec)
        if len(ratios) < 3:
            continue
        med = statistics.median(ratios)
        if 0.7 * G <= med <= 1.4 * G:
            apply_declared_scale(r, G)
            m["declared_factor"] = f
            m["scale_factor"] = g
            m["scale_basis"] = "restatement_corrected"
        elif 0.85 <= med <= 1.15:
            m["scale_basis"] = "declared_verified"
    return results

# ── Helpers ───────────────────────────────────────────────────────────────

def clean_label(v):
    v = v.replace("\xa0", " ").strip()
    v = re.sub(r"\s*\[abstract\]", "", v, flags=re.I)
    v = re.sub(r"\s*\[line items\]", "", v, flags=re.I)
    return v.strip()

def is_header_row(raw):
    return "[abstract]" in raw.lower() or "[line items]" in raw.lower()

def parse_number(v):
    """Handle both '1234567.0' and '1,234,567' formats."""
    v = str(v).replace("\xa0", "").replace(",", "").strip()
    if v in ("", "nan", "-", "\xa0", "None"): return None
    try: return float(v)
    except: return None

def classify_period(key):
    if "_" not in key: return "snapshot"
    s, e = key.split("_", 1)
    try:
        sm = pd.Timestamp(s + "-01"); em = pd.Timestamp(e + "-01")
        months = (em.year - sm.year) * 12 + (em.month - sm.month) + 1
    except: return "unknown"
    if months <= 3:  return "Q"
    if months <= 6:  return "H1"
    if months <= 9:  return "9M"
    return "FY"

def period_sort_key(key):
    """Chronological rank for a period key (fixes XBRL report F5).

    Both snapshot keys ('YYYY-MM') and duration keys ('YYYY-MM_YYYY-MM') are
    ranked by ``(end_date, span_months)`` ascending, so the *last* element of a
    sorted list is always the latest period. When two periods share the same
    end date, the one with the longer span (e.g. the annual FY) ranks last and
    therefore wins the ``latest`` slot over a YTD/quarter window ending the
    same day. Unparseable keys sort to the very end.
    """
    if "_" in key:
        s, e = key.split("_", 1)
    else:
        s = e = key
    try:
        start_ts = pd.Timestamp(s + "-01")
        end_ts = pd.Timestamp(e + "-01")
        span_months = (end_ts.year - start_ts.year) * 12 + (end_ts.month - start_ts.month)
    except Exception:
        return (pd.Timestamp.max, 0)
    return (end_ts, span_months)

def sort_period_keys(keys):
    """Return period keys in chronological order (earliest -> latest)."""
    return sorted(keys, key=period_sort_key)

def make_period_key(start, end, is_snapshot):
    if is_snapshot or not start: return end[:7]
    return f"{start[:7]}_{end[:7]}"

def classify_canonical_section(code: str, title: str) -> str:
    """Classify section canonical key based on title semantics and block code."""
    tl = (title or "").lower().strip()
    if any(k in tl for k in ["cash flow", "cash flows", "التدفقات النقدية", "التدفقات"]):
        return "cash_flow"
    if any(k in tl for k in ["changes in equity", "equity", "حقوق الملكية", "التغيرات في حقوق"]):
        return "equity_changes"
    if any(k in tl for k in ["comprehensive", "الشامل"]):
        return "other_comprehensive_income"
    if any(k in tl for k in ["financial position", "balance sheet", "المركز المالي", "الميزانية"]):
        return "balance_sheet"
    if any(k in tl for k in ["income", "profit", "loss", "operations", "الدخل", "الأرباح والخسائر", "الارباح والخسائر"]):
        return "income_statement"
    return CANONICAL.get(code)

# ── File structure ────────────────────────────────────────────────────────

def get_section_boundaries(df):
    b = []
    for i, row in df.iterrows():
        m = re.search(r"\[(\d{6})\]", str(row[0]))
        if m: b.append((i, m.group(1), str(row[0]).strip()))
    b.append((len(df), "END", ""))
    return b

def get_periods(df, start_row, end_row, is_snapshot):
    starts, ends = {}, {}
    for ri in range(start_row + 1, min(start_row + 8, end_row)):
        row = df.iloc[ri]; lbl = clean_label(str(row[0]))
        for ci in range(1, 10):
            v = str(row[ci]).strip() if ci < len(row) else ""
            if not v or v in ("nan", "", "Note No.", "Note no."): continue
            try: pd.Timestamp(v)
            except: continue
            if lbl in START_DATE_LABELS: starts[ci] = v
            elif lbl in END_DATE_LABELS: ends[ci] = v
    periods = {}
    for ci in sorted(set(list(starts) + list(ends))):
        start = starts.get(ci, ""); end = ends.get(ci, "")
        if not end: continue
        label = make_period_key(start, end, is_snapshot)
        periods[ci] = {"start": start, "end": end, "label": label, "period_type": classify_period(label)}
    return periods

# ── Generic section parser ────────────────────────────────────────────────

def parse_kv_section(df, start_row, end_row):
    items = []
    for ri in range(start_row + 1, end_row):
        row = df.iloc[ri]; raw = str(row[0])
        label = clean_label(raw)
        if not label or label == "nan": continue
        if re.search(r"\[\d{6}\]", raw): break
        
        # Take the first non-empty value in the row
        val = None
        for ci in range(1, len(row)):
            txt = str(row[ci]).replace("\xa0", "").strip()
            if txt and txt not in ("nan", "", "None"):
                val = txt
                break
                
        if val is not None or is_header_row(raw):
            items.append({"label": label, "is_header": is_header_row(raw) and val is None, "values": {"current": val}})
        
    if not items: return None
    return {
        "period_meta": [{"key": "current", "start": "", "end": "", "period_type": "snapshot"}],
        "periods": ["current"],
        "items": items,
        "section_type": "kv_section"
    }

def parse_section(df, start_row, end_row, canonical_key):
    is_snap = canonical_key in SNAPSHOT_SECTIONS
    periods = get_periods(df, start_row, end_row, is_snap)
    if not periods: 
        return parse_kv_section(df, start_row, end_row)
    period_meta = [{"key": p["label"], "start": p["start"], "end": p["end"], "period_type": p["period_type"]} for p in periods.values()]
    items = []
    for ri in range(start_row + 1, end_row):
        row = df.iloc[ri]; raw = str(row[0])
        if re.search(r"\[\d{6}\]", raw): break
        # F7 recovery: label-shifted rows carry the item label in column B while
        # col A is empty; the numeric values stay in the *same* detected period
        # columns (only the label column is excluded). See XBRL report §7 / F7.
        label_col = "A"
        if not raw or raw.strip() == "nan":
            shifted = str(row[1]) if len(row) > 1 else ""
            if not shifted or shifted.strip() == "nan": continue
            if parse_number(shifted) is not None: continue      # col B is numeric, not a label
            if clean_label(shifted) in DATE_LABELS: continue
            if "[member]" in shifted.lower(): continue
            label = clean_label(shifted)
            if not label: continue
            label_col = "B"
        else:
            if clean_label(raw) in DATE_LABELS: continue
            if "[member]" in raw.lower(): continue
            label = clean_label(raw)
            if not label: continue
        values = {}
        for ci, pinfo in periods.items():
            if label_col == "B" and ci == 1: continue           # col B holds the label, not a value
            if ci >= len(row): continue
            num = parse_number(row[ci])
            if num is not None: values[pinfo["label"]] = num
            else:
                txt = str(row[ci]).replace("\xa0", "").strip()
                if txt and txt not in ("nan", ""): values[pinfo["label"]] = txt
        has_num = any(isinstance(v, (int, float)) for v in values.values())
        is_hdr = is_header_row(raw if label_col == "A" else str(row[1])) and not has_num
        if values or is_hdr:
            item = {"label": label, "is_header": is_hdr, "values": values}
            if label_col == "B": item["label_col"] = "B"
            items.append(item)
    return {"period_meta": period_meta, "periods": [p["key"] for p in period_meta], "items": items}

# ── Equity Changes matrix parser ──────────────────────────────────────────

def parse_equity_changes(df, start_row, end_row):
    """
    Equity table is a 2-D matrix: rows = line items, cols = component × period.
    Member labels appear at odd columns (1,3,5,...).
    Each member owns two adjacent columns: current period and prior period.

    Output values shape:
      item["values"] = {
          "2025-04_2025-12": {"Share capital": 315000000, "Retained earnings": 408849015, ...},
          "2024-04_2024-12": {"Share capital": 315000000, ...},
      }
    """
    # find member row
    member_row_idx = None
    for ri in range(start_row + 1, min(start_row + 6, end_row)):
        row = df.iloc[ri]
        if any("[member]" in str(row[ci]).lower() for ci in range(1, len(row))):
            member_row_idx = ri
            break
    if member_row_idx is None:
        return parse_section(df, start_row, end_row, "equity_changes")  # fallback

    r_members = df.iloc[member_row_idx]
    r_start = r_end = None
    for ri in range(member_row_idx + 1, min(member_row_idx + 5, end_row)):
        row = df.iloc[ri]; lbl = clean_label(str(row[0]))
        if lbl in START_DATE_LABELS: r_start = row
        elif lbl in END_DATE_LABELS: r_end = row
    if r_end is None:
        return parse_section(df, start_row, end_row, "equity_changes")

    n_cols = len(r_members)

    # build col_map: {ci: {member, period_key}}
    period_meta_map = {}
    col_map = {}
    last_start, last_end = "", ""
    for ci in range(1, n_cols):
        end_str = str(r_end[ci]).strip() if ci < n_cols else ""
        start_str = str(r_start[ci]).strip() if r_start is not None and ci < n_cols else ""
        
        # Forward-fill merged cells
        if end_str in ("nan", "", " ") and last_end:
            end_str = last_end
            start_str = last_start
        else:
            try: 
                pd.Timestamp(end_str)
                last_end, last_start = end_str, start_str
            except: 
                continue

        period_key = make_period_key(start_str, end_str, is_snapshot=False)
        if period_key not in period_meta_map:
            period_meta_map[period_key] = {"key": period_key, "start": start_str, "end": end_str, "period_type": classify_period(period_key)}

        # nearest member at or before ci
        member_name = None
        for mc in range(ci, 0, -1):
            mv = str(r_members[mc]).strip() if mc < n_cols else ""
            if "[member]" in mv.lower():
                member_name = re.sub(r"\s*\[member\]", "", mv, flags=re.I).strip()
                break
        col_map[ci] = {"member": member_name or f"col_{ci}", "period_key": period_key}

    if not col_map: return None

    # unique ordered periods and components
    seen_periods_set = set(); seen_periods = []
    seen_comp_set = set(); seen_components = []
    for ci, info in col_map.items():
        if info["period_key"] not in seen_periods_set:
            seen_periods.append(info["period_key"]); seen_periods_set.add(info["period_key"])
        if info["member"] not in seen_comp_set:
            seen_components.append(info["member"]); seen_comp_set.add(info["member"])

    sorted_periods = sorted(seen_periods, key=period_sort_key)

    # parse items
    items = []
    data_start = member_row_idx + 3
    for ri in range(data_start, end_row):
        row = df.iloc[ri]; raw = str(row[0])
        if not raw or raw.strip() == "nan": continue
        if re.search(r"\[\d{6}\]", raw): break
        if clean_label(raw) in DATE_LABELS: continue
        if "[member]" in raw.lower(): continue
        label = clean_label(raw)
        if not label: continue
        header = is_header_row(raw)
        values = {}
        for ci, info in col_map.items():
            if ci >= len(row): continue
            num = parse_number(row[ci])
            if num is None: continue
            pk = info["period_key"]; mb = info["member"]
            if pk not in values: values[pk] = {}
            values[pk][mb] = num
        if values or header:
            items.append({"label": label, "is_header": header, "values": values})

    return {
        "period_meta": [period_meta_map[k] for k in sorted_periods if k in period_meta_map],
        "periods": sorted_periods,
        "components": seen_components,
        "items": items,
        "section_type": "equity_matrix",
    }

# ── Meta extractor ────────────────────────────────────────────────────────

def extract_meta(df):
    meta = {}
    for _, row in df.iterrows():
        label = clean_label(str(row[0]))
        # AR filings sometimes leave the value column B empty and carry the value
        # in column C (e.g. 4001_ar rounding row: ['مستوى التقريب...', '', 'بالآلاف']).
        # Take the first non-empty cell across cols 1-3 instead of assuming col B.
        v1 = ""
        for ci in (1, 2, 3):
            if ci >= len(row):
                break
            txt = str(row[ci]).replace("\xa0", "").strip()
            if txt and txt not in ("nan", "", "None"):
                v1 = txt
                break
        if label == "Name of reporting entity" and v1: meta["company_name"] = v1
        elif "Company symbol" in label and v1:
            parts = v1.split("|"); meta["symbol"] = parts[0].strip()
            if len(parts) > 1: meta["isin"] = parts[1].strip()
        elif "Sector" in label and "Industry" in label and v1: meta["sector"] = v1
        elif label == "Reporting period end date" and v1 and "report_end" not in meta: meta["report_end"] = v1
        elif label == "Description of presentation currency" and v1: meta["currency"] = v1
        elif v1 and any(m in label.lower() for m in ROUNDING_LABEL_MARKS): meta["rounding"] = v1
        elif label == "Status of financial statements" and v1: meta["status"] = v1
        # Metadata lives in the leading 1xxxxx block; stop at the first statement
        # block ([2-9]xxxxx). The old `len(meta) >= 7` break stopped before the
        # rounding row when status/isin pushed the count to 7 early, silently
        # skipping the declared scale (regression seen on Almarai 4001 Thousands).
        if re.search(r"\[[2-9]\d{5}\]", str(row[0])): break
    return meta

# ── Single-file parser ────────────────────────────────────────────────────

def parse_xbrl_file(filepath):
    df = pd.read_excel(filepath, sheet_name=0, header=None, dtype=str)
    boundaries = get_section_boundaries(df)
    meta = extract_meta(df); meta["source_file"] = os.path.basename(filepath)
    sections = {}
    for i, (row_idx, code, title) in enumerate(boundaries[:-1]):
        next_row = boundaries[i + 1][0]
        canonical = classify_canonical_section(code, title)
        if not canonical: continue

        # use dedicated equity parser
        if canonical == "equity_changes":
            parsed = parse_equity_changes(df, row_idx, next_row)
        else:
            parsed = parse_section(df, row_idx, next_row, canonical)

        if parsed and parsed["items"]:
            if canonical not in sections:
                sections[canonical] = parsed
            else:
                # Merge periods and items from multiple sections belonging to the same canonical statement
                existing = sections[canonical]
                existing_keys = {pm["key"] for pm in existing.get("period_meta", [])}
                for pm in parsed.get("period_meta", []):
                    if pm.get("key") not in existing_keys:
                        existing["period_meta"].append(pm)
                        existing_keys.add(pm["key"])
                existing["periods"] = [pm["key"] for pm in existing["period_meta"]]
                existing["items"].extend(parsed["items"])

    # D1/D2: scale THIS filing's monetary values to canonical actual SAR using
    # the factor it declares. Unknown / missing token => factor 1 + 'unverified'.
    result = {"meta": meta, "sections": sections}
    declared = meta.get("rounding")
    factor = declared_scale_factor(declared)
    if factor is None:
        factor, basis = 1.0, "unverified"
    else:
        basis = "declared"
    apply_declared_scale(result, factor)
    meta["declared_rounding"] = declared
    meta["scale_factor"] = factor
    meta["scale_basis"] = basis
    return result

# ── Merger ────────────────────────────────────────────────────────────────

def merge_files(file_results):
    if not file_results: return {}
    file_results = sorted(file_results, key=lambda r: r["meta"].get("report_end", "0000-00-00"))
    merged_meta = file_results[-1]["meta"].copy()
    merged_meta.pop("source_file", None)
    merged_meta["source_files"] = [r["meta"].get("source_file", "") for r in file_results]
    # D3 meta contract: values are already canonical actual SAR (scaled per filing
    # at parse time). Stamp the flag the valuation engines check so they skip
    # their live market-cap anchor detector and never re-scale this file again.
    source_scales = []
    for r in file_results:
        m = r["meta"]
        source_scales.append({
            "file": m.get("source_file", ""),
            "declared": m.get("declared_rounding"),
            "factor": m.get("scale_factor", 1.0),
            "basis": m.get("scale_basis", "unverified"),
            "declared_factor": m.get("declared_factor"),
        })
    for k in ("declared_rounding", "scale_factor", "scale_basis"):
        merged_meta.pop(k, None)
    merged_meta["rounding"] = "normalized_sar"
    merged_meta["scale_basis"] = "declared_per_filing"
    merged_meta["source_scales"] = source_scales
    all_keys = set()
    for fr in file_results: all_keys.update(fr["sections"].keys())
    merged_sections = {}
    for sec_key in all_keys:
        item_reg = {}; pm_reg = {}; comp_set = []
        is_matrix = False
        for fr in file_results:
            sec = fr["sections"].get(sec_key)
            if not sec: continue
            if sec.get("section_type") == "equity_matrix": is_matrix = True
            for pm in sec.get("period_meta", []):
                pm_reg[pm["key"]] = pm
            # merge components list (equity matrix)
            for c in sec.get("components", []):
                if c not in comp_set: comp_set.append(c)
            for item in sec["items"]:
                lbl = item["label"]
                if lbl not in item_reg:
                    item_reg[lbl] = {"is_header": item["is_header"], "values": {}}
                if not item["is_header"]:
                    item_reg[lbl]["is_header"] = False
                for pk, pv in item["values"].items():
                    existing = item_reg[lbl]["values"].get(pk)
                    if isinstance(pv, dict):
                        if isinstance(existing, dict):
                            existing.update(pv)
                        else:
                            item_reg[lbl]["values"][pk] = dict(pv)
                    else:
                        if not isinstance(existing, dict):
                            item_reg[lbl]["values"][pk] = pv

        for _lbl, v in item_reg.items():
            vals = v["values"]
            has_num = any(isinstance(x, (int, float)) for x in vals.values()) or any(
                isinstance(x, (int, float))
                for d in vals.values() if isinstance(d, dict)
                for x in d.values()
            )
            if has_num:
                v["is_header"] = False

        sorted_keys = sorted(pm_reg.keys(), key=period_sort_key)
        sec_out = {
            "period_meta": [pm_reg[k] for k in sorted_keys],
            "periods": sorted_keys,
            "items": [{"label": l, "is_header": v["is_header"], "values": v["values"]} for l, v in item_reg.items()],
        }
        if is_matrix:
            sec_out["section_type"] = "equity_matrix"
            sec_out["components"] = comp_set
        merged_sections[sec_key] = sec_out

    # Load the concept registry once so resolve_mapping is catalog-driven.
    xbrl_catalog.load()
    sector = merged_meta.get("sector")

    # --- NORMALIZATION: template + Other/Unmapped ---
    std_sections = {
        "income_statement": "standardized_income_statement",
        "balance_sheet": "standardized_balance_sheet",
        "cash_flow": "standardized_cash_flow",
    }
    for raw_key, std_key in std_sections.items():
        raw_sec = merged_sections.get(raw_key)
        if not raw_sec:
            continue
        periods = raw_sec["periods"]
        period_meta = raw_sec["period_meta"]
        std_items_map = {}
        # Milestone C: the concept registry is the source of the standardized
        # rows. The 65 template codes keep their ids; new sector aggregates
        # (CONCEPT.*) are emitted as additional rows keyed by their own id.
        if xbrl_catalog.CONCEPTS:
            for cid in xbrl_catalog.concept_ids_for_statement(raw_key):
                c = xbrl_catalog.CONCEPTS[cid]
                std_items_map[cid] = {
                    "label": c.get("label_en"),
                    "label_ar": c.get("label_ar"),
                    "is_header": bool(c.get("is_subtotal")),
                    "values": {p: None for p in periods},
                }
        else:
            for code, info in STANDARD_TEMPLATE.items():
                if info["statement"] == raw_key:
                    std_items_map[code] = {
                        "label": info["line_en"],
                        "label_ar": info["line_ar"],
                        "is_header": info["is_subtotal"],
                        "values": {p: None for p in periods},
                    }
        unmapped_items = []
        mapped_any = False
        # Collect signed contributions first so identical-value synonyms are not double-counted
        # Track (value, label) tuples to enable parent/sub-item dedup
        pending: dict[str, dict[str, list[tuple[float, str]]]] = {code: {p: [] for p in periods} for code in std_items_map}
        for item in raw_sec["items"]:
            has_numeric = any(isinstance(v, (int, float)) for v in item["values"].values())
            if item.get("is_header") and not has_numeric:
                continue
            mapping = resolve_mapping(item["label"], statement=raw_key, sector=sector)
            if mapping:
                code, direction, tier = mapping
                if code in std_items_map:
                    mapped_any = True
                    lbl_norm = xbrl_catalog.normalize_label(item["label"])
                    for p in periods:
                        raw_val = item["values"].get(p)
                        if isinstance(raw_val, (int, float)):
                            # XBRL report R2: never silently drop large EPS. Keep the
                            # value and flag the item for review instead of `continue`.
                            if code == "IS-160" and abs(raw_val) > 500:
                                flags = std_items_map[code].setdefault("flags", [])
                                if "out_of_range" not in flags:
                                    flags.append("out_of_range")
                            pending[code][p].append((raw_val * direction, lbl_norm))
                else:
                    unmapped_items.append(_make_unmapped_item(item, periods))
            elif has_numeric or item["values"]:
                unmapped_items.append(_make_unmapped_item(item, periods))

        # Labels that represent a parent/aggregate total and should take precedence
        # over sub-item labels when both map to the same standardized code.
        _PARENT_LABELS = {
            'total revenue', 'revenue', 'total revenues',
            'total income', 'net revenue', 'net revenues',
            'gross premiums written',
            'insurance revenue',
            'general and administrative expenses',
            'general and administration expenses',
        }

        # C4 aggregation policy — replaces the old blanket `sum(vals)` fallback
        # (F1 double-counting). Every total/subtotal concept defaults to ONE_OF
        # (pick ONE source, never sum). Only concepts explicitly marked SUMMABLE
        # may be summed. Conflicts flag the item and keep an auditable source.
        for code, by_period in pending.items():
            agg = xbrl_catalog.aggregation_of(code) if xbrl_catalog.CONCEPTS else "ONE_OF"
            parents = xbrl_catalog.parent_labels_of(code) if xbrl_catalog.CONCEPTS else {l.lower().strip() for l in _PARENT_LABELS}
            canonical = xbrl_catalog.canonical_norm_label(code) if xbrl_catalog.CONCEPTS else ""
            for p, entries in by_period.items():
                if not entries:
                    continue
                vals = [e[0] for e in entries]
                if agg == "SUMMABLE":
                    std_items_map[code]["values"][p] = sum(vals)
                    continue
                if len(vals) == 1:
                    std_items_map[code]["values"][p] = vals[0]
                    continue
                abs_set = {abs(v) for v in vals}
                if len(abs_set) == 1:
                    # Synonym overlap: several raw lines, same amount -> take one.
                    std_items_map[code]["values"][p] = vals[0]
                    continue
                # Genuine conflict across differing values. NEVER sum. Choose one
                # source deterministically and stamp the item for review.
                flags = std_items_map[code].setdefault("flags", [])
                if "conflict" not in flags:
                    flags.append("conflict")
                std_items_map[code].setdefault("sources", {})[p] = [
                    {"label": l, "value": v} for v, l in entries
                ]
                pick = None
                if canonical:                                   # prefer exact canonical label
                    exact = [v for v, l in entries if l == canonical]
                    if exact:
                        pick = exact[0]
                if pick is None and agg == "PARENT_CHILD":      # else the registered aggregate
                    pc = [v for v, l in entries if l in parents]
                    if pc:
                        pick = max(pc, key=abs)
                if pick is None:                                # else the largest magnitude
                    pick = max(vals, key=abs)
                std_items_map[code]["values"][p] = pick

        # NOTE (XBRL report F6): the former "auto-repair double-counted Total Assets"
        # block was dead code — it gated on a non-existent BS-300 and mis-labelled
        # BS-100 (Trade & Other Payables) as TA, so it never executed and never
        # repaired anything. It has been removed. Balance-sheet identity checks now
        # live in scripts/validate_xbrl_outputs.py as *flag-only* validation (never
        # mutate), and double-counting is prevented upstream by the per-concept
        # aggregation policy below.

        items_out = [
            {
                "concept_id": cid,
                "label": it["label"],
                "label_ar": it["label_ar"],
                "is_header": it["is_header"],
                "values": it["values"],
                **({"flags": it["flags"]} if it.get("flags") else {}),
                **({"sources": it["sources"]} if it.get("sources") else {}),
            }
            for cid, it in std_items_map.items()
        ]
        if unmapped_items:
            items_out.append({
                "label": UNMAPPED_HEADER_EN,
                "label_ar": UNMAPPED_HEADER_AR,
                "is_header": True,
                "is_unmapped": True,
                "values": {p: None for p in periods},
            })
            items_out.extend(unmapped_items)
        if mapped_any or unmapped_items:
            merged_sections[std_key] = {
                "period_meta": period_meta,
                "periods": periods,
                "items": items_out,
            }

    return {"meta": merged_meta, "sections": merged_sections}


def _make_unmapped_item(item, periods):
    values = {}
    for p in periods:
        v = item["values"].get(p)
        if isinstance(v, (int, float)):
            values[p] = v
        elif v is not None and not isinstance(v, dict):
            values[p] = v
        else:
            values[p] = None
    return {
        "label": item["label"],
        "label_ar": item.get("label_ar"),
        "is_header": False,
        "is_unmapped": True,
        "status": "unresolved",
        "values": values,
    }


def parse_and_merge_xbrl_files(filepaths):
    results = []
    for p in filepaths:
        try:
            results.append(parse_xbrl_file(str(p)))
        except Exception as e:
            # ASCII-only: an emoji here crashed the whole company under cp1252
            # redirected stdout ('charmap' codec can't encode position 2-3).
            print(f"  WARN: Skipping corrupted file {Path(p).name}: {e}")
    reconcile_declared_scales(results)
    return merge_files(results)


# ── CLI ───────────────────────────────────────────────────────────────────

def main_PLACEHOLDER():
    pass
    parser = argparse.ArgumentParser()
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--folder"); g.add_argument("--files", nargs="+")
    parser.add_argument("--output", default="."); parser.add_argument("--symbol")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    xls_files = sorted(Path(args.folder).glob("*.xls")) + sorted(Path(args.folder).glob("*.XLS")) if args.folder else args.files
    if not xls_files: sys.exit("No .xls files found")
    print(f"Parsing {len(xls_files)} file(s)…")
    results = []
    for fp in xls_files:
        print(f"  → {Path(fp).name}")
        try: results.append(parse_xbrl_file(str(fp)))
        except Exception as e: print(f"    WARN: {e}", file=sys.stderr)
    merged = merge_files(results)
    symbol = re.sub(r"[^\w\-]", "_", args.symbol or merged.get("meta", {}).get("symbol", "company"))
    os.makedirs(args.output, exist_ok=True)
    out = os.path.join(args.output, f"{symbol}_financials.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2 if args.pretty else None)
    print(f"\n✓ {out}")
    for sk, sv in merged["sections"].items():
        types = {}
        for pm in sv.get("period_meta", []): t = pm["period_type"]; types[t] = types.get(t, 0) + 1
        matrix = " [MATRIX]" if sv.get("section_type") == "equity_matrix" else ""
        comps = f" | {len(sv.get('components',[]))} components" if sv.get("components") else ""
        print(f"  {sk:32s}: {len(sv['periods']):2d} periods  {types}{comps}{matrix}")

if __name__ == "__main__": main()