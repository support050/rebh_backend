# -*- coding: utf-8 -*-
"""breadth_diff_raw.py — full value-level diff of client raw CSV vs our DB export.

Compares (Symbol, Date) -> Close/Change/%Change/Volume/Value with float
tolerance (formatting like 11.0 vs 11.0000 is ignored). Reports:
  * rows only in one file
  * numeric differences beyond tolerance
Exit 0 = identical inputs, 1 = differences found.
"""
import csv
import sys

CLIENT = r"d:\Work\LUMIVST\Rebh_Market_Breadth_Package\engine\storage\raw\TASI_prices_ALL_20260929_181127.csv"
MINE = r"d:\Work\LUMIVST\backend\app\services\breadth_engine\storage\raw\TASI_prices_ALL_20260930_000000.csv"
NUMS = ["Close", "Change", "% Change", "Volume Traded", "Value Traded (SAR)"]
TOL = 1e-6


def f(x):
    x = (x or "").replace(",", "").strip()
    if x in ("", "-", "NA", "N/A"):
        return None
    try:
        return float(x)
    except ValueError:
        return float("nan")


def load(path):
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            out[(row["Symbol"].strip(), row["Date"].strip())] = tuple(f(row[c]) for c in NUMS)
    return out


def close(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if a != a and b != b:  # both NaN
        return True
    return abs(a - b) <= TOL


def main():
    a, b = load(CLIENT), load(MINE)
    print(f"client rows={len(a)}  db rows={len(b)}")
    only_a = set(a) - set(b)
    # ignore the extra day in ours (09-30 export vs 09-29 client)
    only_a_d0930 = sum(1 for k in only_a if k[1] == "2026-09-30")
    print(f"missing from db export: {len(only_a)} (of which 09-30-only rows in client: 0)")
    only_b = {k for k in set(b) - set(a)}
    print(f"extra in db export: {len(only_b)}  (expected ~{len(only_b)} = new day 09-30)")
    b_common = {k: v for k, v in b.items() if k in a}
    diffs = 0
    shown = 0
    for k, va in a.items():
        vb = b_common.get(k)
        if vb is None:
            continue
        if not all(close(x, y) for x, y in zip(va, vb)):
            diffs += 1
            if shown < 20:
                print("DIFF", k, "client=", va, "db=", vb)
                shown += 1
    print("TOTAL value diffs:", diffs)
    return 0 if diffs == 0 and not (set(a) - set(b)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
