"""
xbrl_parser.py  v3
==================
Changes vs v2:
  - equity_changes: full matrix parser (period × component)
  - filing_info: exposed as structured company_info dict
  - parse_number: handles string-formatted numbers with commas
"""
import argparse, json, os, re, sys
from pathlib import Path
import pandas as pd

try:
    from app.services.xbrl_mapping import STANDARD_TEMPLATE, resolve_mapping
except ImportError:
    from xbrl_mapping import STANDARD_TEMPLATE, resolve_mapping

UNMAPPED_HEADER_EN = "Other / Unmapped"
UNMAPPED_HEADER_AR = "أخرى / غير موحدة"

CANONICAL = {
    "100010": "filing_info",
    "200100": "auditors_report",
    "300100": "balance_sheet",
    "300200": "balance_sheet",
    "300300": "other_comprehensive_income",
    "300400": "income_statement",
    "300500": "equity_changes",
    "300600": "equity_changes",
    "300700": "cash_flow",
    "300800": "cash_flow",
}

SNAPSHOT_SECTIONS = {"balance_sheet", "filing_info", "auditors_report"}

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
    if "_" in key: s, e = key.split("_", 1); return (e, s)
    return (key, key)

def make_period_key(start, end, is_snapshot):
    if is_snapshot or not start: return end[:7]
    return f"{start[:7]}_{end[:7]}"

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
            if lbl == "Start Date": starts[ci] = v
            elif lbl == "End Date": ends[ci] = v
    periods = {}
    for ci in sorted(set(list(starts) + list(ends))):
        start = starts.get(ci, ""); end = ends.get(ci, "")
        if not end: continue
        label = make_period_key(start, end, is_snapshot)
        periods[ci] = {"start": start, "end": end, "label": label, "period_type": classify_period(label)}
    return periods

# ── Generic section parser ────────────────────────────────────────────────

def parse_section(df, start_row, end_row, canonical_key):
    is_snap = canonical_key in SNAPSHOT_SECTIONS
    periods = get_periods(df, start_row, end_row, is_snap)
    if not periods: return None
    period_meta = [{"key": p["label"], "start": p["start"], "end": p["end"], "period_type": p["period_type"]} for p in periods.values()]
    items = []
    for ri in range(start_row + 1, end_row):
        row = df.iloc[ri]; raw = str(row[0])
        if not raw or raw.strip() == "nan": continue
        if re.search(r"\[\d{6}\]", raw): break
        if clean_label(raw) in ("Start Date", "End Date"): continue
        if "[member]" in raw.lower(): continue
        label = clean_label(raw)
        if not label: continue
        values = {}
        for ci, pinfo in periods.items():
            if ci >= len(row): continue
            num = parse_number(row[ci])
            if num is not None: values[pinfo["label"]] = num
            else:
                txt = str(row[ci]).replace("\xa0", "").strip()
                if txt and txt not in ("nan", ""): values[pinfo["label"]] = txt
        has_num = any(isinstance(v, (int, float)) for v in values.values())
        is_hdr = is_header_row(raw) and not has_num
        if values or is_hdr:
            items.append({"label": label, "is_header": is_hdr, "values": values})
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
        if lbl == "Start Date": r_start = row
        elif lbl == "End Date": r_end = row
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
        if clean_label(raw) in ("Start Date", "End Date"): continue
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
        v1 = str(row[1]).replace("\xa0", "").strip() if len(row) > 1 else ""
        v1 = v1 if v1 not in ("nan", "") else ""
        if label == "Name of reporting entity" and v1: meta["company_name"] = v1
        elif "Company symbol" in label and v1:
            parts = v1.split("|"); meta["symbol"] = parts[0].strip()
            if len(parts) > 1: meta["isin"] = parts[1].strip()
        elif "Sector" in label and "Industry" in label and v1: meta["sector"] = v1
        elif label == "Reporting period end date" and v1 and "report_end" not in meta: meta["report_end"] = v1
        elif label == "Description of presentation currency" and v1: meta["currency"] = v1
        elif label == "Level of rounding used in financial statements" and v1: meta["rounding"] = v1
        elif label == "Status of financial statements" and v1: meta["status"] = v1
        if len(meta) >= 7: break
    return meta

# ── Single-file parser ────────────────────────────────────────────────────

def parse_xbrl_file(filepath):
    df = pd.read_excel(filepath, sheet_name=0, header=None, dtype=str)
    boundaries = get_section_boundaries(df)
    meta = extract_meta(df); meta["source_file"] = os.path.basename(filepath)
    sections = {}
    for i, (row_idx, code, title) in enumerate(boundaries[:-1]):
        next_row = boundaries[i + 1][0]
        canonical = CANONICAL.get(code)
        if not canonical: continue
        if code == "300200": canonical = "income_statement" if "income" in title.lower() else "balance_sheet"
        elif code == "300400": canonical = "cash_flow" if "cash" in title.lower() else "income_statement"

        # use dedicated equity parser
        if canonical == "equity_changes":
            parsed = parse_equity_changes(df, row_idx, next_row)
        else:
            parsed = parse_section(df, row_idx, next_row, canonical)

        if parsed and parsed["items"]:
            if canonical not in sections or len(parsed["items"]) > len(sections[canonical]["items"]):
                sections[canonical] = parsed
    return {"meta": meta, "sections": sections}

# ── Merger ────────────────────────────────────────────────────────────────

def merge_files(file_results):
    if not file_results: return {}
    file_results = sorted(file_results, key=lambda r: r["meta"].get("report_end", "0000-00-00"))
    merged_meta = file_results[-1]["meta"].copy()
    merged_meta.pop("source_file", None)
    merged_meta["source_files"] = [r["meta"].get("source_file", "") for r in file_results]
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
        for item in raw_sec["items"]:
            has_numeric = any(isinstance(v, (int, float)) for v in item["values"].values())
            if item.get("is_header") and not has_numeric:
                continue
            mapping = resolve_mapping(item["label"])
            if mapping:
                code, direction = mapping
                if code in std_items_map:
                    mapped_any = True
                    for p in periods:
                        raw_val = item["values"].get(p)
                        if isinstance(raw_val, (int, float)):
                            cur = std_items_map[code]["values"][p]
                            std_items_map[code]["values"][p] = (0.0 if cur is None else cur) + raw_val * direction
                else:
                    unmapped_items.append(_make_unmapped_item(item, periods))
            elif has_numeric or item["values"]:
                unmapped_items.append(_make_unmapped_item(item, periods))

        items_out = [
            {
                "label": it["label"],
                "label_ar": it["label_ar"],
                "is_header": it["is_header"],
                "values": it["values"],
            }
            for it in std_items_map.values()
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
        "values": values,
    }


def parse_and_merge_xbrl_files(filepaths):
    return merge_files([parse_xbrl_file(str(p)) for p in filepaths])


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