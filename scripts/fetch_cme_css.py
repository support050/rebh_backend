"""Resolve the QuikStrike CSS rules we need to reproduce the FedWatch look.

The tool ships its styling in external .less files served as plain CSS, so the
class colours (highlight / highlight2 / gridtable / vtab) are only obtainable by
fetching them. Read-only; dumps to output/rate_page_design/cme_css_rules.json.

Run: python scripts/fetch_cme_css.py
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(BACKEND, "output", "rate_page_design")
BASE = "https://cmegroup-tools.quikstrike.net"

SHEETS = [
    "/Styles/themes/v2/layout-light.less?79",
    "/Styles/default.less?79",
    "/Styles/themes/v2/theme-light.less?79",
    "/Styles/themes/v2/highchart-light.less?79",
    "/Styles/OptionMatrix.less",
    "/Styles/bantix/bantix-base.css",
]

# Selectors whose computed colour/geometry we must match exactly.
WANTED = [
    "highlight", "highlight2", "gridtable", "vtab", "qs-vtabs", "qs-tabs",
    "ui-tabs-selected", "ui-tabs-anchor", "ui-state-active", "ui-state-default",
    "number", "freeze-me", "subheader", "headerrow", "title-text",
    "content-container", "ui-widget-info",
]


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    from curl_cffi import requests as cc

    s = cc.Session(impersonate="chrome", timeout=60)
    rules_out = {}
    for path in SHEETS:
        url = BASE + path
        try:
            r = s.get(url, headers={"Referer": BASE + "/User/QuikStrikeView.aspx?viewitemid=IntegratedFedWatchTool"})
        except Exception as e:
            print(f"{path}: EXC {type(e).__name__}: {e}")
            continue
        body = r.text or ""
        print(f"{path}: {r.status_code} {len(body)} chars")
        if r.status_code != 200:
            continue
        # Flatten to "selector { decls }" pairs; strip comments first.
        clean = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        found = []
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", clean):
            sel = m.group(1).strip().replace("\n", " ")
            decl = re.sub(r"\s+", " ", m.group(2).strip())
            if any(w in sel.lower() for w in WANTED):
                found.append({"selector": sel[:150], "decls": decl[:400]})
        rules_out[path] = found
        print(f"   matched {len(found)} rules")

    fn = os.path.join(OUT, "cme_css_rules.json")
    with open(fn, "w", encoding="utf-8") as fh:
        json.dump(rules_out, fh, indent=2, ensure_ascii=False)
    print("\nwrote", fn)

    # Print the ones that decide the palette.
    key = ("highlight", "gridtable", "ui-tabs-selected", "ui-state-active", "ui-tabs-anchor")
    for path, rules in rules_out.items():
        for r in rules:
            if any(k in r["selector"].lower() for k in key):
                print(f"  {os.path.basename(path.split('?')[0]):24} {r['selector'][:70]:70} {{ {r['decls'][:170]} }}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
