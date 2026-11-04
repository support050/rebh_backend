"""
extract_fred_design.py — Phase 1 design extraction for the FRED indicator pages.

WHAT THIS DOES
--------------
Opens the LIVE FRED series pages for the target indicators in a real browser
and pulls the *exact* computed styles + geometry (getComputedStyle +
getBoundingClientRect) for the regions we must replicate pixel-for-pixel:

  1. Series title / header
  2. The "Observations" metadata box (latest value, updated, next release,
     units, frequency)
  3. The chart container
  4. The chart control row (1Y/5Y/10Y/Max, Edit Graph, Download, ...)
  5. Page-level design tokens (body font, link colour, page background)

It writes the result to a single JSON file. That JSON — NOT any assumption
baked into this script — is the source of truth the frontend styling must use.

WHY Playwright (and how this relates to Scrapling)
--------------------------------------------------
Computed styles can only be read from a *live, JS-rendered* page: the values
are produced by the browser after CSS cascade + responsive layout. Scrapling's
StealthyFetcher is itself a thin wrapper over Playwright/Camoufox, but it hands
back a parsed DOM object, not a JS-evaluation context, so `getComputedStyle`
is not reachable through it. This script therefore drives the SAME underlying
engine directly (Playwright Chromium, or Camoufox with --engine camoufox) and
reuses the project's Chrome-binary resolution (CHROME_BIN / GOOGLE_CHROME_BIN)
exactly like app/scrapers/scrapling_fetch.py does.

USAGE
-----
    # from the backend/ directory, using the project venv:
    python scripts/extract_fred_design.py
    python scripts/extract_fred_design.py --series UNRATE PAYEMS IC4WSA
    python scripts/extract_fred_design.py --engine camoufox --headful
    python scripts/extract_fred_design.py --out output/fred_design_spec.json

Then send back the produced JSON; the frontend card + chart get styled from it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import List, Optional

# ── Chrome binary resolution (mirrors app/scrapers/scrapling_fetch.py) ─────────
_CHROME_ENV_KEYS = ("GOOGLE_CHROME_BIN", "CHROME_BIN")
_CHROME_DEFAULTS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]


def resolve_chrome() -> Optional[str]:
    for key in _CHROME_ENV_KEYS:
        p = os.getenv(key)
        if p and os.path.exists(p):
            return p
    for d in _CHROME_DEFAULTS:
        if os.path.exists(d):
            return d
    return None  # let Playwright use its own bundled chromium


# The properties we need to reproduce the look exactly. Kept as an explicit list
# so the JSON is compact and every value is one we actually consume.
_STYLE_PROPS = [
    # typography
    "font-family", "font-size", "font-weight", "line-height", "letter-spacing",
    "text-align", "text-transform", "font-style",
    # colour
    "color", "background-color",
    # box model
    "width", "height",
    "padding-top", "padding-right", "padding-bottom", "padding-left",
    "margin-top", "margin-right", "margin-bottom", "margin-left",
    # borders / radius
    "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
    "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
    "border-top-style", "border-bottom-style",
    "border-top-left-radius", "border-top-right-radius",
    "border-bottom-left-radius", "border-bottom-right-radius",
    # misc visual
    "box-shadow", "display", "gap", "opacity",
]

# ── Browser-side JS ───────────────────────────────────────────────────────────
# resolveElement tries each selector in order, then falls back to the smallest
# element whose own text contains the anchor string. This keeps the script
# working even when FRED renames its CSS classes between releases.
_JS = r"""
async (cfg) => {
  const PROPS = cfg.props;

  function rectOf(el) {
    const r = el.getBoundingClientRect();
    return {
      x: Math.round(r.x), y: Math.round(r.y),
      width: Math.round(r.width), height: Math.round(r.height),
      top: Math.round(r.top), left: Math.round(r.left),
      inViewport: r.width > 0 && r.height > 0,
    };
  }

  function stylesOf(el) {
    const cs = getComputedStyle(el);
    const out = {};
    for (const p of PROPS) out[p] = cs.getPropertyValue(p);
    return out;
  }

  // A region is only usable if it is actually painted. FRED ships a hidden
  // #mobile-meta-col (display:none) alongside the visible #desktop-meta-col;
  // without this filter the text-anchor used to grab the invisible one (0x0).
  function isVisible(el) {
    if (!el) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function smallestWithText(anchor) {
    const all = Array.from(document.querySelectorAll('div,section,article,aside'));
    const hits = all.filter(e => isVisible(e) && (e.textContent || '').includes(anchor));
    if (!hits.length) return null;
    hits.sort((a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return (ra.width * ra.height) - (rb.width * rb.height);
    });
    return hits[0];
  }

  function resolve(selectors, anchor) {
    for (const s of selectors) {
      const els = Array.from(document.querySelectorAll(s));
      const el = els.find(isVisible) || null;
      if (el) return { el, via: 'selector', selector: s };
    }
    if (anchor) {
      const el = smallestWithText(anchor);
      if (el) return { el, via: 'text-anchor', selector: null };
    }
    return null;
  }

  // Climb from the text column to the nearest ancestor that is a visible
  // "card" (non-transparent background or a real border) — that is FRED's grey
  // Observations box, whose geometry + background we must replicate.
  function cardOf(el) {
    let cur = el && el.parentElement ? el.parentElement : el;
    for (let i = 0; i < 6 && cur; i++) {
      const cs = getComputedStyle(cur);
      const bg = cs.backgroundColor || '';
      const hasBg = bg && bg !== 'transparent' && !/rgba\(\s*0,\s*0,\s*0,\s*0\s*\)/.test(bg);
      const hasBorder = ['border-top-width','border-left-width','border-right-width','border-bottom-width']
        .some(p => parseFloat(cs.getPropertyValue(p)) > 0);
      if ((hasBg || hasBorder) && isVisible(cur)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }

  // The FRED chart is Highcharts (SVG) inside a container; the old selectors
  // missed it. Try known hooks, then fall back to the largest visible <svg>.
  function findChart() {
    const sels = ['.highcharts-container', '#fred-graph-container', '.fred-chart-container',
                  "[id*='graph' i]", "[class*='graph-container' i]", "[class*='chart-container' i]"];
    for (const s of sels) {
      const els = Array.from(document.querySelectorAll(s));
      const el = els.find(isVisible) || null;
      if (el) return { el, via: 'selector', selector: s };
    }
    const svgs = Array.from(document.querySelectorAll('svg')).filter(isVisible);
    if (svgs.length) {
      svgs.sort((a, b) => {
        const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
        return (rb.width * rb.height) - (ra.width * ra.height);
      });
      const host = svgs[0].closest("[class*='graph' i],[class*='chart' i],div") || svgs[0];
      return { el: host, via: 'largest-svg', selector: null };
    }
    return null;
  }

  function describe(name, selectors, anchor, opts) {
    opts = opts || {};
    let r = opts.chart ? findChart() : resolve(selectors, anchor);
    if (!r) return { name, found: false, triedSelectors: selectors, anchor: anchor || null };
    const el = r.el;
    const out = {
      name,
      found: true,
      resolvedVia: r.via,
      selector: r.selector,
      tagName: el.tagName.toLowerCase(),
      id: el.id || null,
      classes: (el.className && el.className.toString) ? el.className.toString().trim().split(/\s+/).slice(0, 12) : [],
      text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 240),
      rect: rectOf(el),
      styles: stylesOf(el),
    };
    if (opts.climbCard) {
      const card = cardOf(el);
      if (card) {
        out.card = {
          tagName: card.tagName.toLowerCase(),
          id: card.id || null,
          classes: (card.className && card.className.toString) ? card.className.toString().trim().split(/\s+/).slice(0, 12) : [],
          rect: rectOf(card),
          styles: stylesOf(card),
        };
      }
    }
    return out;
  }

  // Wait for the metadata box + chart to be present so we measure settled layout.
  await new Promise(res => requestAnimationFrame(() => res()))

  const regions = cfg.regions.map(rg => describe(rg.name, rg.selectors, rg.anchor, rg.opts));

  // Page-level tokens: body + a representative link.
  const body = document.body;
  const aLink = document.querySelector('a[href*="/series/"], a');
  const tokens = {
    pageTitle: document.title,
    url: location.href,
    bodyFontFamily: body ? getComputedStyle(body).getPropertyValue('font-family') : null,
    bodyFontSize: body ? getComputedStyle(body).getPropertyValue('font-size') : null,
    bodyColor: body ? getComputedStyle(body).getPropertyValue('color') : null,
    bodyBackground: body ? getComputedStyle(body).getPropertyValue('background-color') : null,
    sampleLinkColor: aLink ? getComputedStyle(aLink).getPropertyValue('color') : null,
    sampleLinkText: aLink ? (aLink.textContent || '').trim().slice(0, 60) : null,
  };

  return { tokens, regions };
}
"""

# Region definitions: candidate selectors (best-effort, FRED has redesigned a
# few times) + a text anchor used when every selector misses. `opts.climbCard`
# captures the enclosing grey box; `opts.chart` switches to chart detection.
_REGIONS = [
    {
        "name": "series_title_header",
        "selectors": [
            "h1.fred-series-title", "#series-title", ".series-title",
            "h1.title", "div.page-header h1", "h1",
        ],
        "anchor": None,
    },
    {
        "name": "observations_metadata_box",
        "selectors": [
            "#desktop-meta-col", ".fred-series-observations", "#observations",
            ".observations", "div[class*='observation' i]", ".series-observations",
        ],
        "anchor": "Observations",
        "opts": {"climbCard": True},
    },
    {
        "name": "chart_container",
        "selectors": [
            ".highcharts-container", "#fred-graph-container", ".fred-chart-container",
            "#graphdiv", "div[class*='graph-container' i]", "div[class*='chart-container' i]",
        ],
        "anchor": None,
        "opts": {"chart": True},
    },
    {
        "name": "chart_controls_row",
        "selectors": [
            ".graph-options", "#graph-options", "div[class*='graph-control' i]",
            "div[class*='download' i]", ".series-controls",
        ],
        "anchor": "Edit Graph",
    },
]


def scrape_series(page, series_id: str, out_dir: str) -> dict:
    url = f"https://fred.stlouisfed.org/series/{series_id}"
    print(f"  -> navigating {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    # Give FRED's JS a moment to paint the observations box + chart.
    try:
        page.wait_for_selector("text=Observations", timeout=15_000)
    except Exception:
        print("     (no 'Observations' anchor within 15s - measuring whatever is present)")
    page.wait_for_timeout(2500)

    payload = page.evaluate(_JS, {"props": _STYLE_PROPS, "regions": _REGIONS})
    payload["series_id"] = series_id
    payload["requested_url"] = url
    return payload


def main() -> int:
    # Windows defaults stdout to cp1252, which crashes on the non-ASCII marks
    # below; force UTF-8 so the run never dies mid-print.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Extract exact computed styles from live FRED series pages.")
    ap.add_argument("--series", nargs="+", default=["UNRATE", "PAYEMS", "IC4WSA"],
                    help="FRED series ids to extract (default: UNRATE PAYEMS IC4WSA)")
    ap.add_argument("--out", default=None, help="Output JSON path (default: output/fred_design_spec_<ts>.json)")
    ap.add_argument("--engine", choices=["playwright", "camoufox"], default="playwright")
    ap.add_argument("--headful", action="store_true", help="Show the browser window")
    ap.add_argument("--proxy", default=os.getenv("FRED_SCRAPE_PROXY"), help="Optional http(s):// proxy URL")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed in this venv. Run: pip install playwright && playwright install chromium", file=sys.stderr)
        return 2

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = args.out or os.path.join("output", f"fred_design_spec_{ts}.json")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    exe = resolve_chrome()
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": args.engine,
        "chrome_executable": exe,
        "style_props": _STYLE_PROPS,
        "series": {},
    }

    with sync_playwright() as pw:
        launch_kwargs = {"headless": not args.headful}
        if args.proxy:
            launch_kwargs["proxy"] = {"server": args.proxy}

        if args.engine == "camoufox":
            try:
                from camoufox.sync_api import Camoufox
            except ImportError:
                print("camoufox not installed; falling back to playwright chromium", file=sys.stderr)
                args.engine = "playwright"

        context = None
        browser = None
        camoufox = None
        if args.engine == "camoufox":
            from camoufox.sync_api import Camoufox
            camoufox = Camoufox(headless=not args.headful, humanize=True)
            browser = camoufox.__enter__()
            context = browser.new_context() if hasattr(browser, "new_context") else browser
            page = context.new_page() if hasattr(context, "new_page") else browser
        else:
            if exe:
                launch_kwargs["executable_path"] = exe
            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
            page = context.new_page()

        try:
            for sid in args.series:
                print(f"[{sid}]")
                try:
                    result["series"][sid] = scrape_series(page, sid, os.path.dirname(out_path))
                except Exception as e:
                    print(f"     ERROR on {sid}: {type(e).__name__}: {e}", file=sys.stderr)
                    result["series"][sid] = {"series_id": sid, "error": f"{type(e).__name__}: {e}"}
        finally:
            if args.engine == "camoufox" and camoufox is not None:
                camoufox.__exit__(None, None, None)
            else:
                context.close()
                browser.close()

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\n[ok] Wrote design spec -> {out_path}")
    for sid, data in result["series"].items():
        regions = data.get("regions", [])
        found = [r["name"] for r in regions if r.get("found")]
        print(f"   {sid}: {len(found)}/{len(_REGIONS)} regions captured -> {found}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
