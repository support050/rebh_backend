"""Professional breadth dashboard (single offline HTML file, StockCharts-style multi-panel charts).

python -m breadth.reports.pro_dashboard -> storage/reports/breadth_dashboard.html

Structure: tabs (Overview · New Highs/Lows · McClellan · % Above MAs · Advance/Decline · Stockbee ·
Sectors · Concentration · Signals · Thresholds). Every chart page = synchronized panels (index on top,
breadth panels below), crosshair + tooltip, range buttons (6M/1Y/2Y/5Y/10Y/All), drag-to-pan, wheel-zoom,
threshold zones, canary episodes shaded, signal markers. Rendered on <canvas> by an embedded JS engine
(no external libraries, works offline, never published). Theme: dark (default) / light toggle.
Reference for panel layout and thresholds: Hisham Naser's NH-NL / McClellan / %>MA notes, calibrated to TASI.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from breadth import ROOT, load_config
from breadth.reports.canary_study import episodes as canary_episodes, table as canary_table

TEMPLATE = ROOT / "breadth" / "reports" / "pro_dashboard_template.html"


def first_of_run_mask(x: pd.Series) -> pd.Series:
    from breadth.indicators.thrusts import first_of_run
    return first_of_run(x, 36)


def _s(x: pd.Series, nd=2):
    """Series -> list with None for NaN, rounded."""
    v = x.to_numpy(dtype=float)
    return [None if np.isnan(a) else round(float(a), nd) for a in v]


def build(cfg: dict, eps_growth: dict | None = None) -> str:
    st = ROOT / cfg["paths"]["storage_dir"]
    F = {n: pd.read_parquet(st / "features" / f"{n}.parquet") for n in
         ["participation", "advance_decline", "thrusts", "momentum", "sectors", "sectors_summary", "concentration", "divergences", "composite"]}
    for k in F:
        if k != "sectors":
            F[k] = F[k].set_index("date")
    idx = pd.read_parquet(st / "clean" / "index.parquet")
    tasi = idx[idx["index_code"] == "TASI"].set_index("date")[["open", "high", "low", "close", "volume"]]
    ew = idx[idx["index_code"] == "EW_TASI"].set_index("date")["close"]
    part, ad, th, mom, sec, secs, conc, div, comp = (F[k] for k in ["participation", "advance_decline", "thrusts", "momentum", "sectors", "sectors_summary", "concentration", "divergences", "composite"])
    master = pd.read_parquet(st / "master" / "symbols.parquet")

    dates = part.index[part.index >= pd.Timestamp("2002-01-01")]
    D = {"dates": [d.strftime("%Y-%m-%d") for d in dates]}
    T = tasi.reindex(dates)
    S = {}
    S["tasi"] = _s(T["close"]); S["tasi_o"] = _s(T["open"]); S["tasi_h"] = _s(T["high"]); S["tasi_l"] = _s(T["low"]); S["tasi_vol"] = _s(T["volume"], 0)
    S["ew"] = _s(ew.reindex(dates))
    for c in ["pct_above_ema10", "pct_above_sma20", "pct_above_sma50", "pct_above_sma150", "pct_above_sma200", "pct_stage2", "pct_stage4", "pct_minervini", "n_minervini",
              "new_highs", "new_lows", "net_nh_nl", "hl_index", "n_nhnl", "n_sma200", "new_highs_vol"]:
        S[c] = _s(part[c].reindex(dates))
    S["nhnl_sum5"] = _s(part["net_nh_nl"].rolling(5).sum().reindex(dates))
    S["nhnl_sum10"] = _s(part["net_nh_nl"].rolling(10).sum().reindex(dates))
    S["nhnl_thr"] = _s((-0.75 * part["n_nhnl"].rolling(5).mean()).reindex(dates))
    for c in ["advances", "declines", "ad_line", "ad_ratio", "ud_volume_ratio", "net_up_volume", "up_volume_pct", "up_points_pct", "rana", "mcclellan_osc", "mcclellan_sum", "trin", "trin_10", "trin_open10"]:
        S[c] = _s(ad[c].reindex(dates))
    S["ad_net"] = _s(ad["ad_net"].reindex(dates))
    for c in ["zweig_value", "whaley_value", "deemer_value"]:
        S[c] = _s(th[c].reindex(dates), 3)
    for c in ["lowry_down_points", "lowry_down_volume"]:
        S[c] = _s(th[c].astype(float).reindex(dates), 0)
    # weekly Weinstein stages (his original 30-week setting) mapped onto trading days (step function, no look-ahead)
    wkst = pd.read_parquet(st / "features" / "stages_weekly.parquet").set_index("date")
    for k in (1, 2, 3, 4):
        S[f"pct_stage{k}_w"] = _s(wkst[f"pct_stage{k}_w"].reindex(dates, method="ffill"), 1)
    S["n_stage_w"] = _s(wkst["n_stage_w"].reindex(dates, method="ffill"), 0)
    S["pct_above_sma100"] = _s(part["pct_above_sma100"].reindex(dates))
    # classic library + Bullish Percent
    from breadth.indicators.classic import VERDICTS
    clsc = pd.read_parquet(st / "features" / "classic.parquet").set_index("date")
    for c in ["hlli", "abi", "sto_b", "itbm", "itbm_signal", "mvo", "mvo_sum", "nh_pct_10", "haurlan_st", "ad_pct_21"]:
        S[c] = _s(clsc[c].reindex(dates), 2)
    for c in ["cvi", "cvi_ma50"]:
        S[c] = _s((clsc[c] / 1e6).reindex(dates), 1)          # millions of shares
    for c in ["hindenburg", "hindenburg_confirmed", "titanic", "zweig_91", "zweig_double_91"]:
        S[c] = _s(clsc[c].astype(float).reindex(dates), 0)
    bp = pd.read_parquet(st / "features" / "bpi.parquet").set_index("date")
    S["bpi"] = _s(bp["bpi"].reindex(dates), 1); S["pnf_buy"] = _s(bp["pnf_buy"].reindex(dates), 0); S["pnf_sell"] = _s(bp["pnf_sell"].reindex(dates), 0)
    S["bpi_status"] = [None if (pd.isna(v) or v == "") else v for v in bp["bpi_status"].reindex(dates)]
    D["verdicts"] = {k: {"author": a, "result": r, "tag": t} for k, (a, r, t) in VERDICTS.items()}
    def qs(x):
        x = x.loc["2007":].dropna(); return {p_: round(float(x.quantile(p_)), 2) for p_ in (.02, .05, .5, .95, .98)}
    D["classic_q"] = {c: qs(clsc[c]) for c in ["hlli", "abi", "sto_b", "itbm", "mvo", "mvo_sum", "nh_pct_10", "haurlan_st", "ad_pct_21"]}
    D["classic_q"]["bpi"] = qs(bp["bpi"])
    for c in ["up_x", "down_x", "ratio_5", "ratio_10", "up_25_q", "down_25_q", "up_25_m", "down_25_m", "up_50_m", "down_50_m", "up_13_34", "down_13_34", "pct_up_x", "pct_down_x", "n_mom"]:
        S[c] = _s(mom[c].reindex(dates))
    for c in ["n_sectors", "n_healthy", "n_weak", "pct_healthy", "median_score"]:
        S[c] = _s(secs[c].reindex(dates))
    for c in ["spread_20", "spread_60", "ratio", "ratio_ma", "top5_share", "top5_contrib", "cw_proxy_ret"]:
        S[c] = _s(conc[c].reindex(dates), 3)
    for c in ["score", "score_smooth", "overlay", "A_participation", "B_advance_decline", "E_sectors"]:
        S[c] = _s(comp[c].reindex(dates))
    S["regime"] = [None if pd.isna(v) else v for v in comp["regime"].reindex(dates)]
    wd = pd.read_parquet(st / "features" / "widget.parquet").set_index("date")
    for c in ["up_from_open", "down_from_open", "up_on_volume", "down_on_volume", "stage1", "stage2", "stage3", "stage4", "n_stage"]:
        S[c] = _s(wd[c].reindex(dates), 0)
    for k in (1, 2, 3, 4):
        S[f"pct_stage{k}"] = _s((100 * wd[f"stage{k}"] / wd["n_stage"].replace(0, np.nan)).reindex(dates), 1)
    # index overlays (StockCharts classic view) + traditional McClellan + Deepvue-style market cycle count
    tc = tasi["close"]
    S["tasi_ema21"] = _s(tc.ewm(span=21, adjust=False, min_periods=21).mean().reindex(dates))
    S["tasi_sma50"] = _s(tc.rolling(50, min_periods=50).mean().reindex(dates))
    S["tasi_sma21"] = _s(tc.rolling(21, min_periods=21).mean().reindex(dates))
    S["tasi_sma200"] = _s(tc.rolling(200, min_periods=200).mean().reindex(dates))
    S["tasi_chg"] = _s(tc.pct_change().reindex(dates), 4)
    adn = ad["ad_net"]
    S["mcclellan_trad"] = _s((adn.ewm(span=19, adjust=False).mean() - adn.ewm(span=39, adjust=False).mean()).reindex(dates))
    def cycle_count(ma_len):
        sma = tc.rolling(ma_len, min_periods=ma_len).mean()
        above = (tc > sma)
        cnt = []; c = 0; prev = None
        for a, ok in zip(above.to_numpy(), sma.notna().to_numpy()):
            if not ok:
                cnt.append(np.nan); continue
            if prev is None or a != prev:
                c = 1 if a else -1
            else:
                c = c + 1 if a else c - 1
            prev = a; cnt.append(c)
        cyc = pd.Series(cnt, index=tc.index)
        # stress test: a dip below the MA lasting <= 3 days that ends within the first 60 days of an uptrend
        stress = []
        vals = cyc.to_numpy(); ix = cyc.index
        for i in range(1, len(vals)):
            if vals[i] == 1 and i >= 2:
                j = i - 1; dip = 0
                while j >= 0 and vals[j] < 0: dip += 1; j -= 1
                if 1 <= dip <= 3 and j >= 0 and 0 < vals[j] <= 60:
                    stress.append(ix[i].strftime("%Y-%m-%d"))
        return sma, cyc, stress
    _, cyc21, stress21 = cycle_count(21)
    sma50c, cyc50, stress50 = cycle_count(50)
    S["dv_cycle"] = _s(cyc21.reindex(dates), 0); S["dv_cycle50"] = _s(cyc50.reindex(dates), 0)
    stress = stress21
    D["series"] = S
    D["dv_stress"] = stress

    # ---- events
    def ev_dates(mask):
        return [d.strftime("%Y-%m-%d") for d in mask.index[mask.fillna(False).astype(bool)] if d in dates]
    E = {"zweig": ev_dates(th["zweig_signal"]), "whaley": ev_dates(th["whaley_signal"]), "pct50": ev_dates(th["pct50_thrust_signal"]),
         "lowry_down": ev_dates(th["lowry_down_signal"]), "bear_div": ev_dates(div["bearish_signal"]), "bull_div": ev_dates(div["bullish_signal"]),
         "day90_down": ev_dates(ad["day90_down_vol"]), "day90_up": ev_dates(ad["day90_up_vol"])}
    reg_chg = comp["regime"][comp["regime"] != comp["regime"].shift()].dropna()
    E["regime_changes"] = [{"date": d.strftime("%Y-%m-%d"), "regime": r} for d, r in reg_chg.items()]
    # canary episodes (Hisham rule calibrated to TASI)
    eps = canary_episodes(part["net_nh_nl"], part["n_nhnl"], tasi["close"], 75.0)
    ctab = canary_table(eps, part["net_nh_nl"], tasi)
    nn_ = lambda v: None if pd.isna(v) else float(v)
    E["canary"] = [{"start": str(r["Start Date"]), "end": str(r["End Date"]), "c0": nn_(r["Close"]), "c1": nn_(r["Close "]), "pct": nn_(r["% Change"]), "days": int(r["No. of Trading Days"]),
                    "max_read": int(r["Max Reading"]), "sum5": int(r["Max Reading (5d sum)"]), "low": nn_(r["Lowest Low"]), "dd": nn_(r["% MAX DD"]), "open": bool(pd.Timestamp(r["End Date"]) >= part.index[-1])} for _, r in ctab.iterrows()]
    E["dv_stress"] = D.pop("dv_stress"); E["dv_stress50"] = stress50
    for k, col in [("zweig_rare", "zweig_rare_signal"), ("deemer_rare", "deemer_rare_signal"), ("lowry_seq", "lowry_seq_signal"), ("lowry_up", "lowry_up_signal")]:
        E[k] = ev_dates(th[col])
    E["hindenburg"] = ev_dates(clsc["hindenburg_signal"]); E["hindenburg_confirmed"] = ev_dates(first_of_run_mask(clsc["hindenburg_confirmed"]))
    E["titanic"] = ev_dates(clsc["titanic_signal"]); E["zweig_double_91"] = ev_dates(clsc["zweig_double_91_signal"])
    E["mc_bear"] = ev_dates(div["bear_mcclellan_sum_signal"]); E["mc_bull"] = ev_dates(div["bull_mcclellan_sum_signal"])
    E["ad_bear"] = ev_dates(div["bear_ad_line_signal"]); E["ad_bull"] = ev_dates(div["bull_ad_line_signal"])
    D["events"] = E

    # ---- thrust events with forward returns
    tc = tasi["close"]
    fwd = {h: 100 * (tc.shift(-h) / tc - 1) for h in (5, 20, 60, 120)}
    SIGDEF = [("Zweig", "zweig_signal", "20d", "scored"), ("Whaley 5d", "whaley_signal", "20d", "scored"), ("%>50DMA thrust", "pct50_thrust_signal", "60d", "scored"),
              ("Lowry 95% down", "lowry_down_signal", "60d", "scored"), ("Zweig rare (≥0.66)", "zweig_rare_signal", "—", "tested: no edge"),
              ("Deemer rare (≥2.6)", "deemer_rare_signal", "60d?", "indicative (n<10)"), ("Lowry sequence", "lowry_seq_signal", "—", "tested: no edge"),
              ("Deemer 1.97", "deemer_signal", "—", "dropped"), ("Lowry 95% up", "lowry_up_signal", "—", "dropped")]
    rows = []; stats = []
    for name, col, hz, status in SIGDEF:
        ds = [d for d in th.index[th[col].fillna(False).astype(bool)] if d >= pd.Timestamp("2007-01-01")]
        for d in ds:
            rows.append({"signal": name, "date": d.strftime("%Y-%m-%d"), "tasi": None if d not in tc.index else round(float(tc[d]), 0), "horizon": hz, "status": status,
                         **{f"f{h}": (None if d not in tc.index or pd.isna(fwd[h].get(d, np.nan)) else round(float(fwd[h][d]), 1)) for h in (5, 20, 60, 120)}})
        st_row = {"signal": name, "n": len(ds), "horizon": hz, "status": status}
        for h in (20, 60, 120):
            x = pd.Series([fwd[h].get(d, np.nan) for d in ds]).dropna()
            st_row[f"mean{h}"] = None if x.empty else round(float(x.mean()), 1); st_row[f"hit{h}"] = None if x.empty else round(100 * float((x > 0).mean()), 0)
        stats.append(st_row)
    D["signals_table"] = sorted(rows, key=lambda r: r["date"], reverse=True)
    D["signal_stats"] = stats

    # ---- sectors (last 30 days heatmap + today detail)
    last = secs.index[-30:]
    piv = sec[sec["date"].isin(last)].pivot(index="sector", columns="date", values="score")
    piv = piv.sort_values(piv.columns[-1], ascending=False)
    today = sec[sec["date"] == secs.index[-1]].set_index("sector")
    D["sectors"] = {"dates": [c.strftime("%m-%d") for c in piv.columns], "rows": [
        {"sector": s_, "scores": [None if np.isnan(v) else round(float(v), 0) for v in r.values],
         **({"n": int(today.loc[s_, "n"]), "pct50": round(float(today.loc[s_, "pct50"]), 0), "pct200": round(float(today.loc[s_, "pct200"]), 0),
             "nhnl": round(float(today.loc[s_, "nhnl"]), 0), "ad10": round(float(today.loc[s_, "ad10"]), 0), "mom10": None if pd.isna(today.loc[s_, "mom10"]) else round(float(today.loc[s_, "mom10"]), 0)}
            if s_ in today.index else {})} for s_, r in piv.iterrows()]}
    _heat_rows = D["sectors"]["rows"]   # RS fields are merged in after the group RS is computed below

    # ---- sector performance: equal-weight index per industry group -> Theme Tracker + Industry Ranks
    from breadth.indicators.common import build_panels
    prices = pd.read_parquet(st / "clean" / "prices.parquet")
    uni = set(master.loc[master["in_universe"], "symbol"])
    pn = build_panels(prices, uni)
    grp = master.set_index("symbol")["industry_group"].astype("string").fillna("Unknown")
    g = grp.reindex(pn.ret.columns)
    gret = pn.ret.T.groupby(g.values).mean().T                       # daily EW return per group
    gcnt = pn.ret.notna().T.groupby(g.values).sum().T                # stocks with a return that day
    gidx = (1 + gret.fillna(0)).cumprod()
    last = gidx.index[-1]
    ytd0 = gidx.index[gidx.index < pd.Timestamp(f"{last.year}-01-01")]
    H = {"Today": 1, "1W": 5, "1M": 21, "3M": 63, "6M": 126, "12M": 252}
    def perf(asof_pos):
        row = {}
        for k, h in H.items():
            a = gidx.iloc[asof_pos]; b = gidx.iloc[asof_pos - h]
            row[k] = 100 * (a / b - 1)
        row["YTD"] = 100 * (gidx.iloc[asof_pos] / gidx.loc[ytd0[-1]] - 1) if len(ytd0) else np.nan
        return pd.DataFrame(row)
    now = perf(len(gidx) - 1); prev = perf(len(gidx) - 1 - 21)
    keep = [c for c in now.index if gcnt.iloc[-1][c] >= 3 and c != "Unknown"]
    now, prev = now.loc[keep], prev.loc[keep]
    ranks_now = now.rank(ascending=False).astype(int); ranks_prev = prev.rank(ascending=False).astype(int)
    # group RS rating 1-99: 2x3M + 6M + 9M + 12M return of the group EW index, percentile-ranked across groups each day
    gi = gidx[keep]
    gscore = 2 * (gi / gi.shift(63) - 1) + (gi / gi.shift(126) - 1) + (gi / gi.shift(189) - 1) + (gi / gi.shift(252) - 1)
    grs = (gscore.rank(axis=1, pct=True) * 99).round()
    def grs_row(name):
        if name not in grs.columns or pd.isna(grs[name].iloc[-1]): return {"rs": None, "rs21": None, "rsh": None}
        h = grs[name].iloc[-30:]
        return {"rs": int(grs[name].iloc[-1]), "rs21": None if pd.isna(grs[name].iloc[-22]) else int(grs[name].iloc[-1] - grs[name].iloc[-22]),
                "rsh": [None if pd.isna(v) else int(v) for v in h.to_numpy()]}
    D["sector_perf"] = {"periods": list(H.keys()) + ["YTD"], "rows": [
        {"name": sname, **{k: round(float(now.loc[sname, k]), 2) for k in now.columns}, **grs_row(sname),
         "rank": {k: int(ranks_now.loc[sname, k]) for k in now.columns}, "rank_chg": {k: int(ranks_prev.loc[sname, k] - ranks_now.loc[sname, k]) for k in now.columns},
         "stocks": int(gcnt.iloc[-1][sname])} for sname in keep]}
    for r_ in _heat_rows:
        r_.update(grs_row(r_["sector"]))
    # market EW for reference
    ewr = pn.ret.mean(axis=1); ewi = (1 + ewr.fillna(0)).cumprod()
    D["sector_perf"]["market"] = {k: round(100 * float(ewi.iloc[-1] / ewi.iloc[-1 - h] - 1), 2) for k, h in H.items()}
    D["sector_perf"]["market"]["YTD"] = round(100 * float(ewi.iloc[-1] / ewi.loc[ytd0[-1]] - 1), 2) if len(ytd0) else None

    # ---- per-stock snapshot for sector drill-down (latest day) + leaders + Stockbee name lists + OHLC for stock charts
    from breadth.indicators.participation import trend_template, rs_rating, _active
    lvl = pn.level; last_i = -1
    active_m = _active(pn)
    tt_mask, tt_elig, _ = trend_template(pn, cfg, active_m)
    rs = rs_rating(pn, active_m)
    ma50s = lvl.rolling(50, min_periods=50).mean(); ma200s = lvl.rolling(200, min_periods=200).mean()
    ma150s = lvl.rolling(150, min_periods=150).mean()
    ma50 = ma50s.iloc[last_i]; ma200 = ma200s.iloc[last_i]
    rising = (ma150s > ma150s.shift(20)).iloc[last_i]; falling = (ma150s < ma150s.shift(20)).iloc[last_i]; ma150 = ma150s.iloc[last_i]
    hi52s = lvl.rolling(252, min_periods=252).max(); lo52s = lvl.rolling(252, min_periods=252).min()
    hi52 = hi52s.iloc[last_i]; lo52 = lo52s.iloc[last_i]
    cl = lvl.iloc[last_i]; r1 = pn.ret.iloc[last_i]; r21 = lvl.iloc[last_i] / lvl.iloc[last_i - 21] - 1; r63 = lvl.iloc[last_i] / lvl.iloc[last_i - 63] - 1
    vol = pn.volume.iloc[last_i]; vavg20 = pn.volume.where(pn.close.notna()).rolling(20, min_periods=10).mean(); vavg = vavg20.iloc[last_i - 1]
    vavg50 = pn.volume.where(pn.close.notna()).rolling(50, min_periods=20).mean().shift(1)
    traded = pn.close.iloc[last_i].notna()
    # new high on volume today (Minervini)
    prior_max = hi52s.shift(1).iloc[last_i]
    nh_today = (pn.close.iloc[last_i] > prior_max) & traded
    nhv_today = nh_today & (vol > vavg)
    # Weinstein breakout volume: last cross of the level above MA150 within 250 days -> volume that day / 50d avg
    cross = (lvl > ma150s) & (lvl.shift(1) <= ma150s.shift(1))
    tail = cross.iloc[-250:]
    bo_ratio = {}
    for sym in tail.columns:
        idxs = np.where(tail[sym].to_numpy())[0]
        if len(idxs):
            d = tail.index[idxs[-1]]
            v50 = vavg50.at[d, sym]
            bo_ratio[sym] = None if (pd.isna(v50) or v50 == 0) else round(float(pn.volume.at[d, sym] / v50), 2)
    names = master.set_index("symbol")["name_last"].astype(str)
    def stage(sym):
        if pd.isna(ma150.get(sym)): return None
        ab = cl[sym] > ma150[sym]
        return 2 if (ab and rising[sym]) else 3 if ab else 4 if falling[sym] else 1
    def f(v, nd=1, mult=1.0):
        return None if pd.isna(v) else round(mult * float(v), nd)
    stocks = {}; allrows = []
    active_syms = set(master.loc[master["status"] == "active", "symbol"])
    tt_last = tt_mask.iloc[last_i]; rs_last = rs.iloc[last_i]; rs_5 = rs.iloc[last_i - 5]; rs_21 = rs.iloc[last_i - 21]
    rs_hist = rs.iloc[-30:]   # 30-day RS history per stock for the sparkline
    for sym in pn.ret.columns:
        if sym not in active_syms:
            continue
        gname = str(grp.get(sym, "Unknown"))
        row = {"sym": int(sym), "name": names.get(sym, ""), "group": gname, "close": f(cl[sym], 2),
               "d1": f(r1[sym], 2, 100), "m1": f(r21[sym], 1, 100), "m3": f(r63[sym], 1, 100),
               "vs50": f(cl[sym] / ma50[sym] - 1, 1, 100) if not pd.isna(ma50.get(sym)) else None,
               "vs200": f(cl[sym] / ma200[sym] - 1, 1, 100) if not pd.isna(ma200.get(sym)) else None,
               "hi52": f(cl[sym] / hi52[sym] - 1, 1, 100) if not pd.isna(hi52.get(sym)) else None,
               "lo52": f(cl[sym] / lo52[sym] - 1, 1, 100) if not pd.isna(lo52.get(sym)) else None,
               "stage": stage(sym), "rvol": None if (pd.isna(vavg.get(sym)) or vavg[sym] == 0) else round(float(vol[sym] / vavg[sym]), 2),
               "rs": None if pd.isna(rs_last.get(sym)) else int(rs_last[sym]),
               "rs5": None if (pd.isna(rs_last.get(sym)) or pd.isna(rs_5.get(sym))) else int(rs_last[sym] - rs_5[sym]),
               "rs21": None if (pd.isna(rs_last.get(sym)) or pd.isna(rs_21.get(sym))) else int(rs_last[sym] - rs_21[sym]),
               "rsh": [None if pd.isna(v) else int(v) for v in rs_hist[sym].to_numpy()] if sym in rs_hist.columns else None, "tt": bool(tt_last.get(sym, False)),
               "nh": bool(nh_today.get(sym, False)), "nhv": bool(nhv_today.get(sym, False)), "bo": bo_ratio.get(sym),
               "traded": bool(traded[sym])}
        stocks.setdefault(gname, []).append(row); allrows.append(row)
    for k in stocks:
        stocks[k].sort(key=lambda r: (r["m1"] is None, -(r["m1"] or 0)))
    D["stocks"] = stocks
    D["leaders"] = sorted([r for r in allrows if r["tt"]], key=lambda r: -(r["rs"] or 0))
    D["nh_vol_list"] = sorted([r for r in allrows if r["nhv"]], key=lambda r: -(r["rvol"] or 0))
    # heatmap row -> pooled name mapping (groups with <5 eligible names are shown as "Other")
    D["sector_pool"] = {k: ("Other" if len(v) < 5 else k) for k, v in stocks.items()}
    # Stockbee name lists for the last 15 days (click a number -> names)
    mcfg = cfg["momentum"]; thr = mcfg["move_threshold"]
    rr = pn.ret.round(8); volup = pn.volume > pn.volume.shift(1)
    def chg(n): return lvl / lvl.shift(n) - 1
    okq = (pn.age > mcfg["quarter_days"]) & pn.close.notna(); okm = (pn.age > mcfg["month_days"]) & pn.close.notna(); ok34 = (pn.age > mcfg["d34_days"]) & pn.close.notna()
    masks = {"up_x": (rr >= thr) & volup, "down_x": (rr <= -thr) & volup,
             "up_25_q": (chg(mcfg["quarter_days"]) >= mcfg["quarter_pct"]) & okq, "down_25_q": (chg(mcfg["quarter_days"]) <= -mcfg["quarter_pct"]) & okq,
             "up_25_m": (chg(mcfg["month_days"]) >= mcfg["month_pct"]) & okm, "down_25_m": (chg(mcfg["month_days"]) <= -mcfg["month_pct"]) & okm,
             "up_50_m": (chg(mcfg["month_days"]) >= mcfg["month_big_pct"]) & okm, "down_50_m": (chg(mcfg["month_days"]) <= -mcfg["month_big_pct"]) & okm,
             "up_13_34": (chg(mcfg["d34_days"]) >= mcfg["d34_pct"]) & ok34, "down_13_34": (chg(mcfg["d34_days"]) <= -mcfg["d34_pct"]) & ok34}
    sb = {}
    for d in lvl.index[-15:]:
        sb[d.strftime("%Y-%m-%d")] = {k: [int(x) for x in m.columns[m.loc[d].fillna(False).to_numpy(dtype=bool)]] for k, m in masks.items()}
    D["sb_lists"] = sb
    D["names"] = {int(k): v for k, v in names.items() if k in active_syms}
    # OHLCV (adjusted) for the last 260 trading days of every active stock -> in-page stock chart
    win = lvl.index[-260:]
    D["ohlc_dates"] = [d.strftime("%Y-%m-%d") for d in win]
    sub = prices[prices["symbol"].isin(active_syms) & prices["date"].isin(win)]
    oh = {}
    m50w = ma50s.reindex(win); m200w = ma200s.reindex(win); m150w = ma150s.reindex(win)
    nanw = pd.Series(np.nan, index=win)
    for sym, g in sub.groupby("symbol"):
        g = g.set_index("date").reindex(win)
        ma_ = lambda M: M[sym] if sym in M.columns else nanw
        oh[int(sym)] = {"o": _s(g["open_adj"], 2), "h": _s(g["high_adj"], 2), "l": _s(g["low_adj"], 2), "c": _s(g["close_adj"], 2), "v": _s(g["volume_adj"], 0),
                        "m50": _s(ma_(m50w), 2), "m150": _s(ma_(m150w), 2), "m200": _s(ma_(m200w), 2)}
    D["ohlc"] = oh
    D["tasi_win"] = _s(tasi["close"].reindex(win), 0)

    # ---- thresholds (TASI percentiles 2007+) — Hisham-style extremes table
    m = ad.loc["2007":]; nn = part["net_nh_nl"].loc["2007":]
    def q(x, p): return round(float(x.quantile(p)), 1)
    D["thresholds"] = [
        {"indicator": "NH−NL daily net", "p2": q(nn, .02), "p5": q(nn, .05), "p95": q(nn, .95), "p98": q(nn, .98), "us": "Hisham: −180/−200 (5d sum) warning; −1,800 extreme"},
        {"indicator": "NH−NL 5-day sum", "p2": q(nn.rolling(5).sum(), .02), "p5": q(nn.rolling(5).sum(), .05), "p95": q(nn.rolling(5).sum(), .95), "p98": q(nn.rolling(5).sum(), .98), "us": "canary rule (TASI): ≤ −75% of eligible names"},
        {"indicator": "McClellan Oscillator (ratio-adj.)", "p2": q(m["mcclellan_osc"], .02), "p5": q(m["mcclellan_osc"], .05), "p95": q(m["mcclellan_osc"], .95), "p98": q(m["mcclellan_osc"], .98), "us": "StockCharts ±50–75 · Refinitiv NYSE +390/−400"},
        {"indicator": "McClellan Summation", "p2": q(m["mcclellan_sum"], .02), "p5": q(m["mcclellan_sum"], .05), "p95": q(m["mcclellan_sum"], .95), "p98": q(m["mcclellan_sum"], .98), "us": "Hisham (own data): +2,000 / −10,000"},
        {"indicator": "TRIN-10", "p2": q(m["trin_10"], .02), "p5": q(m["trin_10"], .05), "p95": q(m["trin_10"], .95), "p98": q(m["trin_10"], .98), "us": ">1.2 oversold (TASI OOS +2.0%/20d)"},
        {"indicator": "%>200DMA", "p2": q(part["pct_above_sma200"].loc["2007":], .02), "p5": q(part["pct_above_sma200"].loc["2007":], .05), "p95": q(part["pct_above_sma200"].loc["2007":], .95), "p98": q(part["pct_above_sma200"].loc["2007":], .98), "us": "Hisham: 80 optimism peak / 20 pessimism peak — TASI: 60–80 topping zone, >80 best"},
        {"indicator": "%>50DMA", "p2": q(part["pct_above_sma50"].loc["2007":], .02), "p5": q(part["pct_above_sma50"].loc["2007":], .05), "p95": q(part["pct_above_sma50"].loc["2007":], .95), "p98": q(part["pct_above_sma50"].loc["2007":], .98), "us": "thrust 20→55 in 15d"},
    ]
    # ---- meta / today
    c = comp.dropna(subset=["score"]); d0, d1 = c.index[-1], c.index[-2]
    D["today"] = {"date": d0.strftime("%Y-%m-%d"), "score": round(float(c.loc[d0, "score"]), 1), "score_chg": round(float(c.loc[d0, "score"] - c.loc[d1, "score"]), 1),
                  "smooth": round(float(c.loc[d0, "score_smooth"]), 1), "regime": c.loc[d0, "regime"], "driver": str(c.loc[d0, "driver"]).replace("_", " "),
                  "overlay": round(float(c.loc[d0, "overlay"]), 1), "tasi": (round(float(tc[d0]), 2) if d0 in tc.index else None), "tasi_chg": (round(100 * float(tc[d0] / tc[d1] - 1), 2) if (d0 in tc.index and d1 in tc.index) else None),
                  "tasi_last_date": tc.dropna().index.max().strftime("%Y-%m-%d"), "index_stale": bool(d0 not in tc.index),
                  "regime_since": reg_chg.index[-1].strftime("%Y-%m-%d"), "n_universe": int(master["in_universe"].sum()),
                  "fam": {"A": round(float(c.loc[d0, "A_participation"]), 0), "B": round(float(c.loc[d0, "B_advance_decline"]), 0), "E": round(float(c.loc[d0, "E_sectors"]), 0)},
                  "hlli": round(float(clsc.loc[d0, "hlli"]), 2), "hindenburg_recent": int(clsc["hindenburg"].loc[:d0].tail(36).sum()), "bpi": round(float(bp.loc[d0, "bpi"]), 1), "bpi_status": str(bp.loc[d0, "bpi_status"])}

    # ---- Guru Stock Scans: an INDEPENDENT leaderboard payload (9 columns + RS/Stage
    # badges only). compute_guru works on the same `pn` Panels, so its RS/stage match
    # the Leaders rows by construction. eps_growth (optional, injected by breadth_job
    # from app.services.eps_growth_service) gates the TML column; without it TML is
    # empty but every other column still lands. A failure here must never poison the
    # proven snapshot, so the error is recorded in-band and read back by the API.
    from breadth.indicators.guru import compute_guru
    try:
        guru = compute_guru(pn, cfg, eps_growth)
        for sym, row in guru["rows"].items():
            row["name"] = str(names.get(int(sym), ""))
        D["guru"] = guru
    except Exception as exc:                                   # noqa: BLE001
        D["guru"] = {"error": f"{type(exc).__name__}: {str(exc)[:180]}"}
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/", "const DATA=" + json.dumps(D, separators=(",", ":")) + ";")
    out = st / "reports"; out.mkdir(exist_ok=True)
    p = out / "breadth_dashboard.html"; p.write_text(html, encoding="utf-8")
    return str(p)


if __name__ == "__main__":
    print(build(load_config()))
