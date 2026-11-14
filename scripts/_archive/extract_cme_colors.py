"""Focused colour probe for the CME FedWatch tool.

Pulls the exact palette for the three things the general design script could not
anchor on: the meeting-date tab strip, the yellow "NOW" column of the target-rate
table, and the yellow/cyan diagonal of the conditional probabilities matrix.

Read-only. Run:  python scripts/extract_cme_colors.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(BACKEND, "output", "rate_page_design")
# The cmegroup.com wrapper page sits behind Akamai and starts returning
# net::ERR_HTTP2_PROTOCOL_ERROR once navigated too often. The tool itself is
# served by QuikStrike, which is the same DOM we actually need to measure.
CME_URL = "https://cmegroup-tools.quikstrike.net/User/QuikStrikeView.aspx?viewitemid=IntegratedFedWatchTool"

sys.path.insert(0, HERE)
from extract_rate_pages_design import resolve_chrome  # noqa: E402

JS_TABS = r"""
() => {
  const vis = el => { const c = getComputedStyle(el); const r = el.getBoundingClientRect();
    return c.display !== 'none' && c.visibility !== 'hidden' && r.width > 0 && r.height > 0; };
  const pick = e => { const c = getComputedStyle(e); const r = e.getBoundingClientRect();
    return { tag: e.tagName.toLowerCase(), cls: (e.className||'').toString().slice(0,90),
             text: (e.textContent||'').replace(/\s+/g,' ').trim().slice(0,30),
             rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
             color: c.color, bg: c.backgroundColor, fontSize: c.fontSize, fontWeight: c.fontWeight,
             borderTop: c.border-top-width + ' ' + c.border-top-color,
             borderBottom: c.border-bottom-width + ' ' + c.border-bottom-color,
             borderLeft: c.border-left-width + ' ' + c.border-left-color,
             borderRight: c.border-right-width + ' ' + c.border-right-color,
             radius: c.border-top-left-radius, padding: c.padding-top + ' ' + c.padding-left,
             cursor: c.cursor }; };

  // Meeting-date tabs: short labels like "9 Dec26". Anchor on the /\w+ \w{3}\d\d/
  // text pattern rather than a class, because the class is jQuery-UI generic.
  const anchors = Array.from(document.querySelectorAll('a,li,span,td'))
    .filter(e => vis(e) && /^[0-9]{1,2} [A-Z][a-z]{2}\d{0,2}$/.test((e.textContent||'').trim()));
  const tabs = anchors.slice(0, 20).map(pick);

  // Yellow NOW column + the whole target-rate table, cell by cell.
  const tables = Array.from(document.querySelectorAll('table')).filter(vis);
  const dump = [];
  for (const t of tables) {
    const txt = (t.textContent || '');
    if (!/Target Rate \(bps\)|PROBABILITY|Conditional Meeting|Fed Fund Futures/i.test(txt)) continue;
    const rows = Array.from(t.querySelectorAll('tr')).slice(0, 6).map(tr =>
      Array.from(tr.querySelectorAll('th,td')).map(c => {
        const cs = getComputedStyle(c);
        return { t: (c.textContent||'').replace(/\s+/g,' ').trim().slice(0,22),
                 bg: cs.backgroundColor, color: cs.color, fw: cs.fontWeight, fs: cs.fontSize,
                 ta: cs.textAlign, bd: cs.borderBottomWidth + ' ' + cs.borderBottomColor };
      }));
    dump.push({ cls: (t.className||'').toString().slice(0,90), rows });
  }
  return { tabs, tables: dump, diag: {
      bodyLen: (document.body && document.body.innerText || '').length,
      bodyHead: (document.body && document.body.innerText || '').replace(/\s+/g,' ').trim().slice(0, 220),
      tables: tables.length,
      anchors: Array.from(document.querySelectorAll('a,li,span,td')).length,
      tabish: Array.from(document.querySelectorAll('a,li,span,td'))
        .map(e => (e.textContent||'').trim()).filter(t => /^[0-9]{1,2} [A-Z][a-z]{2}\d{0,2}$/.test(t)).slice(0, 14),
  } };
}
"""


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, executable_path=resolve_chrome())
        ctx = b.new_context(viewport={"width": 1600, "height": 1100}, locale="en-US")
        page = ctx.new_page()
        # CME intermittently drops the first navigation with net::ERR_HTTP2_PROTOCOL_ERROR.
        for attempt in range(4):
            try:
                page.goto(CME_URL, wait_until="domcontentloaded", timeout=90000)
                break
            except Exception as e:
                print(f"  goto attempt {attempt + 1} failed: {type(e).__name__}")
                page.wait_for_timeout(4000)
                if attempt == 3:
                    raise
        try:
            page.click("#onetrust-accept-btn-handler", timeout=4000)
        except Exception:
            pass
        page.wait_for_timeout(12000)
        fr = next((f for f in page.frames if "quikstrike" in (f.url or "").lower()), None)
        if fr is None:
            # Navigated straight to QuikStrike: the tool *is* the top frame.
            fr = page.main_frame
        if "quikstrike" not in (fr.url or "").lower():
            print("NO QUIKSTRIKE FRAME; got", fr.url[:90])
            return 1
        out["current"] = fr.evaluate(JS_TABS)
        print("  current diag:", json.dumps(out["current"]["diag"])[:400])
        # Switch to the Probabilities view and dump the conditional matrix colours.
        fr.evaluate(r"""() => {
          const a = Array.from(document.querySelectorAll('a'))
            .find(e => (e.textContent || '').trim() === 'Probabilities');
          if (a) a.click();
        }""")
        page.wait_for_timeout(12000)
        out["probabilities"] = fr.evaluate(JS_TABS)
        b.close()

    fn = os.path.join(OUT, "cme_colors.json")
    with open(fn, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print("wrote", fn)

    for view in ("current", "probabilities"):
        v = out[view]
        print(f"\n########## {view} ##########")
        print("  tabs:", len(v["tabs"]))
        for t in v["tabs"][:12]:
            print(f"    {t['text']!r:14} {t['tag']:4} bg={t['bg']} color={t['color']} "
                  f"fw={t['fontWeight']} fs={t['fontSize']} border={t['borderTop']} r={t['radius']}")
        for tbl in v["tables"]:
            print(f"\n  TABLE cls={tbl['cls']!r}")
            for row in tbl["rows"][:5]:
                print("    " + " | ".join(
                    f"{c['t']}[{c['bg'].replace('rgb', '').replace('(', '').replace(')', '')}]"
                    for c in row[:10]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
