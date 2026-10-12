"""Local HTML dashboard (single self-contained file, no external resources, never published).

python -m breadth.reports.dashboard  -> storage/reports/dashboard_<date>.html (+ dashboard_latest.html)

Charts are inline SVG rendered here (works offline). Colors follow the validated reference
palette: series-1 blue = TASI / index, series-2 orange = breadth metric; status colors carry
regime state together with a text label (never color alone).
"""
from __future__ import annotations

import html
import json
from datetime import date

import numpy as np
import pandas as pd

from breadth import ROOT, load_config

PAL = {"blue": "#2a78d6", "orange": "#eb6834", "aqua": "#1baf7a", "violet": "#4a3aa7", "red": "#e34948",
       "good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b",
       "grid": "#e6e5e1", "text": "#0b0b0b", "text2": "#52514e", "surface": "#fcfcfb", "mid": "#f0efec"}
SEQ = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95"]
REGIME_AR = {"Strong": "Strong", "Healthy": "Healthy", "Neutral": "Neutral", "Weak": "Weak", "Washout": "Washout"}
REGIME_COLOR = {"Strong": PAL["good"], "Healthy": PAL["good"], "Neutral": PAL["text2"], "Weak": PAL["serious"], "Washout": PAL["violet"]}
REGIME_ICON = {"Strong": "▲▲", "Healthy": "▲", "Neutral": "●", "Weak": "▼", "Washout": "◆"}


# --------------------------------------------------------------------------- helpers
def _load(cfg):
    st = ROOT / cfg["paths"]["storage_dir"]
    F = {n: pd.read_parquet(st / "features" / f"{n}.parquet") for n in
         ["participation", "advance_decline", "thrusts", "momentum", "sectors", "sectors_summary", "concentration", "divergences", "composite"]}
    for k in F:
        if "date" in F[k].columns and k != "sectors":
            F[k] = F[k].set_index("date")
    idx = pd.read_parquet(st / "clean" / "index.parquet")
    F["tasi"] = idx[idx["index_code"] == "TASI"].set_index("date")["close"]
    F["ew"] = idx[idx["index_code"] == "EW_TASI"].set_index("date")["close"]
    ca = st / "master" / "corporate_actions.csv"
    F["actions"] = pd.read_csv(ca) if ca.exists() else pd.DataFrame()
    return F, st


def fmt(v, d=1):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"\u2066{v:,.{d}f}\u2069"          # LRI…PDI isolate (harmless in LTR, keeps signs correct if embedded in RTL text)


def line_chart(series: dict[str, pd.Series], title: str, width=640, height=220, zero_line=False, bands=None, note=""):
    """Multi-series SVG line chart, single y-axis per chart (series are normalised when `bands` is None
    and more than one series with different scales -> caller passes already comparable series)."""
    keys = list(series)
    df = pd.DataFrame(series).dropna(how="all")
    if df.empty:
        return f"<div class='card'><h3>{html.escape(title)}</h3><p class='muted'>no data</p></div>"
    L, R, T, B = 48, 12, 28, 26
    w, h = width - L - R, height - T - B
    x = np.linspace(0, w, len(df))
    ymin, ymax = np.nanmin(df.to_numpy()), np.nanmax(df.to_numpy())
    if zero_line:
        ymin, ymax = min(ymin, 0), max(ymax, 0)
    pad = (ymax - ymin) * 0.06 or 1
    ymin, ymax = ymin - pad, ymax + pad

    def Y(v):
        return T + h - (v - ymin) / (ymax - ymin) * h
    colors = [PAL["blue"], PAL["orange"], PAL["aqua"], PAL["violet"]]
    out = [f"<svg viewBox='0 0 {width} {height}' class='chart' role='img' aria-label='{html.escape(title)}'>"]
    if bands:
        for lo, hi, col in bands:
            out.append(f"<rect x='{L}' y='{Y(hi):.1f}' width='{w}' height='{Y(lo)-Y(hi):.1f}' fill='{col}' opacity='0.35'/>")
    for k in range(5):
        v = ymin + (ymax - ymin) * k / 4
        out.append(f"<line x1='{L}' x2='{L+w}' y1='{Y(v):.1f}' y2='{Y(v):.1f}' stroke='{PAL['grid']}' stroke-width='1'/>")
        out.append(f"<text x='{L-6}' y='{Y(v)+4:.1f}' text-anchor='end' class='tick'>{v:,.0f}</text>")
    if zero_line and ymin < 0 < ymax:
        out.append(f"<line x1='{L}' x2='{L+w}' y1='{Y(0):.1f}' y2='{Y(0):.1f}' stroke='{PAL['text2']}' stroke-width='1' stroke-dasharray='3,3'/>")
    for i, k in enumerate(keys):
        s = df[k].to_numpy(dtype=float)
        pts = [f"{x[j]+L:.1f},{Y(s[j]):.1f}" for j in range(len(s)) if not np.isnan(s[j])]
        out.append(f"<polyline fill='none' stroke='{colors[i]}' stroke-width='2' stroke-linejoin='round' points='{' '.join(pts)}'/>")
    # x ticks (5)
    for j in np.linspace(0, len(df) - 1, 5).astype(int):
        out.append(f"<text x='{x[j]+L:.1f}' y='{height-8}' text-anchor='middle' class='tick'>{df.index[j].strftime('%Y-%m')}</text>")
    # hover layer
    data = json.dumps({"dates": [d.strftime("%Y-%m-%d") for d in df.index], "series": {k: [None if np.isnan(v) else round(float(v), 2) for v in df[k]] for k in keys},
                       "x": [round(float(v + L), 1) for v in x]})
    out.append(f"<rect class='hover' x='{L}' y='{T}' width='{w}' height='{h}' fill='transparent'/>")
    out.append(f"<line class='xh' x1='0' x2='0' y1='{T}' y2='{T+h}' stroke='{PAL['text2']}' stroke-width='1' visibility='hidden'/>")
    out.append("</svg>")
    legend = " ".join(f"<span class='lg'><i style='background:{colors[i]}'></i>{html.escape(k)}</span>" for i, k in enumerate(keys))
    return (f"<div class='card' data-chart='{html.escape(data, quote=True)}'><h3>{html.escape(title)}</h3>"
            f"<div class='legend'>{legend}</div>{''.join(out)}<div class='tip' hidden></div>"
            f"{'<p class=muted>'+html.escape(note)+'</p>' if note else ''}</div>")


def gauge(score: float, regime: str) -> str:
    """Semi-circular gauge 0-100 with the regime bands."""
    cx, cy, r = 110, 110, 90
    import math

    def arc(a0, a1, col):
        p0 = (cx + r * math.cos(math.pi * (1 - a0)), cy - r * math.sin(math.pi * (1 - a0)))
        p1 = (cx + r * math.cos(math.pi * (1 - a1)), cy - r * math.sin(math.pi * (1 - a1)))
        return f"<path d='M{p0[0]:.1f},{p0[1]:.1f} A{r},{r} 0 0 1 {p1[0]:.1f},{p1[1]:.1f}' stroke='{col}' stroke-width='14' fill='none'/>"
    bands = [(0, .40, PAL["serious"]), (.40, .60, PAL["text2"]), (.60, .75, PAL["good"]), (.75, 1, PAL["good"])]
    parts = [f"<svg viewBox='-10 0 240 130' class='gauge' role='img' aria-label='Breadth Score {score:.0f}'>"]
    for a0, a1, col in bands:
        parts.append(arc(a0 + 0.004, a1 - 0.004, col))
    a = max(0, min(100, score)) / 100
    px, py = cx + (r - 22) * math.cos(math.pi * (1 - a)), cy - (r - 22) * math.sin(math.pi * (1 - a))
    parts.append(f"<line x1='{cx}' y1='{cy}' x2='{px:.1f}' y2='{py:.1f}' stroke='{PAL['text']}' stroke-width='3'/>")
    parts.append(f"<circle cx='{cx}' cy='{cy}' r='5' fill='{PAL['text']}'/>")
    parts.append(f"<text x='{cx}' y='{cy-30}' text-anchor='middle' class='big'>{score:.0f}</text>")
    for v in (0, 40, 60, 75, 100):
        aa = v / 100; tx, ty = cx + (r + 16) * math.cos(math.pi * (1 - aa)), cy - (r + 16) * math.sin(math.pi * (1 - aa))
        parts.append(f"<text x='{tx:.1f}' y='{ty+4:.1f}' text-anchor='middle' class='tick'>{v}</text>")
    parts.append("</svg>")
    return "".join(parts)


def heat_color(v: float) -> str:
    if v is None or np.isnan(v):
        return PAL["mid"]
    i = int(np.clip(v / 100 * (len(SEQ) - 1), 0, len(SEQ) - 1))
    return SEQ[i]


# --------------------------------------------------------------------------- build
def build(cfg: dict, days: int = 250) -> str:
    F, st = _load(cfg)
    comp = F["composite"].dropna(subset=["score"])
    d = comp.index[-1]; prev = comp.index[-2]
    row, prow = comp.loc[d], comp.loc[prev]
    part, ad, th, mom, sec, secs, conc, div = (F[k] for k in ["participation", "advance_decline", "thrusts", "momentum", "sectors", "sectors_summary", "concentration", "divergences"])
    tasi, ew = F["tasi"], F["ew"]
    win = comp.index[-days:]
    regime = row["regime"]; score = float(row["score"]); chg = float(row["score"] - prow["score"])
    driver = str(row["driver"]).replace("_", " ")

    # ---- five questions
    p_now, p_prev = part.loc[d], part.loc[prev]
    a_now = ad.loc[d]
    q = []
    q.append(("1. Is participation broad or narrow?", f"%>200DMA = {fmt(p_now['pct_above_sma200'],0)}% · %>50DMA = {fmt(p_now['pct_above_sma50'],0)}% · Stage 2 = {fmt(p_now['pct_stage2'],0)}% · healthy sectors {int(secs.loc[d,'n_healthy'])}/{int(secs.loc[d,'n_sectors'])}",
              "Broad" if p_now['pct_above_sma200'] > 60 else ("Narrow" if p_now['pct_above_sma200'] < 40 else "Mixed")))
    ad20 = ad["ad_line"].diff(20).loc[d]; mc = a_now["mcclellan_osc"]
    q.append(("2. Is breadth improving or deteriorating?", f"A/D line 20d change = {fmt(ad20,0)} · McClellan = {fmt(mc,0)} · Score today {score:.0f} ({chg:+.1f} vs yesterday)",
              "Improving" if (ad20 > 0 and mc > 0) else ("Deteriorating" if (ad20 < 0 and mc < 0) else "Mixed")))
    dv = div.loc[d]
    if dv["index_high"]:
        conf = "60d index high, confirmed by all metrics" if dv["n_bearish"] == 0 else f"60d index high with {int(dv['n_bearish'])} diverging metric(s)"
    elif dv["index_low"]:
        conf = "60d index low" + (" with breadth holding up" if dv["n_bullish"] >= 2 else "")
    else:
        conf = "Index not at a 60d high/low"
    sp60 = conc.loc[d, "spread_60"]
    q.append(("3. Is breadth confirming the index?", f"{conf} · EW−TASI 60d spread = {fmt(sp60)} pts",
              "Mega-caps masking weakness" if sp60 < -5 else ("Broad market leading" if sp60 > 5 else "Balanced")))
    ov = float(row["overlay"]); t10 = a_now["trin_10"]; r10 = mom.loc[d, "ratio_10"]
    recent_thr = [c for c in ["zweig_signal", "whaley_signal", "pct50_thrust_signal"] if th[c].loc[win[-20]:d].any()]
    q.append(("4. Oversold / overbought? Thrust?", f"Overlay = {ov:.0f}/100 · TRIN-10 = {fmt(t10,2)} · 4% ratio (10d) = {fmt(r10,2)} · Zweig EMA = {fmt(th.loc[d,'zweig_value'],3)}" + (f" · recent thrust: {', '.join(s.replace('_signal','') for s in recent_thr)}" if recent_thr else " · no thrust in the last 20 days"),
              "Oversold" if ov >= 75 else ("Exhaustion / overbought" if (mom.loc[d, 'pct_up_x'] > 20 or mc > 100) else "No extreme")))
    q.append(("5. Regime & Score", f"Breadth Score = {score:.0f}/100 (EMA5 = {row['score_smooth']:.0f}) · driver: {driver}", f"{REGIME_ICON[regime]} {REGIME_AR[regime]}"))

    # ---- charts (last `days`)
    ch = []
    idx_n = tasi.reindex(win); adl = ad["ad_line"].reindex(win)
    ch.append(line_chart({"TASI (rebased 100)": 100 * idx_n / idx_n.dropna().iloc[0], "A/D line (rebased)": 100 + (adl - adl.iloc[0]) / max(1, adl.abs().max()) * 100},
                         "TASI vs A/D line", note="Both rebased to 100 at the start of the window — shape matters, not level"))
    ch.append(line_chart({"%>50DMA": part["pct_above_sma50"].reindex(win), "%>200DMA": part["pct_above_sma200"].reindex(win)}, "% of stocks above 50 & 200 DMA",
                         bands=[(60, 80, "#fde8de"), (80, 100, "#dff3df")], note="Orange band 60–80 = historical topping zone; green >80 = best state"))
    ch.append(line_chart({"Net NH−NL (daily)": part["net_nh_nl"].reindex(win)}, "52-week new highs − new lows (daily net)", zero_line=True))
    ch.append(line_chart({"High-Low Index": part["hl_index"].reindex(win)}, "High-Low Index (10d MA of NH/(NH+NL))", bands=[(0.3, 0.7, "#fde8de")], note="0.3–0.7 is the historically weakest zone; both extremes are better"))
    ch.append(line_chart({"McClellan Osc": ad["mcclellan_osc"].reindex(win)}, "McClellan Oscillator", zero_line=True, bands=[(-300, -100, "#dff3df"), (100, 300, "#fde8de")]))
    ch.append(line_chart({"Summation": ad["mcclellan_sum"].reindex(win)}, "McClellan Summation", zero_line=True))
    ew_n = ew.reindex(win)
    ch.append(line_chart({"TASI": 100 * idx_n / idx_n.dropna().iloc[0], "Equal-Weight TASI": 100 * ew_n / ew_n.dropna().iloc[0]}, "TASI vs Equal-Weight TASI (both rebased = 100)"))
    ch.append(line_chart({"Breadth Score": comp["score"].reindex(win), "EMA5": comp["score_smooth"].reindex(win)}, "Breadth Score", bands=[(0, 40, "#fde8de"), (60, 100, "#dff3df")]))
    ch.append(line_chart({"Overlay (oversold / thrust)": comp["overlay"].reindex(win)}, "Overlay — oversold & thrust context", bands=[(75, 100, "#e8e4f7")]))

    # ---- sector heatmap (last 10 days x sectors)
    last10 = secs.index[-10:]
    sl = sec[sec["date"].isin(last10)].pivot(index="sector", columns="date", values="score")
    sl = sl.sort_values(sl.columns[-1], ascending=False)
    hm = ["<table class='heat'><tr><th>Sector</th>"] + [f"<th>{c.strftime('%m-%d')}</th>" for c in sl.columns] + ["<th>%>50</th><th>%>200</th><th>NH−NL</th><th>n</th></tr>"]
    today_sec = sec[sec["date"] == d].set_index("sector")
    for s_, r_ in sl.iterrows():
        cells = "".join(f"<td style='background:{heat_color(v)};color:{'#fff' if (not np.isnan(v) and v>55) else PAL['text']}'>{fmt(v,0)}</td>" for v in r_.values)
        t = today_sec.loc[s_] if s_ in today_sec.index else None
        extra = f"<td>{fmt(t['pct50'],0)}</td><td>{fmt(t['pct200'],0)}</td><td>{fmt(t['nhnl'],0)}</td><td>{int(t['n'])}</td>" if t is not None else "<td></td>" * 4
        hm.append(f"<tr><th>{html.escape(str(s_))}</th>{cells}{extra}</tr>")
    hm.append("</table>")

    # ---- Stockbee monitor table (last 10 days)
    mcols = ["up_x", "down_x", "ratio_5", "ratio_10", "up_25_q", "down_25_q", "up_25_m", "down_25_m", "up_50_m", "down_50_m", "up_13_34", "down_13_34"]
    mt = mom[mcols].tail(10).iloc[::-1]
    sb = ["<table class='mon'><tr><th>Date</th><th>↑4%</th><th>↓4%</th><th>Ratio 5d</th><th>Ratio 10d</th><th>↑25% qtr</th><th>↓25% qtr</th><th>↑25% mo</th><th>↓25% mo</th><th>↑50% mo</th><th>↓50% mo</th><th>↑13%/34d</th><th>↓13%/34d</th></tr>"]
    for dd, r_ in mt.iterrows():
        sb.append(f"<tr><td>{dd.strftime('%Y-%m-%d')}</td>" + "".join(f"<td>{fmt(r_[c], 2 if 'ratio' in c else 0)}</td>" for c in mcols) + "</tr>")
    sb.append("</table>")

    # ---- alerts
    al = []
    for c, name in [("zweig_signal", "Zweig Breadth Thrust"), ("whaley_signal", "Whaley 5d thrust (20-day horizon)"), ("pct50_thrust_signal", "%>50DMA thrust (20→55)"), ("lowry_down_signal", "95% down day (Lowry capitulation)")]:
        for dd in th.index[th[c].fillna(False)][-3:]:
            if dd >= win[0]:
                al.append((dd, "thrust", f"{name}"))
    for dd in div.index[div["bearish_signal"].fillna(False)][-3:]:
        if dd >= win[0]:
            al.append((dd, "div", "60d index high not confirmed by ≥2 breadth metrics (confirmation-quality warning — not a sell signal)"))
    for dd in comp.index[(comp["regime"] != comp["regime"].shift())][-6:]:
        if dd >= win[0]:
            al.append((dd, "regime", f"Regime change → {REGIME_AR[comp.loc[dd,'regime']]}"))
    if len(F["actions"]):
        a = F["actions"].copy(); a["ex_date"] = pd.to_datetime(a["ex_date"])
        for _, r_ in a[a["ex_date"] >= win[0]].iterrows():
            al.append((r_["ex_date"], "data", f"Detected corporate action: {int(r_['symbol'])} {r_['action_type']} ×{r_['factor']:.3f} (confidence {r_['confidence']}) — review"))
    al.sort(key=lambda t: t[0], reverse=True)
    alerts = "".join(f"<li class='{k}'><span class='dt'>{dd.strftime('%Y-%m-%d')}</span>{html.escape(msg)}</li>" for dd, k, msg in al[:25]) or "<li class='muted'>No alerts in the window</li>"

    # ---- top-5 contribution (if available)
    top5 = ""
    if pd.notna(conc.loc[d].get("top5_share", np.nan)):
        top5 = f"<p>Top 5 names explain <b>{100*conc.loc[d,'top5_share']:.0f}%</b> of today's move ({html.escape(str(conc.loc[d,'top5_names']))}) — contribution {fmt(conc.loc[d,'top5_contrib'],2)} pts of {fmt(conc.loc[d,'cw_proxy_ret'],2)}% (cap-weighted proxy, 15% cap)</p>"

    # ---- family contributions
    fams = [c for c in comp.columns if c.startswith("contrib_")]
    fam_rows = "".join(f"<tr><td>{c.replace('contrib_','').replace('_',' ')}</td><td>{fmt(comp.loc[d, c.replace('contrib_','')],0)}</td><td>{fmt(row[c],1)}</td><td>{fmt(row[c.replace('contrib_','delta_')],1)}</td></tr>" for c in fams)

    css = f"""
    :root{{--surface:{PAL['surface']};--text:{PAL['text']};--text2:{PAL['text2']};--grid:{PAL['grid']}}}
    body{{font-family:Segoe UI,Tahoma,Arial,sans-serif;background:var(--surface);color:var(--text);margin:0;padding:16px;direction:ltr}}
    h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:22px 0 8px;border-bottom:1px solid var(--grid);padding-bottom:4px}} h3{{font-size:14px;margin:0 0 4px;color:var(--text2)}}
    .muted{{color:var(--text2);font-size:12px}} .top{{display:flex;gap:18px;flex-wrap:wrap;align-items:center}}
    .regime{{font-size:26px;font-weight:700;padding:8px 16px;border-radius:8px;color:#fff;background:{REGIME_COLOR[regime]}}}
    .kpi{{font-size:13px;line-height:1.7}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px}}
    .card{{border:1px solid var(--grid);border-radius:8px;padding:10px;background:#fff;position:relative}} .chart{{width:100%;height:auto}}
    .tick{{font-size:10px;fill:var(--text2)}} svg{{direction:ltr}} svg text{{unicode-bidi:plaintext}} .big{{font-size:34px;font-weight:700;fill:var(--text)}} .gauge{{width:240px}}
    .legend{{font-size:12px;color:var(--text2);margin-bottom:4px}} .lg i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}} .lg{{margin-right:12px}}
    table{{border-collapse:collapse;font-size:12px;width:100%}} th,td{{border:1px solid var(--grid);padding:4px 6px;text-align:center}} td{{unicode-bidi:plaintext}} th{{background:#f6f6f4}}
    .heat th:first-child{{text-align:left;white-space:nowrap}} .q td:first-child{{text-align:left;font-weight:600;width:28%}} .q td{{text-align:left}} .q td:last-child{{font-weight:700;width:18%}}
    ul.alerts{{list-style:none;padding:0;margin:0;font-size:13px}} ul.alerts li{{padding:4px 8px;border-left:4px solid var(--grid);margin:3px 0;background:#fff}}
    li.thrust{{border-color:{PAL['good']}}} li.div{{border-color:{PAL['warning']}}} li.regime{{border-color:{PAL['blue']}}} li.data{{border-color:{PAL['text2']}}} .dt{{color:var(--text2);margin-right:8px;font-family:monospace}}
    .tip{{position:absolute;background:#fff;border:1px solid var(--grid);border-radius:6px;padding:6px 8px;font-size:12px;pointer-events:none;direction:ltr;box-shadow:0 2px 6px rgba(0,0,0,.08)}}
    footer{{margin-top:24px;font-size:11px;color:var(--text2)}}
    """
    js = """
    document.querySelectorAll('.card[data-chart]').forEach(card=>{const data=JSON.parse(card.dataset.chart);const svg=card.querySelector('svg');const hover=svg.querySelector('.hover');const xh=svg.querySelector('.xh');const tip=card.querySelector('.tip');
      hover.addEventListener('mousemove',e=>{const pt=svg.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;const p=pt.matrixTransform(svg.getScreenCTM().inverse());let best=0,bd=1e9;data.x.forEach((x,i)=>{const dd=Math.abs(x-p.x);if(dd<bd){bd=dd;best=i}});
        xh.setAttribute('x1',data.x[best]);xh.setAttribute('x2',data.x[best]);xh.setAttribute('visibility','visible');
        let h='<b>'+data.dates[best]+'</b>';for(const k in data.series){const v=data.series[k][best];h+='<br>'+k+': '+(v===null?'—':v.toLocaleString())}tip.innerHTML=h;tip.hidden=false;
        const r=card.getBoundingClientRect();tip.style.left=Math.min(e.clientX-r.left+12,r.width-170)+'px';tip.style.top=(e.clientY-r.top+12)+'px';});
      hover.addEventListener('mouseleave',()=>{tip.hidden=true;xh.setAttribute('visibility','hidden')});});
    """
    doc = f"""<!DOCTYPE html><html lang="en" dir="ltr"><head><meta charset="utf-8"><title>TASI Breadth — {d.date()}</title><style>{css}</style></head><body>
    <h1>TASI Market Breadth — {d.strftime('%Y-%m-%d')}</h1><p class="muted">Local file generated by the engine — never published. Chart window: last {days} trading days.</p>
    <div class="top">{gauge(score, regime)}<div><div class="regime">{REGIME_ICON[regime]} {REGIME_AR[regime]}</div>
    <div class="kpi">Breadth Score <b>{score:.0f}</b> ({chg:+.1f} vs yesterday) · EMA5 {row['score_smooth']:.0f} · Overlay {ov:.0f}<br>Driver today: <b>{html.escape(driver)}</b> · TASI {fmt(tasi.get(d, float('nan')),0)} ({(100*(tasi.get(d, float('nan'))/tasi.get(prev, float('nan'))-1)):+.2f}%){'' if d in tasi.index else ' — index file not updated to this date'}<br>Regime since: {comp.index[(comp['regime']!=comp['regime'].shift())][-1].strftime('%Y-%m-%d')}</div></div></div>
    <h2>The five questions</h2><table class="q">{''.join(f"<tr><td>{a}</td><td>{html.escape(b)}</td><td>{c}</td></tr>" for a,b,c in q)}</table>
    <h2>Family contributions to the Score</h2><table><tr><th>Family</th><th>Family score (0–100)</th><th>Contribution (± vs 50)</th><th>Change today</th></tr>{fam_rows}</table>{top5}
    <h2>Charts</h2><div class="grid">{''.join(ch)}</div>
    <h2>Sector heatmap (Sector Health Score, last 10 days)</h2>{''.join(hm)}<p class="muted">Score = mean of %>50DMA, %>200DMA, normalised NH−NL and 10d A/(A+D). Healthy ≥ 60, weak ≤ 40. Groups with fewer than 5 eligible stocks are pooled into Other.</p>
    <h2>Stockbee Monitor (≥4% moves with volume above the prior day)</h2>{''.join(sb)}
    <h2>Alerts</h2><ul class="alerts">{alerts}</ul>
    <footer>Universe: TASI main-market common stocks, no REITs · adjusted prices · suspension days excluded · official TASI from 2007 · own Equal-Weight TASI from 2002 · Score = ⅓ participation + ⅓ advance/decline + ⅓ sectors on rolling 5-year percentiles (Phase 6).</footer>
    <script>{js}</script></body></html>"""
    out_dir = st / "reports"; out_dir.mkdir(exist_ok=True)
    p = out_dir / f"dashboard_{d.date()}.html"
    p.write_text(doc, encoding="utf-8")
    (out_dir / "dashboard_latest.html").write_text(doc, encoding="utf-8")
    return str(p)


if __name__ == "__main__":
    print(build(load_config()))
