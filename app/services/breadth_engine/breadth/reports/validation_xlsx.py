"""Excel validation workbook: formulas that reproduce Python indicator values by hand.

python -m breadth.reports.validation_xlsx [--days 320] [--check 10]

Layout (identical grid on every data sheet: row 1 = symbols from column B, column A = dates):
  Close   adjusted close (blank on suspended days)          [input, blue]
  Level   last valid adjusted close (forward-filled)        [input, blue]
  Volume  adjusted volume                                   [input, blue]
  ExDay   1 on a corporate-action ex-day (return unknown)   [input, blue]
  Age     trading days since listing                        [input, blue]
  Ret     =IF(OR(Close="",Level[prev]="",ExDay=1),"",Close/Level[prev]-1)  [formula]
  SMA20/SMA50/SMA200, Max252/Min252 (prior window), NHflag/NLflag: helper formulas, last `check` rows only
  Daily   per-day A, D, UV, DV, UP, DP, RANA, EMA19, EMA39, McClellan, TRIN   [formulas, all rows]
  Check   Python value vs Excel formula for the last `check` dates, with the difference
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter as L

from breadth import ROOT, load_config
from breadth.indicators.common import build_panels

BLUE = Font(name="Arial", color="0000FF")
BLACK = Font(name="Arial")
BOLD = Font(name="Arial", bold=True)
YELLOW = PatternFill("solid", fgColor="FFFF00")


def _grid(ws, dates, symbols, values: pd.DataFrame | None, font=BLUE, fmt=None):
    ws["A1"] = "date"; ws["A1"].font = BOLD
    for j, s in enumerate(symbols):
        c = ws.cell(row=1, column=j + 2, value=int(s)); c.font = BOLD
    for i, d in enumerate(dates):
        c = ws.cell(row=i + 2, column=1, value=d.to_pydatetime()); c.number_format = "yyyy-mm-dd"; c.font = BOLD
    if values is not None:
        arr = values.to_numpy()
        for i in range(len(dates)):
            for j in range(len(symbols)):
                v = arr[i, j]
                if v is None or (isinstance(v, float) and np.isnan(v)):
                    continue
                c = ws.cell(row=i + 2, column=j + 2, value=float(v)); c.font = font
                if fmt:
                    c.number_format = fmt


def build(cfg: dict, days: int, check: int) -> str:
    st = ROOT / cfg["paths"]["storage_dir"]
    prices = pd.read_parquet(st / "clean" / "prices.parquet")
    master = pd.read_parquet(st / "master" / "symbols.parquet")
    part = pd.read_parquet(st / "features" / "participation.parquet").set_index("date")
    ad = pd.read_parquet(st / "features" / "advance_decline.parquet").set_index("date")
    th = pd.read_parquet(st / "features" / "thrusts.parquet").set_index("date")
    mom = pd.read_parquet(st / "features" / "momentum.parquet").set_index("date")
    secl = pd.read_parquet(st / "features" / "sectors.parquet")
    conc = pd.read_parquet(st / "features" / "concentration.parquet").set_index("date")
    div = pd.read_parquet(st / "features" / "divergences.parquet").set_index("date")
    idx_df = pd.read_parquet(st / "clean" / "index.parquet")
    dcfg = cfg["divergences"]
    mcfg = cfg["momentum"]; thr = mcfg["move_threshold"]
    uni = set(master.loc[master["in_universe"], "symbol"])
    p = build_panels(prices, uni)
    dates = p.close.index[-days:]
    syms = [s for s in p.close.columns if p.listed.loc[dates, s].any()]
    n, m = len(dates), len(syms)
    r0, r1 = 2, n + 1                        # first / last data row
    c0, c1 = 2, m + 1                        # first / last symbol column
    cL0, cL1 = L(c0), L(c1)
    chk_rows = list(range(r1 - check + 1, r1 + 1))

    exday = prices[prices["symbol"].isin(syms) & prices["adj_event"]].pivot(index="date", columns="symbol", values="adj_event")
    exday = exday.reindex(index=dates, columns=syms).fillna(False).astype(float).replace(0.0, np.nan)

    wb = Workbook()
    ws = wb.active; ws.title = "README"
    lines = [
        "TASI Breadth — Phase 2 validation workbook (formulas reproduce Python values)",
        f"Window: last {n} trading days ({dates[0].date()} → {dates[-1].date()}), {m} universe stocks (no REITs).",
        "Blue cells = data exported from storage/clean (inputs). Black cells = formulas. Yellow = Python value to compare.",
        "Sheets: Close, Level, Volume, ExDay, Age (inputs) · Ret, SMA20, SMA50, SMA200, Max252, Min252, NHflag, NLflag (helpers)",
        "        Daily (A/D family per day) · Check (Python vs Excel for the last check dates).",
        "Definitions: see breadth/indicators/participation.py and advance_decline.py docstrings.",
        "EMA19/EMA39 in 'Daily' are seeded with the SMA of the first 19/39 rows of this window; after ~300 rows the",
        "difference to the full-history Python EMA is < 1e-3 (memory of the seed decays as (1-alpha)^t).",
        "Summation Index and A/D line are cumulative since 2002 and are validated on their daily change only.",
    ]
    for i, t in enumerate(lines):
        ws.cell(row=i + 1, column=1, value=t).font = BOLD if i == 0 else BLACK

    _grid(wb.create_sheet("Close"), dates, syms, p.close.loc[dates, syms])
    _grid(wb.create_sheet("Level"), dates, syms, p.level.loc[dates, syms])
    _grid(wb.create_sheet("Volume"), dates, syms, p.volume.loc[dates, syms])
    _grid(wb.create_sheet("ExDay"), dates, syms, exday)
    _grid(wb.create_sheet("Age"), dates, syms, p.age.loc[dates, syms])
    # SectorMap: row 2 = industry group per symbol (input)
    ws = wb.create_sheet("SectorMap"); ws["A1"] = "symbol"; ws["A2"] = "industry_group"
    grp = master.set_index("symbol")["industry_group"]
    for j, sym in enumerate(syms):
        ws.cell(row=1, column=j + 2, value=int(sym)).font = BOLD
        ws.cell(row=2, column=j + 2, value=str(grp.get(sym, ""))).font = BLUE
    # Index sheet: TASI and EW closes (inputs) + spreads and 60-day-high flags (formulas)
    ws = wb.create_sheet("Index")
    for j, h in enumerate(["date", "TASI", "EW", "spread_20", "spread_60", "TASI_60d_high", "ADline", "ADline_60d_max", "bear_ad_line"]):
        ws.cell(row=1, column=j + 1, value=h).font = BOLD
    tasi_s = idx_df[idx_df["index_code"] == "TASI"].set_index("date")["close"]
    ew_s = idx_df[idx_df["index_code"] == "EW_TASI"].set_index("date")["close"]
    N, K = dcfg["index_window"], dcfg["tolerance_days"]
    for i, d in enumerate(dates):
        r_ = i + 2
        ws.cell(row=r_, column=1, value=d.to_pydatetime()).number_format = "yyyy-mm-dd"
        ws.cell(row=r_, column=2, value=float(tasi_s.get(d, np.nan))).font = BLUE
        ws.cell(row=r_, column=3, value=float(ew_s.get(d, np.nan))).font = BLUE
        ws.cell(row=r_, column=7, value=float(ad.loc[d, "ad_line"])).font = BLUE
        if i >= 20:
            ws.cell(row=r_, column=4, value=f"=100*(C{r_}/C{r_-20}-B{r_}/B{r_-20})").font = BLACK
        if i >= 60:
            ws.cell(row=r_, column=5, value=f"=100*(C{r_}/C{r_-60}-B{r_}/B{r_-60})").font = BLACK
        if i >= N - 1:
            ws.cell(row=r_, column=6, value=f"=IF(B{r_}>=MAX(B{r_-N+1}:B{r_}),1,0)").font = BLACK
            ws.cell(row=r_, column=8, value=f"=MAX(G{r_-N+1}:G{r_})").font = BLACK
            ws.cell(row=r_, column=9, value=f"=IF(AND(F{r_}=1,MAX(G{r_-K}:G{r_})<H{r_}),1,0)").font = BLACK

    # ---- Ret (formulas for rows r0+1..r1)
    ws = wb.create_sheet("Ret"); _grid(ws, dates, syms, None)
    for i in range(r0 + 1, r1 + 1):
        for j in range(c0, c1 + 1):
            col = L(j)
            ws.cell(row=i, column=j, value=f'=IF(OR(Close!{col}{i}="",Level!{col}{i-1}="",ExDay!{col}{i}=1),"",Close!{col}{i}/Level!{col}{i-1}-1)').font = BLACK

    # ---- helper sheets (last `check` rows only)
    def helper(name, fexpr):
        ws = wb.create_sheet(name); _grid(ws, dates, syms, None)
        for i in chk_rows:
            for j in range(c0, c1 + 1):
                ws.cell(row=i, column=j, value=fexpr(L(j), i)).font = BLACK
        return ws

    for w in (20, 50, 200):
        helper(f"SMA{w}", lambda col, i, w=w: f'=IF(Age!{col}{i}>={w},AVERAGE(Level!{col}{i-w+1}:{col}{i}),"")')
    helper("Max252", lambda col, i: f'=IF(Age!{col}{i}>=253,MAX(Level!{col}{i-252}:{col}{i-1}),"")')
    helper("Min252", lambda col, i: f'=IF(Age!{col}{i}>=253,MIN(Level!{col}{i-252}:{col}{i-1}),"")')
    helper("NHflag", lambda col, i: f'=IF(AND(Close!{col}{i}<>"",Max252!{col}{i}<>""),IF(Close!{col}{i}>Max252!{col}{i},1,0),0)')
    helper("NLflag", lambda col, i: f'=IF(AND(Close!{col}{i}<>"",Min252!{col}{i}<>""),IF(Close!{col}{i}<Min252!{col}{i},1,0),0)')
    for nm, nd in (("Chg21", mcfg["month_days"]), ("Chg63", mcfg["quarter_days"]), ("Chg34", mcfg["d34_days"])):
        helper(nm, lambda col, i, nd=nd: f'=IF(OR(Close!{col}{i}="",Level!{col}{i-nd}="",Age!{col}{i}<={nd}),"",Level!{col}{i}/Level!{col}{i-nd}-1)')
    # active = traded in last 10 days ; eligible_w = Age>=w AND active  -> AboveSMAw flag
    helper("Active", lambda col, i: f'=IF(COUNT(Close!{col}{i-9}:{col}{i})>0,1,0)')
    for w in (20, 50, 200):
        helper(f"Above{w}", lambda col, i, w=w: f'=IF(AND(SMA{w}!{col}{i}<>"",Active!{col}{i}=1),IF(Level!{col}{i}>SMA{w}!{col}{i},1,0),"")')

    # ---- Daily A/D family (all rows)
    ws = wb.create_sheet("Daily")
    hdr = ["date", "A", "D", "U", "UV", "DV", "UP", "DP", "RANA", "EMA19", "EMA39", "McClellan", "TRIN", "UpVol%", "UpPts%",
           "A/(A+D)", "ZBT_EMA10", "Whaley5", "Deemer10", "UpX", "DownX", "Ratio5", "Ratio10"]
    for j, h in enumerate(hdr):
        ws.cell(row=1, column=j + 1, value=h).font = BOLD
    fast, slow = cfg["indicators"]["advance_decline"]["mcclellan_fast"], cfg["indicators"]["advance_decline"]["mcclellan_slow"]
    for i in range(r0, r1 + 1):
        d = dates[i - r0]
        ws.cell(row=i, column=1, value=d.to_pydatetime()).number_format = "yyyy-mm-dd"
        if i == r0:
            continue
        rr = f"Ret!{cL0}{i}:{cL1}{i}"; vv = f"Volume!{cL0}{i}:{cL1}{i}"
        pts = f"(Close!{cL0}{i}:{cL1}{i}-Level!{cL0}{i-1}:{cL1}{i-1})"
        ws[f"B{i}"] = f'=COUNTIF({rr},">0")'
        ws[f"C{i}"] = f'=COUNTIF({rr},"<0")'
        ws[f"D{i}"] = f'=COUNTIF({rr},0)'
        ws[f"E{i}"] = f'=SUMPRODUCT(ISNUMBER({rr})*({rr}>0)*{vv})'
        ws[f"F{i}"] = f'=SUMPRODUCT(ISNUMBER({rr})*({rr}<0)*{vv})'
        ws[f"G{i}"] = f'=SUMPRODUCT(ISNUMBER({rr})*({rr}>0)*{pts})'
        ws[f"H{i}"] = f'=-SUMPRODUCT(ISNUMBER({rr})*({rr}<0)*{pts})'
        ws[f"I{i}"] = f'=IF(B{i}+C{i}=0,"",1000*(B{i}-C{i})/(B{i}+C{i}))'
        # EMA seeded with SMA of first n RANA rows (rows r0+1 .. r0+n)
        for colL, nn in (("J", fast), ("K", slow)):
            seed_row = r0 + nn
            if i < seed_row:
                continue
            if i == seed_row:
                ws[f"{colL}{i}"] = f"=AVERAGE(I{r0+1}:I{i})"
            else:
                ws[f"{colL}{i}"] = f"={colL}{i-1}+(2/({nn}+1))*(I{i}-{colL}{i-1})"
        if i > r0 + slow:
            ws[f"L{i}"] = f"=J{i}-K{i}"
        ws[f"M{i}"] = f'=IF(OR(C{i}=0,F{i}=0,E{i}=0),"",(B{i}/C{i})/(E{i}/F{i}))'
        ws[f"N{i}"] = f'=IF(E{i}+F{i}=0,"",E{i}/(E{i}+F{i}))'
        ws[f"O{i}"] = f'=IF(G{i}+H{i}=0,"",G{i}/(G{i}+H{i}))'
        # ---- thrusts (Zweig EMA seeded with the first ratio, as pandas ewm(adjust=False))
        ws[f"P{i}"] = f'=IF(B{i}+C{i}=0,"",B{i}/(B{i}+C{i}))'
        zs = cfg["thrusts"]["zweig"]["ema_span"]
        ws[f"Q{i}"] = f"=P{i}" if i == r0 + 1 else f"=Q{i-1}+(2/({zs}+1))*(P{i}-Q{i-1})"
        wd = cfg["thrusts"]["whaley"]["days"]; dd = cfg["thrusts"]["deemer"]["days"]
        if i >= r0 + wd:
            ws[f"R{i}"] = f"=SUM(B{i-wd+1}:B{i})/SUM(C{i-wd+1}:C{i})"
        if i >= r0 + dd:
            ws[f"S{i}"] = f"=SUM(B{i-dd+1}:B{i})/SUM(C{i-dd+1}:C{i})"
        # ---- Stockbee daily movers (return >= thr with volume > previous day)
        vp = f"Volume!{cL0}{i-1}:{cL1}{i-1}"
        ws[f"T{i}"] = f'=SUMPRODUCT(ISNUMBER({rr})*({rr}>={thr}-0.000000001)*({vv}>{vp}))'
        ws[f"U{i}"] = f'=SUMPRODUCT(ISNUMBER({rr})*({rr}<=-{thr}+0.000000001)*({vv}>{vp}))'
        if i >= r0 + 5:
            ws[f"V{i}"] = f'=IF(SUM(U{i-4}:U{i})=0,"",SUM(T{i-4}:T{i})/SUM(U{i-4}:U{i}))'
        if i >= r0 + 10:
            ws[f"W{i}"] = f'=IF(SUM(U{i-9}:U{i})=0,"",SUM(T{i-9}:T{i})/SUM(U{i-9}:U{i}))'

    # ---- Check sheet
    ws = wb.create_sheet("Check")
    items = [  # (label, python column, source frame, excel formula builder)
        ("pct_above_sma20", "pct_above_sma20", part, lambda i: f'=100*SUM(Above20!{cL0}{i}:{cL1}{i})/COUNT(Above20!{cL0}{i}:{cL1}{i})'),
        ("pct_above_sma50", "pct_above_sma50", part, lambda i: f'=100*SUM(Above50!{cL0}{i}:{cL1}{i})/COUNT(Above50!{cL0}{i}:{cL1}{i})'),
        ("pct_above_sma200", "pct_above_sma200", part, lambda i: f'=100*SUM(Above200!{cL0}{i}:{cL1}{i})/COUNT(Above200!{cL0}{i}:{cL1}{i})'),
        ("new_highs", "new_highs", part, lambda i: f"=SUM(NHflag!{cL0}{i}:{cL1}{i})"),
        ("new_lows", "new_lows", part, lambda i: f"=SUM(NLflag!{cL0}{i}:{cL1}{i})"),
        ("advances", "advances", ad, lambda i: f"=Daily!B{i}"),
        ("declines", "declines", ad, lambda i: f"=Daily!C{i}"),
        ("up_volume", "up_volume", ad, lambda i: f"=Daily!E{i}"),
        ("down_volume", "down_volume", ad, lambda i: f"=Daily!F{i}"),
        ("rana", "rana", ad, lambda i: f"=Daily!I{i}"),
        ("mcclellan_osc", "mcclellan_osc", ad, lambda i: f"=Daily!L{i}"),
        ("trin", "trin", ad, lambda i: f"=Daily!M{i}"),
        ("up_volume_pct", "up_volume_pct", ad, lambda i: f"=Daily!N{i}"),
        ("up_points_pct", "up_points_pct", ad, lambda i: f"=Daily!O{i}"),
        ("zweig_value", "zweig_value", th, lambda i: f"=Daily!Q{i}"),
        ("whaley_value", "whaley_value", th, lambda i: f"=Daily!R{i}"),
        ("deemer_value", "deemer_value", th, lambda i: f"=Daily!S{i}"),
        ("up_x", "up_x", mom, lambda i: f"=Daily!T{i}"),
        ("down_x", "down_x", mom, lambda i: f"=Daily!U{i}"),
        ("ratio_5", "ratio_5", mom, lambda i: f"=Daily!V{i}"),
        ("ratio_10", "ratio_10", mom, lambda i: f"=Daily!W{i}"),
        ("up_25_m", "up_25_m", mom, lambda i: f'=SUMPRODUCT(ISNUMBER(Chg21!{cL0}{i}:{cL1}{i})*(Chg21!{cL0}{i}:{cL1}{i}>={mcfg["month_pct"]}))'),
        ("down_25_m", "down_25_m", mom, lambda i: f'=SUMPRODUCT(ISNUMBER(Chg21!{cL0}{i}:{cL1}{i})*(Chg21!{cL0}{i}:{cL1}{i}<=-{mcfg["month_pct"]}))'),
        ("up_25_q", "up_25_q", mom, lambda i: f'=SUMPRODUCT(ISNUMBER(Chg63!{cL0}{i}:{cL1}{i})*(Chg63!{cL0}{i}:{cL1}{i}>={mcfg["quarter_pct"]}))'),
        ("down_25_q", "down_25_q", mom, lambda i: f'=SUMPRODUCT(ISNUMBER(Chg63!{cL0}{i}:{cL1}{i})*(Chg63!{cL0}{i}:{cL1}{i}<=-{mcfg["quarter_pct"]}))'),
        ("up_13_34", "up_13_34", mom, lambda i: f'=SUMPRODUCT(ISNUMBER(Chg34!{cL0}{i}:{cL1}{i})*(Chg34!{cL0}{i}:{cL1}{i}>={mcfg["d34_pct"]}))'),
        ("down_13_34", "down_13_34", mom, lambda i: f'=SUMPRODUCT(ISNUMBER(Chg34!{cL0}{i}:{cL1}{i})*(Chg34!{cL0}{i}:{cL1}{i}<=-{mcfg["d34_pct"]}))'),
        ("banks_pct50", "pct50", secl[secl["sector"] == "Banks"].set_index("date"),
         lambda i: f'=100*SUMPRODUCT((SectorMap!{cL0}$2:{cL1}$2="Banks")*(Above50!{cL0}{i}:{cL1}{i}=1))/SUMPRODUCT((SectorMap!{cL0}$2:{cL1}$2="Banks")*ISNUMBER(Above50!{cL0}{i}:{cL1}{i}))'),
        ("materials_pct50", "pct50", secl[secl["sector"] == "Materials"].set_index("date"),
         lambda i: f'=100*SUMPRODUCT((SectorMap!{cL0}$2:{cL1}$2="Materials")*(Above50!{cL0}{i}:{cL1}{i}=1))/SUMPRODUCT((SectorMap!{cL0}$2:{cL1}$2="Materials")*ISNUMBER(Above50!{cL0}{i}:{cL1}{i}))'),
        ("spread_20", "spread_20", conc, lambda i: f"=Index!D{i}"),
        ("spread_60", "spread_60", conc, lambda i: f"=Index!E{i}"),
        ("index_high", "index_high", div.astype({"index_high": float}), lambda i: f"=Index!F{i}"),
        ("bear_ad_line", "bear_ad_line", div.astype({"bear_ad_line": float}), lambda i: f"=Index!I{i}"),
    ]
    ws["A1"] = "date"; ws["A1"].font = BOLD
    col = 2
    for label, _, _, _ in items:
        for k, suffix in enumerate(["python", "excel", "diff"]):
            c = ws.cell(row=1, column=col + k, value=f"{label} [{suffix}]"); c.font = BOLD
        col += 3
    for r, i in enumerate(chk_rows, start=2):
        d = dates[i - r0]
        ws.cell(row=r, column=1, value=d.to_pydatetime()).number_format = "yyyy-mm-dd"
        col = 2
        for label, pycol, frame, fx in items:
            pv = frame.loc[d, pycol]
            c = ws.cell(row=r, column=col, value=None if pd.isna(pv) else float(pv)); c.fill = YELLOW; c.font = BLUE
            ws.cell(row=r, column=col + 1, value=fx(i)).font = BLACK
            ws.cell(row=r, column=col + 2, value=f'=IF(OR({L(col)}{r}="",{L(col+1)}{r}=""),"",{L(col+1)}{r}-{L(col)}{r})').font = BLACK
            col += 3
    rmax = len(chk_rows) + 3
    ws.cell(row=rmax, column=1, value="max |diff| per item:").font = BOLD
    for k in range(len(items)):
        c = L(2 + 3 * k + 2)
        ws.cell(row=rmax, column=2 + 3 * k + 2,
                value=f"=MAX(MAX(0,{c}2:{c}{len(chk_rows)+1}),-MIN(0,{c}2:{c}{len(chk_rows)+1}))").font = BLACK
    ws.cell(row=len(chk_rows) + 4, column=1, value="Tolerance: 1e-6 for counts/ratios; 1e-3 for McClellan / Zweig EMA (seed inside the window).").font = BLACK

    out = ROOT / "storage" / "validation" ; out.mkdir(exist_ok=True)
    path = out / "Phase2_4_validation.xlsx"
    wb.save(path)
    return str(path)


# --------------------------------------------------------------------------- Phase 5: composite workbook
def build_composite(cfg: dict, check: int = 10) -> str:
    """Phase5_validation.xlsx: rolling percentiles, family scores and the Breadth Score by formula."""
    st = ROOT / cfg["paths"]["storage_dir"]
    comp = pd.read_parquet(st / "features" / "composite.parquet").set_index("date")
    c = cfg["composite"]; W, mp = c["rank_window"], c["rank_min_periods"]
    fams = list(c["families"].keys())
    metrics = [m for cols in c["families"].values() for m in cols]
    n_rows = W + check
    dates = comp.index[-n_rows:]
    wb = Workbook(); ws = wb.active; ws.title = "README"
    for i, t in enumerate([
        "Phase 5 validation — Breadth Score reproduced by formulas",
        f"Raw metrics (blue) for the last {n_rows} days; percentile = (COUNTIF(<x)+COUNTIF(<=x)+1)/2/COUNT over the trailing {W}-day window",
        "(this equals pandas rolling rank(pct=True) with average ranks for ties). Family = mean of its metric percentiles;",
        "confirmation is used as-is (already 0/50/100). Score = SUMPRODUCT(weights, families) / SUM(weights over available families).",
        "Weights (yellow) are the in-sample values from config/config.yaml.",
    ]):
        ws.cell(row=i + 1, column=1, value=t).font = BOLD if i == 0 else BLACK
    ws = wb.create_sheet("Raw")
    ws["A1"] = "date"; ws["A1"].font = BOLD
    for j, m in enumerate(metrics):
        ws.cell(row=1, column=j + 2, value=m).font = BOLD
    for i, d in enumerate(dates):
        ws.cell(row=i + 2, column=1, value=d.to_pydatetime()).number_format = "yyyy-mm-dd"
        for j, m in enumerate(metrics):
            v = comp.loc[d, f"raw_{m}"]
            if pd.notna(v):
                ws.cell(row=i + 2, column=j + 2, value=float(v)).font = BLUE
    r1 = n_rows + 1
    ws = wb.create_sheet("Score")
    hdr = ["date"] + [f"pct_{m}" for m in metrics] + fams + ["score_excel", "score_python", "diff"] + [f"{f}_python" for f in fams]
    for j, h in enumerate(hdr):
        ws.cell(row=1, column=j + 1, value=h).font = BOLD
    # weights block
    ws.cell(row=1, column=len(hdr) + 2, value="weights").font = BOLD
    wcell = {}
    for k, f in enumerate(fams):
        ws.cell(row=2 + k, column=len(hdr) + 2, value=f).font = BOLD
        cc = ws.cell(row=2 + k, column=len(hdr) + 3, value=float(c["weights"].get(f, 0))); cc.font = BLUE; cc.fill = YELLOW
        wcell[f] = f"${L(len(hdr) + 3)}${2 + k}"
    for r, i in enumerate(range(r1 - check + 1, r1 + 1), start=2):
        d = dates[i - 2]
        ws.cell(row=r, column=1, value=d.to_pydatetime()).number_format = "yyyy-mm-dd"
        col = 2
        pcol = {}
        for j, m in enumerate(metrics):
            rc = L(j + 2)
            rng = f"Raw!{rc}{i - W + 1}:{rc}{i}"; x = f"Raw!{rc}{i}"
            if m == "confirmation":
                f_ = f"={x}"
            else:
                f_ = f'=IF(OR({x}="",COUNT({rng})<{mp}),"",100*(COUNTIF({rng},"<"&{x})+COUNTIF({rng},"<="&{x})+1)/2/COUNT({rng}))'
            ws.cell(row=r, column=col, value=f_).font = BLACK
            pcol[m] = f"{L(col)}{r}"; col += 1
        fcol = {}
        for f in fams:
            cells = ",".join(pcol[m] for m in c["families"][f])
            ws.cell(row=r, column=col, value=f'=IF(COUNT({cells})=0,"",AVERAGE({cells}))').font = BLACK
            fcol[f] = f"{L(col)}{r}"; col += 1
        num = "+".join(f"IF(ISNUMBER({fcol[f]}),{wcell[f]}*{fcol[f]},0)" for f in fams)
        den = "+".join(f"IF(ISNUMBER({fcol[f]}),{wcell[f]},0)" for f in fams)
        ws.cell(row=r, column=col, value=f"=({num})/({den})").font = BLACK; sc_x = f"{L(col)}{r}"; col += 1
        pv = comp.loc[d, "score"]
        cc = ws.cell(row=r, column=col, value=float(pv)); cc.fill = YELLOW; cc.font = BLUE; sc_p = f"{L(col)}{r}"; col += 1
        ws.cell(row=r, column=col, value=f"={sc_x}-{sc_p}").font = BLACK; col += 1
        for f in fams:
            v = comp.loc[d, f]
            cc = ws.cell(row=r, column=col, value=None if pd.isna(v) else float(v)); cc.fill = YELLOW; cc.font = BLUE; col += 1
    dcol = L(2 + len(metrics) + len(fams) + 2)
    ws.cell(row=check + 3, column=1, value="max |diff|").font = BOLD
    ws.cell(row=check + 3, column=2, value=f"=MAX(MAX(0,{dcol}2:{dcol}{check+1}),-MIN(0,{dcol}2:{dcol}{check+1}))").font = BLACK
    out = ROOT / "storage" / "validation" / "Phase5_validation.xlsx"
    wb.save(out)
    return str(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=320)
    ap.add_argument("--check", type=int, default=10)
    ap.add_argument("--composite", action="store_true", help="build Phase5_validation.xlsx instead")
    a = ap.parse_args()
    if a.composite:
        print(build_composite(load_config(), a.check))
    else:
        print(build(load_config(), a.days, a.check))


