"""Point & Figure Bullish Percent Index (Chartcraft / Tom Dorsey).

For every stock a P&F chart is kept with percentage boxes (`box_pct`, default 2%) and a 3-box
reversal, on the adjusted level (last valid close). A stock is "on a P&F buy signal" from the day
an X column rises above the top of the previous X column (double-top breakout) until an O column
falls below the bottom of the previous O column (double-bottom breakdown).
  BPI_T = 100 * #(stocks on buy signal) / #(eligible stocks)        eligible = age >= 60 and active
Classic reading (Dorsey): BPI > 70 overbought / < 30 oversold; the BPI's own column status
(Bull Confirmed / Bull Correction / Bear Alert / Bear Confirmed / Bull Alert) is derived from a
P&F chart of the BPI itself with 2-point boxes and 3-box reversal.
Pure-python loop per stock (state machine); no look-ahead: signal on T uses closes <= T.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from breadth.indicators.common import Panels, eligible


def pnf_buy_state(prices: np.ndarray, box_pct: float = 0.02, reversal: int = 3) -> np.ndarray:
    """Return an int8 array: 1 = on buy signal, -1 = on sell signal, 0 = no signal yet. NaN prices carry the state."""
    n = len(prices)
    out = np.zeros(n, dtype=np.int8)
    col = 0            # +1 X column, -1 O column, 0 none
    top = bot = np.nan # current column top / bottom (box levels, in log space)
    prev_x_top = np.nan; prev_o_bot = np.nan
    state = 0
    lb = np.log1p(box_pct)
    for i in range(n):
        p = prices[i]
        if np.isnan(p) or p <= 0:
            out[i] = state; continue
        b = np.floor(np.log(p) / lb)          # box index
        if col == 0:
            col = 1; top = bot = b
        elif col == 1:
            if b > top:
                top = b
                if not np.isnan(prev_x_top) and top > prev_x_top and state != 1:
                    state = 1
            elif b <= top - reversal:
                prev_x_top = top; col = -1; bot = b; top = top - 1
                if not np.isnan(prev_o_bot) and bot < prev_o_bot and state != -1:
                    state = -1
        else:
            if b < bot:
                bot = b
                if not np.isnan(prev_o_bot) and bot < prev_o_bot and state != -1:
                    state = -1
            elif b >= bot + reversal:
                prev_o_bot = bot; col = 1; top = b; bot = bot + 1
                if not np.isnan(prev_x_top) and top > prev_x_top and state != 1:
                    state = 1
        out[i] = state
    return out


def compute_bpi(p: Panels, cfg: dict) -> pd.DataFrame:
    c = cfg["indicators"].get("pnf", {"box_pct": 0.02, "reversal": 3, "min_age": 60})
    lvl = p.level
    traded = p.close.notna()
    active = traded.rolling(10, min_periods=1).max().astype(bool) & p.listed
    elig = eligible(p.age, c["min_age"]) & active
    states = {}
    for sym in lvl.columns:
        states[sym] = pnf_buy_state(lvl[sym].to_numpy(dtype=float), c["box_pct"], c["reversal"])
    st = pd.DataFrame(states, index=lvl.index)
    buy = (st == 1) & elig
    sell = (st == -1) & elig
    out = pd.DataFrame(index=lvl.index)
    out["n_pnf"] = elig.sum(axis=1)
    out["pnf_buy"] = buy.sum(axis=1)
    out["pnf_sell"] = sell.sum(axis=1)
    out["bpi"] = 100 * out["pnf_buy"] / out["n_pnf"].replace(0, np.nan)
    # BPI status from a P&F chart of the BPI itself (2-point boxes, 3-box reversal)
    out["bpi_status"] = bpi_status(out["bpi"].to_numpy(dtype=float))
    out.index.name = "date"
    return out, st


def bpi_status(bpi: np.ndarray, box: float = 2.0, reversal: int = 3) -> np.ndarray:
    """Dorsey's six BPI statuses. Bull Confirmed: X column that broke above the previous X top;
    Bear Confirmed: O column below the previous O bottom; Bull Correction: O column while last signal was buy;
    Bear Alert: O column reversing from above 70 while last signal was buy... (simplified, documented):
      X column + last signal buy  -> 'Bull Confirmed'      X column + last signal sell -> 'Bear Correction' (Bull Alert if from <30)
      O column + last signal sell -> 'Bear Confirmed'      O column + last signal buy  -> 'Bull Correction' (Bear Alert if from >70)
    """
    n = len(bpi); out = np.array([""] * n, dtype=object)
    col = 0; top = bot = np.nan; px = po = np.nan; state = 0; from_hi = from_lo = False
    for i in range(n):
        v = bpi[i]
        if np.isnan(v):
            out[i] = ""; continue
        b = np.floor(v / box)
        if col == 0:
            col = 1; top = bot = b
        elif col == 1:
            if b > top:
                top = b
                if not np.isnan(px) and top > px: state = 1
            elif b <= top - reversal:
                px = top; col = -1; bot = b; from_hi = (top * box) >= 70
                if not np.isnan(po) and bot < po: state = -1
        else:
            if b < bot:
                bot = b
                if not np.isnan(po) and bot < po: state = -1
            elif b >= bot + reversal:
                po = bot; col = 1; top = b; from_lo = (bot * box) <= 30
                if not np.isnan(px) and top > px: state = 1
        if col == 1:
            out[i] = "Bull Confirmed" if state == 1 else ("Bull Alert" if from_lo else "Bear Correction")
        else:
            out[i] = "Bear Confirmed" if state == -1 else ("Bear Alert" if from_hi else "Bull Correction")
    return out
