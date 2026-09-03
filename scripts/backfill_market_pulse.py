"""
backfill_market_pulse.py
========================
Reads ALL rows from historical_reports (oldest → newest),
computes Market Pulse signals for each day, and inserts into market_pulse.

Run from backend/:
    ..\venv\Scripts\python.exe scripts\backfill_market_pulse.py
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decimal import Decimal
from app.core.database import SessionLocal
from app.models.market_reports import HistoricalReport
from sqlalchemy import func
from datetime import date as dt_date
from app.models.market_pulse import MarketPulse
from app.services.market_pulse_calc import (
    OHLCVInput, HistoryRow, compute_signals, build_record, get_calc_settings,
)


def parse_num(val: str | None) -> float | None:
    """Parse string like '11,031.32' → float."""
    if not val:
        return None
    try:
        return float(val.replace(",", ""))
    except (ValueError, TypeError):
        return None


def parse_int(val: str | None) -> int | None:
    if not val:
        return None
    try:
        return int(val.replace(",", "").split(".")[0])
    except (ValueError, TypeError):
        return None


def main():
    db = SessionLocal()
    try:
        # Check if market_pulse already has data
        existing = db.query(MarketPulse.id).count()
        if existing > 0:
            print(f"⚠️  market_pulse already has {existing} rows. Skipping existing dates.")

        # Get the latest date currently in market_pulse
        latest_date_result = db.query(func.max(MarketPulse.date)).scalar()
        
        if latest_date_result:
            print(f"⚠️  market_pulse has data up to {latest_date_result}. Fetching newer reports only.")
            reports = (
                db.query(HistoricalReport)
                .filter(HistoricalReport.report_date > latest_date_result)
                .order_by(HistoricalReport.report_date.asc())
                .all()
            )
        else:
            print("⚠️  market_pulse is empty. Starting from 2010-01-01.")
            START_DATE = dt_date(2010, 1, 1)
            reports = (
                db.query(HistoricalReport)
                .filter(HistoricalReport.report_date >= START_DATE)
                .order_by(HistoricalReport.report_date.asc())
                .all()
            )

        print(f"📊 Found {len(reports)} new historical reports to process")
        
        existing_dates = set() # No need to load all dates in memory anymore since we filter in SQL

        calc_settings = get_calc_settings(db)
        print(f"⚙️ Loaded TASI settings: {calc_settings}")

        inserted = 0
        skipped = 0

        for i, report in enumerate(reports):
            if report.report_date in existing_dates:
                skipped += 1
                continue

            o = parse_num(report.open_price)
            h = parse_num(report.high_price)
            lo = parse_num(report.low_price)
            c = parse_num(report.close_price)
            vol = parse_num(report.volume_traded)

            # Skip rows with missing OHLCV
            if None in (o, h, lo, c, vol):
                print(f"  ⏭️  {report.report_date} — missing OHLCV, skipping")
                skipped += 1
                continue

            today_in = OHLCVInput(
                date=report.report_date,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume_traded=vol,
                value_traded=parse_num(report.value_traded),
                no_of_trades=parse_int(report.no_of_trades),
            )

            # Get history from already-inserted market_pulse rows (newest first)
            raw_history = (
                db.query(MarketPulse)
                .filter(MarketPulse.date < report.report_date)
                .order_by(MarketPulse.date.desc())
                .limit(200)
                .all()
            )

            history = [
                HistoryRow(
                    close=float(r.close),
                    volume_traded=float(r.volume_traded),
                    high=float(r.high),
                    low=float(r.low),
                    ema_21=float(r.ema_21) if r.ema_21 is not None else None,
                    atr=float(r.atr) if r.atr is not None else None,
                    rd_count=r.rd_count,
                    ftd=r.ftd,
                    dd_sd=r.dd_sd,
                    current_outlook=r.current_outlook,
                    change_pct=float(r.change_pct) if r.change_pct is not None else None,
                )
                for r in raw_history
            ]

            signals = compute_signals(today_in, history, settings=calc_settings)
            record = MarketPulse(**build_record(today_in, signals))
            db.add(record)

            inserted += 1

            # Commit in batches
            if inserted % 100 == 0:
                db.commit()
                print(f"  ✅ {inserted} inserted ({report.report_date})")

        db.commit()
        print(f"\n🎉 Done! Inserted: {inserted}, Skipped: {skipped}")
        try:
            total_count = db.query(func.count(MarketPulse.id)).scalar()
            print(f"📈 Total market_pulse rows: {total_count}")
        except Exception:
            pass

    except Exception as e:
        db.rollback()
        print(f"❌ Error: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
