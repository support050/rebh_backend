"""Measured design extraction for the two rate pages.

Mirrors scripts/extract_fred_design.py: drives a real browser, pulls
getComputedStyle + getBoundingClientRect for every region we must replicate,
and writes JSON + screenshots under output/rate_page_design/.

Targets
  1. CME FedWatch  -> cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html
                      (tool itself lives in a QuikStrike iframe)
  2. Eurodollar    -> investing.com/rates-bonds/eurodollar-futures

Read-only against the DB. Run:
  python scripts/extract_rate_pages_design.py
"""

import json
import os
import sys
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
OUT_DIR = os.path.join(BACKEND, "output", "rate_page_design")

CME_URL = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
INV_URL = "https://www.investing.com/rates-bonds/eurodollar-futures"

_CHROME_ENV_KEYS = ("PLAYWRIGHT_CHROME", "CHROME_PATH")
_CHROME_DEFAULTS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def resolve_chrome() -> Optional[str]:
    for key in _CHROME_ENV_KEYS:
        p = os.getenv(key)
        if p and os.path.exists(p):
            return p
    for d in _CHROME_DEFAULTS:
        if os.path.exists(d):
            return d
    return None


STYLE_PROPS = [
    "font-family", "font-size", "font-weight", "line-height", "letter-spacing",
    "text-align", "text-transform", "font-style", "vertical-align",
    "color", "background-color", "background-image",
    "width", "height",
    "padding-top", "padding-right", "padding-bottom", "padding-left",
    "margin-top", "margin-right", "margin-bottom", "margin-left",
    "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
    "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
    "border-top-style", "border-bottom-style",
    "border-top-left-radius", "border-top-right-radius",
    "border-bottom-left-radius", "border-bottom-right-radius",
    "box-shadow", "display", "gap", "opacity", "cursor", "white-space",
    # SVG chart paints with fill/stroke, not background-color
    "fill", "stroke", "stroke-width",
]

# Named regions: (selectors, text anchor). Selector list is tried first so a
# rename on the source site degrades to the text anchor rather than to nothing.
CME_REGIONS = {
    "tool_root":            [["#qs-tool", ".quikstrike"], None],
    "meeting_info_card":    [["table.gridtable"], "Meeting Information"],
    "meeting_info_header":  [["th"], "Meeting Date"],
    "probabilities_card":   [[], "Ease"],
    "target_rate_table":    [[], "Target Rate (bps)"],
    "chart_title":          [[".highcharts-title", "text.highcharts-title"], "Target Rate Probabilities"],
    "chart_subtitle":       [[".highcharts-subtitle", "text.highcharts-subtitle"], "Current target rate"],
    "chart_bar":            [[".highcharts-series path", ".highcharts-point", ".highcharts-series rect"], None],
    "chart_data_label":     [[".highcharts-data-label text", "text.highcharts-data-label"], None],
    "chart_axis_title":     [[".highcharts-yaxis-title", ".highcharts-xaxis-title"], "Probability"],
    "chart_gridline":       [[".highcharts-yaxis-grid path", ".highcharts-grid-line"], None],
    "meeting_tabs":         [["ul.vtab", ".tabs", "ul.ui-tabs-nav"], None],
    "meeting_tab":          [["ul.vtab li a", ".ui-tabs-anchor", "li.tab a"], None],
    "meeting_tab_active":   [["ul.vtab li.ui-tabs-selected a", ".ui-tabs-active a", "li.tab.active a"], None],
    "now_column_cell":      [["td"], "(Current)"],
    "sidebar_link":         [["a"], "Compare"],
}

# Second QuikStrike view: the "Probabilities" tree node (screenshot #2).
PROB_REGIONS = {
    "futures_card":     [[], "Fed Fund Futures"],
    "conditional_card": [[], "Conditional Meeting Probabilities"],
    "conditional_cell": [["td"], None],
}

# The grid markup is stable (measured 2026-11-01 in _tmp_cme_dumps/inv_cffi.html):
# table#BarchartDataTable.genTbl.closedTbl.crossRatesTbl, first td holds
# span.grayArrowIcon, the month cell is td.bold.left.noWrap > a, the change cell
# is td.grayFont.bold. Selectors are written against that shape so the link blue
# and the header bar get measured instead of guessed.
INV_REGIONS = {
    "page_title":       [["h1", ".text-black-1"], "Eurodollar Futures"],
    "table_wrapper":    [["table#BarchartDataTable", "table"], "Last"],
    "table_head_cell":  [["#BarchartDataTable th"], "Month"],
    "table_body_cell":  [["#BarchartDataTable tbody td"], None],
    "first_data_row":   [["#BarchartDataTable tbody tr"], "Dec"],
    "header_bar":       [[".bcHeader", ".bcPageTitle"], "Eurodollar Contracts"],
    "month_link":       [["#BarchartDataTable tbody td.left a"], None],
    "chart_link":       [["#BarchartDataTable tbody td:last-child a"], None],
    "change_cell":      [["#BarchartDataTable tbody td.grayFont"], None],
    "expand_arrow":     [["span.grayArrowIcon", "[data-testid]", ".expand"], None],
    "info_icon":        [[".icon-info", "[aria-label*='info' i]"], None],
    "positive_cell":    [[".greenFlag", ".greenFont", ".up"], None],
    "negative_cell":    [[".redFlag", ".redFont", ".down"], None],
}

JS = r"""
async (cfg) => {
  const PROPS = cfg.props;
  const out = { regions: {}, notes: [] };

  function isVisible(el) {
    if (!el) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function stylesOf(el) {
    const cs = getComputedStyle(el);
    const o = {};
    for (const p of PROPS) o[p] = cs.getPropertyValue(p);
    return o;
  }
  function rectOf(el) {
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.x), y: Math.round(r.y),
             width: Math.round(r.width), height: Math.round(r.height) };
  }
  function smallestWithText(anchor) {
    const all = Array.from(document.querySelectorAll('div,section,article,aside,table,span,h1,h2,h3'));
    const hits = all.filter(e => isVisible(e) && (e.textContent || '').includes(anchor));
    if (!hits.length) return null;
    hits.sort((a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return (ra.width * ra.height) - (rb.width * rb.height);
    });
    return hits[0];
  }
  function resolve(selectors, anchor) {
    for (const s of (selectors || [])) {
      let els;
      try { els = Array.from(document.querySelectorAll(s)); } catch (e) { continue; }
      const el = els.find(isVisible);
      if (el) return { el, via: 'selector', hit: s, count: els.filter(isVisible).length };
    }
    if (anchor) {
      const el = smallestWithText(anchor);
      if (el) return { el, via: 'text-anchor', hit: null, count: 1 };
    }
    return null;
  }

  for (const [name, spec] of Object.entries(cfg.regions)) {
    const r = resolve(spec[0], spec[1]);
    if (!r) { out.notes.push(name + ': NOT FOUND'); continue; }
    out.regions[name] = {
      via: r.via, selector: r.hit, matchCount: r.count,
      tag: r.el.tagName.toLowerCase(), id: r.el.id || null,
      className: (typeof r.el.className === 'string' ? r.el.className : '').slice(0, 200),
      text: (r.el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160),
      rect: rectOf(r.el), styles: stylesOf(r.el),
    };
    // For chart bars we also want the per-series fill set (bars differ by bucket).
    if (r.el.tagName && String(r.el.tagName).toLowerCase() !== 'table'
        && (r.el.className || '').toString().indexOf('highcharts') >= 0) {
      const sibs = Array.from(document.querySelectorAll('.highcharts-series path, .highcharts-series rect, .highcharts-point'))
        .filter(isVisible).slice(0, 14);
      out.regions[name].siblings = sibs.map(e => ({
        tag: e.tagName.toLowerCase(), fill: getComputedStyle(e).fill,
        stroke: getComputedStyle(e).stroke, opacity: getComputedStyle(e).opacity,
        rect: rectOf(e),
        label: (e.nextElementSibling && e.nextElementSibling.textContent || '').trim().slice(0, 20),
      }));
    }
  }

  // Global typography + palette anchors, always captured regardless of regions.
  const body = document.body;
  out.page = {
    title: document.title,
    url: location.href,
    bodyFont: body ? getComputedStyle(body).fontFamily : null,
    bodyFontSize: body ? getComputedStyle(body).fontSize : null,
    bodyColor: body ? getComputedStyle(body).color : null,
    bodyBg: body ? getComputedStyle(body).backgroundColor : null,
    stylesheets: Array.from(document.querySelectorAll('link[rel="stylesheet"]'))
      .map(l => l.href).slice(0, 40),
  };

  // Full text skeleton of the tool so we can see section order/labels.
  out.skeleton = Array.from(document.querySelectorAll('h1,h2,h3,h4,caption,th,.title'))
    .filter(isVisible).slice(0, 120)
    .map(e => ({ tag: e.tagName.toLowerCase(), cls: (e.className || '').toString().slice(0, 80),
                 text: (e.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 90) }));

  // Every visible table: headers + first two rows, so column order is measured.
  out.tables = Array.from(document.querySelectorAll('table')).filter(isVisible).slice(0, 20)
    .map(t => ({
      cls: (t.className || '').toString().slice(0, 120),
      rect: rectOf(t),
      borderCollapse: getComputedStyle(t).borderCollapse,
      headStyles: (() => { const th = t.querySelector('th, thead td');
        return th ? stylesOf(th) : null; })(),
      cellStyles: (() => { const td = t.querySelector('tbody td');
        return td ? stylesOf(td) : null; })(),
      rows: Array.from(t.querySelectorAll('tr')).slice(0, 4)
        .map(tr => Array.from(tr.querySelectorAll('th,td'))
          .map(c => (c.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 30))),
    }));
  return out;
}
"""


def measure_in_frame(page, regions, label):
    """Run JS in the QuikStrike iframe (or the top frame) and return the result.

    The FedWatch tool is an ASP.NET UpdatePanel that renders well after the
    iframe's own load event, so we poll every frame for the marker text before
    measuring - measuring a still-blank frame yields zero regions.
    """
    target = page
    frames = list(page.frames)
    print(f"  [{label}] frames: {[f.url[:70] for f in frames]}")
    for fr in frames:
        if "quikstrike" in (fr.url or "").lower():
            target = fr
            break
    print(f"  [{label}] measuring in: {target.url[:90]}")

    marker = ("Meeting Information", "Target Rate (bps)", "MEETING INFORMATION")
    for attempt in range(40):
        try:
            txt = target.evaluate("() => (document.body ? document.body.innerText : '')") or ""
        except Exception as e:
            txt = ""
            print(f"  [{label}] poll {attempt} err: {type(e).__name__}")
        if any(m in txt for m in marker):
            print(f"  [{label}] content ready after ~{attempt * 2}s (len {len(txt)})")
            break
        target.page.wait_for_timeout(2000)
    else:
        print(f"  [{label}] WARNING: marker never appeared")

    return target.evaluate(JS, {"props": STYLE_PROPS, "regions": regions})


def main() -> int:
    # cmegroup.com rate-limits repeat Playwright navigations (ERR_HTTP2_PROTOCOL_ERROR),
    # so the investing-only mode exists to re-measure the grid without burning the CME budget.
    only_inv = "--only-inv" in sys.argv
    os.makedirs(OUT_DIR, exist_ok=True)
    from playwright.sync_api import sync_playwright

    exe = resolve_chrome()
    print("chrome:", exe or "playwright bundled")
    result = {}
    fn_out = os.path.join(OUT_DIR, "measured_design.json")
    if only_inv and os.path.exists(fn_out):
        # Keep the CME sections measured earlier; --only-inv refreshes investing only.
        try:
            result.update(json.load(open(fn_out, encoding="utf-8")))
        except Exception as e:
            print("  could not reuse previous JSON:", e)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=exe)
        ctx = browser.new_context(
            viewport={"width": 1600, "height": 1100},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"),
            locale="en-US",
        )

        # ── 1. CME FedWatch ────────────────────────────────────────────────
        if not only_inv:
          page = ctx.new_page()
          try:
            page.goto(CME_URL, wait_until="domcontentloaded", timeout=90000)
            # Cookie wall covers the tool until dismissed.
            for sel in ("#onetrust-accept-btn-handler", "button:has-text('Accept All Cookies')",
                        "button:has-text('Accept all')"):
                try:
                    page.click(sel, timeout=4000)
                    page.wait_for_timeout(1500)
                    break
                except Exception:
                    pass
            page.wait_for_timeout(6000)
            try:
                page.mouse.wheel(0, 1200)
                page.wait_for_timeout(2500)
            except Exception:
                pass
            m = measure_in_frame(page, CME_REGIONS, "cme")
            result["cme_fedwatch"] = m
            page.screenshot(path=os.path.join(OUT_DIR, "cme_full.png"), full_page=True)
            tf = [f for f in page.frames if "quikstrike" in (f.url or "").lower()]
            if tf:
                try:
                    # The meeting-date tab strip sits at the very top of the tool,
                    # below the CME site header - scroll the frame up so it is in shot.
                    tf[0].evaluate("() => window.scrollTo(0, 0)")
                    page.evaluate("() => window.scrollTo(0, 0)")
                    page.wait_for_timeout(1200)
                    tf[0].locator("body").screenshot(path=os.path.join(OUT_DIR, "cme_tool.png"))
                except Exception as e:
                    print("  tool screenshot failed:", e)
            print("  regions found:", len(m.get("regions", {})), "notes:", m.get("notes"))

            # ── 1b. Probabilities view (tree node click inside the frame) ──
            tf = [f for f in page.frames if "quikstrike" in (f.url or "").lower()]
            if tf:
                fr = tf[0]
                try:
                    clicked = fr.evaluate(r"""
                      () => {
                        // The tree renders as jQuery-UI vertical tabs; clicking the
                        // <li> is a no-op, the navigation lives on the inner <a>.
                        const cands = Array.from(document.querySelectorAll('a'))
                          .filter(e => (e.textContent || '').trim() === 'Probabilities');
                        if (!cands.length) {
                          const li = Array.from(document.querySelectorAll('li'))
                            .find(e => (e.textContent || '').trim() === 'Probabilities');
                          if (li) { const a = li.querySelector('a'); if (a) { a.click();
                            return 'clicked-inner-a'; } li.click(); return 'clicked-li-noanchor'; }
                          return 'no-node';
                        }
                        cands[0].click();
                        return 'clicked:a.' + (cands[0].className || '') + ' href=' +
                               (cands[0].getAttribute('href') || '').slice(0, 80);
                      }
                    """)
                    print("  [prob] tree click:", clicked)
                    page.wait_for_timeout(12000)
                    mp = fr.evaluate(JS, {"props": STYLE_PROPS, "regions": PROB_REGIONS})
                    result["cme_probabilities"] = mp
                    try:
                        fr.locator("body").screenshot(path=os.path.join(OUT_DIR, "cme_probabilities.png"))
                    except Exception as e:
                        print("  prob screenshot failed:", e)
                    print("  prob regions found:", len(mp.get("regions", {})),
                          "notes:", mp.get("notes"))
                except Exception as e:
                    result["cme_probabilities"] = {"error": f"{type(e).__name__}: {e}"}
                    print("  PROB ERROR:", type(e).__name__, e)
          except Exception as e:
            result["cme_fedwatch"] = {"error": f"{type(e).__name__}: {e}"}
            print("CME ERROR:", type(e).__name__, e)

        # ── 2. investing.com Eurodollar ────────────────────────────────────
        page2 = ctx.new_page()
        try:
            page2.goto(INV_URL, wait_until="domcontentloaded", timeout=90000)
            page2.wait_for_timeout(6000)
            for sel in ("#onetrust-accept-btn-handler", "button:has-text('Accept all')"):
                try:
                    page2.click(sel, timeout=2500)
                    page2.wait_for_timeout(1200)
                    break
                except Exception:
                    pass
            m2 = page2.evaluate(JS, {"props": STYLE_PROPS, "regions": INV_REGIONS})
            result["eurodollar"] = m2
            page2.screenshot(path=os.path.join(OUT_DIR, "investing_full.png"), full_page=True)
            try:
                t = page2.query_selector("table")
                if t:
                    t.screenshot(path=os.path.join(OUT_DIR, "investing_table.png"))
            except Exception as e:
                print("  table screenshot failed:", e)
            print("  inv regions found:", len(m2.get("regions", {})), "notes:", m2.get("notes"))
        except Exception as e:
            result["eurodollar"] = {"error": f"{type(e).__name__}: {e}"}
            print("INV ERROR:", type(e).__name__, e)

        browser.close()

    fn = fn_out
    with open(fn, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
    print("wrote", fn, os.path.getsize(fn), "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
