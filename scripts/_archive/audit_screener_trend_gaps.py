"""
Diagnose zero / flat periods on Minervini trend charts.

Zeros in the chart are usually NOT "missing API data" — they mean either:
  A) no rows in stock_indicators for that date, or
  B) no stocks passed the screener rules (or missing RS / indicator columns).

Usage:
  cd backend

  # Inspect a known gap (2005–2007)
  ..\\venv\\Scripts\\python.exe scripts\\audit_screener_trend_gaps.py --from 2005-09-01 --to 2008-03-01

  # Find long zero runs in stored chart data (all 4 metrics = 0)
  ..\\venv\\Scripts\\python.exe scripts\\audit_screener_trend_gaps.py --find-runs --min-days 30

  # Single date deep dive
  ..\\venv\\Scripts\\python.exe scripts\\audit_screener_trend_gaps.py --date 2007-06-22
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import and_, func

from app.core.database import SessionLocal
from app.models.screener_daily_trend import ScreenerDailyTrend
from app.models.stock_indicators import StockIndicator
from app.models.rs_daily import RSDaily
from app.services.screener_daily_trend_service import (
    _WIDE_FILTERS,
    _MONTH1_FILTERS,
    _MONTH4_FILTERS,
    compute_counts_for_date,
)


def _parse_date(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def _count(db, q) -> int:
    return int(q.scalar() or 0)


def diagnose_date(db, d: date) -> dict:
    """Funnel: how many stocks at each layer for one trading day."""
    base = db.query(func.count(StockIndicator.symbol)).filter(StockIndicator.date == d)

    stocks_total = _count(db, base)

    rs_rows = _count(
        db,
        db.query(func.count(RSDaily.symbol))
        .filter(RSDaily.date == d),
    )

    si_with_rs = _count(
        db,
        db.query(func.count(StockIndicator.symbol))
        .join(
            RSDaily,
            and_(
                RSDaily.symbol == StockIndicator.symbol,
                RSDaily.date == StockIndicator.date,
            ),
        )
        .filter(StockIndicator.date == d),
    )

    rs_gt_69 = _count(
        db,
        db.query(func.count(StockIndicator.symbol))
        .join(
            RSDaily,
            and_(
                RSDaily.symbol == StockIndicator.symbol,
                RSDaily.date == StockIndicator.date,
            ),
        )
        .filter(StockIndicator.date == d, RSDaily.rs_rating > 69),
    )

    wide = _count(db, base.filter(_WIDE_FILTERS))
    month1 = _count(
        db,
        db.query(func.count(StockIndicator.symbol))
        .join(
            RSDaily,
            and_(
                RSDaily.symbol == StockIndicator.symbol,
                RSDaily.date == StockIndicator.date,
            ),
        )
        .filter(StockIndicator.date == d, _MONTH1_FILTERS),
    )
    month4 = _count(
        db,
        db.query(func.count(StockIndicator.symbol))
        .join(
            RSDaily,
            and_(
                RSDaily.symbol == StockIndicator.symbol,
                RSDaily.date == StockIndicator.date,
            ),
        )
        .filter(StockIndicator.date == d, _MONTH4_FILTERS),
    )
    alrayan = _count(
        db,
        base.filter(StockIndicator.trend_signal == True),
    )

    null_5m = _count(
        db,
        base.filter(StockIndicator.sma_200_5m_ago.is_(None)),
    )
    null_pct_low = _count(
        db,
        base.filter(StockIndicator.percent_off_52w_low.is_(None)),
    )
    null_pct_high = _count(
        db,
        base.filter(StockIndicator.percent_off_52w_high.is_(None)),
    )

    stored = db.query(ScreenerDailyTrend).filter(ScreenerDailyTrend.date == d).first()
    recomputed = compute_counts_for_date(db, d)

    if stocks_total == 0:
        reason = "NO_STOCK_INDICATORS_ROWS"
    elif rs_rows == 0:
        reason = "NO_RS_DAILY_FOR_DATE"
    elif si_with_rs == 0:
        reason = "RS_SYMBOL_DATE_MISMATCH"
    elif null_5m > stocks_total * 0.5:
        reason = "MISSING_LONG_SMA_COLUMNS (e.g. sma_200_5m_ago)"
    elif null_pct_low > stocks_total * 0.5 or null_pct_high > stocks_total * 0.5:
        reason = "MISSING_52W_PERCENT_COLUMNS"
    elif wide == 0 and month1 == 0 and month4 == 0 and alrayan == 0:
        reason = "NO_STOCKS_PASS_ANY_SCREENER (rules too strict or bad market)"
    elif month1 == 0 and rs_gt_69 == 0:
        reason = "NO_RS_RATING_ABOVE_69"
    else:
        reason = "PARTIAL_ZEROS_OK"

    return {
        "date": str(d),
        "reason": reason,
        "stocks_total": stocks_total,
        "rs_daily_rows": rs_rows,
        "si_joined_rs": si_with_rs,
        "rs_rating_gt_69": rs_gt_69,
        "null_sma_200_5m_ago": null_5m,
        "null_pct_52w_low": null_pct_low,
        "null_pct_52w_high": null_pct_high,
        "computed_wide": wide,
        "computed_1m": month1,
        "computed_4m": month4,
        "computed_alrayan": alrayan,
        "stored": None
        if not stored
        else {
            "trend_1m": stored.trend_1m,
            "trend_4m": stored.trend_4m,
            "trend_5m_wide": stored.trend_5m_wide,
            "alrayan": stored.alrayan,
        },
        "recomputed": recomputed,
    }


def find_zero_runs(db, min_days: int) -> list[dict]:
    rows = (
        db.query(ScreenerDailyTrend)
        .order_by(ScreenerDailyTrend.date)
        .all()
    )
    runs: list[dict] = []
    start = None
    for r in rows:
        all_zero = (
            r.trend_1m == 0
            and r.trend_4m == 0
            and r.trend_5m_wide == 0
            and r.alrayan == 0
        )
        if all_zero:
            if start is None:
                start = r.date
            end = r.date
        else:
            if start is not None and (end - start).days + 1 >= min_days:
                runs.append({"from": str(start), "to": str(end), "days": (end - start).days + 1})
            start = None
    if start is not None and rows:
        end = rows[-1].date
        if (end - start).days + 1 >= min_days:
            runs.append({"from": str(start), "to": str(end), "days": (end - start).days + 1})
    return runs


def print_report(info: dict) -> None:
    print(f"\n=== {info['date']} | {info['reason']} ===")
    print(
        f"  stock_indicators: {info['stocks_total']} symbols | "
        f"rs_daily: {info['rs_daily_rows']} | joined: {info['si_joined_rs']} | RS>69: {info['rs_rating_gt_69']}"
    )
    print(
        f"  NULLs: sma_200_5m_ago={info['null_sma_200_5m_ago']} | "
        f"pct_52w_low={info['null_pct_52w_low']} | pct_52w_high={info['null_pct_52w_high']}"
    )
    print(
        f"  computed → 1M={info['computed_1m']} 4M={info['computed_4m']} "
        f"5MW={info['computed_wide']} ALR={info['computed_alrayan']}"
    )
    print(f"  stored table: {info['stored']}")
    print(f"  recomputed:   {info['recomputed']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Minervini trend zero gaps")
    parser.add_argument("--from", dest="date_from", help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="End date YYYY-MM-DD")
    parser.add_argument("--date", help="Single date to inspect")
    parser.add_argument("--find-runs", action="store_true", help="List long all-zero periods")
    parser.add_argument("--min-days", type=int, default=30, help="Min length for --find-runs")
    parser.add_argument("--sample", type=int, default=8, help="Dates to sample in range")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.find_runs:
            runs = find_zero_runs(db, args.min_days)
            print(f"All-zero runs (all 4 metrics = 0), min {args.min_days} days:\n")
            if not runs:
                print("  (none)")
            for run in runs:
                print(f"  {run['from']} → {run['to']}  ({run['days']} days)")
            if runs:
                first = runs[0]
                mid = _parse_date(first["from"]) + timedelta(days=first["days"] // 2)
                print("\n--- Sample diagnosis from first run (start, mid, end) ---")
                d_start = _parse_date(first["from"])
                d_end = _parse_date(first["to"])
                for sample_d in [d_start, mid, d_end]:
                    print_report(diagnose_date(db, sample_d))
            return

        if args.date:
            print_report(diagnose_date(db, _parse_date(args.date)))
            return

        if not args.date_from or not args.date_to:
            parser.error("Use --from/--to, --date, or --find-runs")

        d0 = _parse_date(args.date_from)
        d1 = _parse_date(args.date_to)
        dates = (
            db.query(StockIndicator.date)
            .filter(StockIndicator.date >= d0, StockIndicator.date <= d1)
            .distinct()
            .order_by(StockIndicator.date)
            .all()
        )
        uniq = [r[0] for r in dates]
        if not uniq:
            print(f"No stock_indicators dates between {d0} and {d1}")
            return

        step = max(1, len(uniq) // max(1, args.sample))
        picked = uniq[::step][: args.sample]
        print(f"Range {d0} → {d1}: {len(uniq)} trading days in stock_indicators")
        print(f"Sampling {len(picked)} dates:\n")
        for d in picked:
            print_report(diagnose_date(db, d))

        print("\n--- Stored chart zeros in range (from screener_daily_trend_counts) ---")
        stored = (
            db.query(ScreenerDailyTrend)
            .filter(ScreenerDailyTrend.date >= d0, ScreenerDailyTrend.date <= d1)
            .order_by(ScreenerDailyTrend.date)
            .all()
        )
        zero_days = sum(
            1
            for r in stored
            if r.trend_1m == 0 and r.trend_4m == 0 and r.trend_5m_wide == 0 and r.alrayan == 0
        )
        print(f"  days in table: {len(stored)} | all-four-zero: {zero_days}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
