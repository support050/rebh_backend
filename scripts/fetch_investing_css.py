"""Resolve the investing.com CSS rules needed to reproduce the Eurodollar grid.

The page ships its styling in external minified sheets, so the table colours
(bcHeader bar, grayArrowIcon, grayFont/green/red change cells, link blue) are
only obtainable by fetching them. Read-only; dumps to
output/rate_page_design/investing_css_rules.json.

Run: python scripts/fetch_investing_css.py
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(BACKEND, "output", "rate_page_design")

SHEETS = [
    "https://i-invdn-com.investing.com/css/mainOldMin_v3b.css",
    "https://i-invdn-com.investing.com/css/newMainCssMin_v80w.css",
    "https://i-invdn-com.investing.com/cross_rates/cross_rates_v4.css",
]

WANTED = [
    "gentbl", "crossratestbl", "barchartdatatable", "bcheader", "bccontainer",
    "grayarrowicon", "grayfont", "green", "red", "tableheader", "tablerow",
    "datatbl", "icon", "nowrap", "a:hover", "a {", ".tabTitle",
]


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    from curl_cffi import requests as cc

    s = cc.Session(impersonate="chrome", timeout=60)
    rules_out = {}
    for url in SHEETS:
        try:
            r = s.get(url, headers={"Referer": "https://www.investing.com/"})
        except Exception as e:
            print(f"{url}: EXC {type(e).__name__}: {e}")
            continue
        body = r.text or ""
        print(f"{os.path.basename(url)}: {r.status_code} {len(body):,} chars")
        if r.status_code != 200:
            continue
        clean = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        found = []
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", clean):
            sel = re.sub(r"\s+", " ", m.group(1).strip())
            decl = re.sub(r"\s+", " ", m.group(2).strip())
            low = sel.lower()
            if any(w in low for w in WANTED):
                found.append({"selector": sel[:160], "decls": decl[:300]})
        rules_out[url] = found
        print(f"   matched {len(found)} rules")

    fn = os.path.join(OUT, "investing_css_rules.json")
    with open(fn, "w", encoding="utf-8") as fh:
        json.dump(rules_out, fh, indent=2, ensure_ascii=False)
    print("\nwrote", fn)

    key = ("gentbl", "crossratestbl", "bcheader", "grayarrowicon", "grayfont",
           ".green", ".red", "tableheader", "tablerow")
    for url, rules in rules_out.items():
        for r in rules:
            if any(k in r["selector"].lower() for k in key):
                print(f"  {os.path.basename(url):28} {r['selector'][:60]:60} {{ {r['decls'][:150]} }}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
