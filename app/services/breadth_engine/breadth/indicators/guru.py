# -*- coding: utf-8 -*-
"""Guru Stock Scans — nine per-stock membership scans on the newest trading day.

Reproduces the "Guru" scanner (Qullamaggie / Minervini / Stockbee / O'Neil) for
TASI. Every scan is a boolean day x symbol panel built from the SAME Panels the
participation/momentum families use, so day T only ever sees rows <= T and no
price is re-fetched. The heavy primitives (Minervini trend template, IBD RS
rating, Weinstein stage) are IMPORTED from participation.py, never re-derived,
so a stock's RS / stage here is identical to the Leaders page by construction.

Design notes tied to the four review checkpoints:
  * Qullamaggie pivot uses the PRIOR 52-week high — `hi52.shift(1)` drops today
    from the window, so a breakout is a close above every prior-252-session
    level, exactly the codebase's new-high idiom (pro_dashboard `prior_max`).
  * 97 Club reference high is the WHOLE 52 weeks (`rolling(252).max()`); the
    10-session window is a SEPARATE recency test (the year's high was made in
    the last N sessions), not a shorter reference.
  * HVC relative volume = today's volume / PRIOR-50-session average
    (`volume.where(close.notna()).rolling(50).mean().shift(1)`) — the same
    `vavg50` definition pro_dashboard uses for its breakout-volume ratio.
  * CANSLIM requires the Minervini trend template AND RS >= 80 AND a 3-month
    momentum floor — never RS alone.

`eps_growth` (optional): {symbol_str: {"basis","latest_yoy_pct",...}} from
app.services.eps_growth_service. It is a point-in-time latest-quarter figure, so
the TML panel is only asserted on the newest row (never broadcast backwards).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from breadth.indicators.common import Panels
from breadth.indicators.participation import trend_template, rs_rating, _active

# column order mirrors the reference image (left -> right)
SCAN_ORDER = ["qullamaggie", "minervini", "sb_9m", "sb_4pct", "sb_weekly",
              "canslim", "tml", "club_97", "hvc"]


def _weinstein_stage(p: Panels, cfg: dict) -> pd.DataFrame:
    """Daily Weinstein stage as an int panel (1 basing / 2 advancing / 3 topping / 4 declining).

    Mirrors pro_dashboard.stage(): above MA150 & rising -> 2; above & not rising -> 3;
    below & falling -> 4; else 1. NaN MA150 (thin history) stays NaN."""
    c = cfg["indicators"]["participation"]
    w, sl = c["weinstein_ma"], c["weinstein_slope_lookback"]
    ma = p.level.rolling(w, min_periods=w).mean()
    rising, falling = ma > ma.shift(sl), ma < ma.shift(sl)
    above = p.level > ma
    stage = pd.DataFrame(np.nan, index=ma.index, columns=ma.columns)
    stage[above & rising] = 2
    stage[above & ~rising] = 3
    stage[~above & falling] = 4
    stage[~above & ~falling] = 1
    stage[ma.isna()] = np.nan
    return stage


def _rvol(p: Panels, window: int) -> pd.DataFrame:
    """Relative volume: today's volume / prior-`window`-session average (shifted off today)."""
    avg = p.volume.where(p.close.notna()).rolling(window, min_periods=20).mean().shift(1)
    return p.volume / avg.replace(0, np.nan)


def scan_masks(p: Panels, cfg: dict, eps_growth: dict | None = None) -> dict[str, pd.DataFrame]:
    """Return {scan_key: boolean day x symbol panel} for all nine Guru columns.

    `eps_growth` gates the TML panel; when None, TML is all-False (the panel still
    exists so the snapshot shape is stable)."""
    g = cfg["guru"]
    lvl, cl, vol, age = p.level, p.close, p.volume, p.age
    active = _active(p)
    traded = cl.notna()

    # --- shared primitives (imported, not re-derived) ---
    tt, tt_elig, _ = trend_template(p, cfg, active)
    rs = rs_rating(p, active)
    hi52 = lvl.rolling(252, min_periods=252).max()

    masks: dict[str, pd.DataFrame] = {}

    # 1. Qullamaggie breakout: close above the PRIOR 52-week high on volume expansion,
    #    still "fresh" (the cross happened within the last `fresh_days` sessions).
    q = g["qullamaggie"]
    prior_hi = lvl.rolling(q["lookback_days"], min_periods=q["lookback_days"]).max().shift(1)
    v50 = vol.where(traded).rolling(50, min_periods=20).mean().shift(1)
    bo_day = (cl > prior_hi) & traded & (vol >= q["vol_mult"] * v50)
    bo_day &= age >= (q["lookback_days"] + 1)
    masks["qullamaggie"] = bo_day.rolling(q["fresh_days"], min_periods=1).max().astype(bool)

    # 2. Minervini trend template (verbatim).
    masks["minervini"] = tt

    # 3. SB "9 Million Movers" -> relative-adapted: daily move >= 9% with volume up.
    s9 = g["sb_9m"]
    r = p.ret.round(8)
    up9 = (r >= s9["move_pct"]) & (age > 1) & traded
    if s9["require_volume_up"]:
        up9 &= vol > vol.shift(1)
    masks["sb_9m"] = up9

    # 4. SB 4% daily movers (the up_x membership, same rule as momentum).
    m = cfg["momentum"]
    up4 = (r >= m["move_threshold"]) & (age > 1) & traded
    if m["require_volume_up"]:
        up4 &= vol > vol.shift(1)
    masks["sb_4pct"] = up4

    # 5. SB 20% weekly movers: 5-session change >= 20%.
    w = g["sb_weekly"]
    wk_chg = lvl / lvl.shift(w["days"]) - 1
    masks["sb_weekly"] = (wk_chg >= w["move_pct"]) & (age > w["days"]) & traded

    # 6. CANSLIM (momentum flavour): trend template AND RS >= rs_min AND 3-month momentum.
    cs = g["canslim"]
    q_mom = lvl / lvl.shift(63) - 1
    masks["canslim"] = tt & (rs >= cs["rs_min"]) & (q_mom >= cs["q_mom_pct"]) & traded

    # 7. TML: trend template AND latest-quarter EPS YoY >= threshold (needs eps_growth).
    t = g["tml"]
    tml = pd.DataFrame(False, index=lvl.index, columns=lvl.columns)
    if eps_growth:
        ok = set()
        for sym, rec in eps_growth.items():
            if rec.get("latest_yoy_pct") is None:
                continue
            if t["require_quarterly"] and rec.get("basis") != "quarterly":
                continue
            if rec["latest_yoy_pct"] >= t["eps_growth_min"]:
                ok.add(str(sym))
        col_ok = pd.Series([str(c) in ok for c in lvl.columns], index=lvl.columns)
        last = lvl.index[-1]
        tml.loc[last] = (tt.loc[last] & col_ok) & traded.loc[last]
    masks["tml"] = tml

    # 8. 97 Club: within 3% of the 52-week high AND that high was made recently.
    c97 = g["club_97"]
    near_high = lvl >= (1 - c97["pct_off_high"]) * hi52
    recent_max = lvl.rolling(c97["recent_high_days"], min_periods=c97["recent_high_days"]).max()
    made_recently = (hi52 - recent_max).abs() < 1e-9          # year's high sits inside the last N days
    masks["club_97"] = near_high & made_recently & (age >= 253) & traded

    # 9. HVC: top-N relative volume today (rvol = vol / prior-50d avg).
    hv = g["hvc"]
    rv = _rvol(p, hv["vol_window"])
    rv_last = rv.where(traded)
    top = rv_last.rank(axis=1, ascending=False, method="first") <= hv["top_n"]
    masks["hvc"] = top.fillna(False).astype(bool)

    return masks


def compute_guru(p: Panels, cfg: dict, eps_growth: dict | None = None) -> dict:
    """Latest-day snapshot for the dashboard, shaped for breadth_data["guru"].

    {date, columns: {scan: [sym,...]}, rows: {sym: {chg_pct, rs, stage, count}},
     counts: {scan: n}} — columns ordered by occurrence desc then RS desc; a symbol
    with no RS/stage on the last day is still listed but its badge is omitted."""
    masks = scan_masks(p, cfg, eps_growth)
    idx = p.level.index[-1]
    rs_last = rs_rating(p, _active(p)).iloc[-1]
    stage_last = _weinstein_stage(p, cfg).iloc[-1]
    chg_last = (p.ret.iloc[-1] * 100).round(2)
    traded = p.close.iloc[-1].notna()

    columns: dict[str, list[int]] = {}
    count: dict[int, int] = {}
    for key in SCAN_ORDER:
        row = masks[key].iloc[-1] & traded
        syms = [int(s) for s in row.index[row.fillna(False).to_numpy(dtype=bool)]]
        columns[key] = syms
        for s in syms:
            count[s] = count.get(s, 0) + 1

    # order every column by occurrence (desc) then RS (desc)
    def rank(sym: int):
        return (-count.get(sym, 0), -(int(rs_last[sym]) if pd.notna(rs_last.get(sym)) else -1))
    for key in columns:
        columns[key].sort(key=rank)

    rows: dict[str, dict] = {}
    for sym in count:
        rs_v = rs_last.get(sym)
        st_v = stage_last.get(sym)
        rows[str(sym)] = {
            "chg_pct": None if pd.isna(chg_last.get(sym)) else float(chg_last[sym]),
            "rs": None if pd.isna(rs_v) else int(rs_v),
            "stage": None if pd.isna(st_v) else int(st_v),
            "count": count[sym],
        }
    return {
        "date": idx.strftime("%Y-%m-%d"),
        "columns": columns,
        "rows": rows,
        "counts": {k: len(v) for k, v in columns.items()},
    }
