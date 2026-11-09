"""
Pre-computed Minervini / Alrayan daily counts — how production dashboards avoid heavy queries.

- Daily job: upsert ONE row per market date (~4 fast COUNT queries).
- Backfill script: bulk load history once into `screener_daily_trend_counts`.
- API: SELECT ordered rows (milliseconds), optional Redis cache on top.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models.screener_daily_trend import ScreenerDailyTrend
from app.models.stock_indicators import StockIndicator
from app.models.rs_daily import RSDaily

logger = logging.getLogger(__name__)

_WIDE_FILTERS = and_(
    StockIndicator.sma_50 > StockIndicator.sma_200,
    StockIndicator.sma_200 > StockIndicator.sma_200_5m_ago,
    StockIndicator.price_vs_sma_50_percent > 0.0,
    StockIndicator.price_vs_sma_150_percent > 0.0,
    StockIndicator.price_vs_sma_200_percent > 0.0,
    StockIndicator.sma_30w.isnot(None),
    StockIndicator.close > StockIndicator.sma_30w,
    StockIndicator.sma_40w.isnot(None),
    StockIndicator.close > StockIndicator.sma_40w,
)

_MONTH1_FILTERS = and_(
    RSDaily.rs_rating > 69,
    StockIndicator.sma_50 > StockIndicator.sma_150,
    StockIndicator.sma_50 > StockIndicator.sma_200,
    StockIndicator.sma_150 > StockIndicator.sma_200,
    StockIndicator.sma_200 > StockIndicator.sma_200_1m_ago,
    StockIndicator.percent_off_52w_low > 30.0,
    StockIndicator.percent_off_52w_high > -25.0,
    StockIndicator.price_vs_sma_50_percent > 0.0,
    StockIndicator.price_vs_sma_150_percent > 0.0,
    StockIndicator.price_vs_sma_200_percent > 0.0,
    StockIndicator.sma_30w.isnot(None),
    StockIndicator.close > StockIndicator.sma_30w,
    StockIndicator.sma_40w.isnot(None),
    StockIndicator.close > StockIndicator.sma_40w,
)

_ALHUSSAIN_FILTERS = and_(
    StockIndicator.sma_50 > StockIndicator.sma_150,
    StockIndicator.sma_50 > StockIndicator.sma_200,
    StockIndicator.sma_150 > StockIndicator.sma_200,
    StockIndicator.price_vs_sma_50_percent >= 0.0,
    StockIndicator.average_volume_50 >= 100_000,
)

_MONTH4_FILTERS = and_(
    RSDaily.rs_rating > 69,
    StockIndicator.sma_50 > StockIndicator.sma_150,
    StockIndicator.sma_50 > StockIndicator.sma_200,
    StockIndicator.sma_150 > StockIndicator.sma_200,
    StockIndicator.sma_200 > StockIndicator.sma_200_1m_ago,
    StockIndicator.sma_200 > StockIndicator.sma_200_2m_ago,
    StockIndicator.sma_200 > StockIndicator.sma_200_3m_ago,
    StockIndicator.sma_200 > StockIndicator.sma_200_4m_ago,
    StockIndicator.sma_200_1m_ago > StockIndicator.sma_200_2m_ago,
    StockIndicator.sma_200_2m_ago > StockIndicator.sma_200_3m_ago,
    StockIndicator.sma_200_3m_ago > StockIndicator.sma_200_4m_ago,
    StockIndicator.percent_off_52w_high > -25.0,
    StockIndicator.percent_off_52w_low > 30.0,
    StockIndicator.price_vs_sma_50_percent > 0.0,
    StockIndicator.price_vs_sma_150_percent > 0.0,
    StockIndicator.price_vs_sma_200_percent > 0.0,
    StockIndicator.sma_30w.isnot(None),
    StockIndicator.close > StockIndicator.sma_30w,
    StockIndicator.sma_40w.isnot(None),
    StockIndicator.close > StockIndicator.sma_40w,
)


def compute_counts_for_date(db: Session, target: date) -> dict[str, int]:
    """Four indexed COUNTs for a single trading day — used by the daily pipeline."""
    wide = (
        db.query(func.count(StockIndicator.symbol))
        .filter(StockIndicator.date == target, _WIDE_FILTERS)
        .scalar()
        or 0
    )

    month1 = (
        db.query(func.count(StockIndicator.symbol))
        .join(
            RSDaily,
            and_(
                RSDaily.symbol == StockIndicator.symbol,
                RSDaily.date == StockIndicator.date,
            ),
        )
        .filter(StockIndicator.date == target, _MONTH1_FILTERS)
        .scalar()
        or 0
    )

    month4 = (
        db.query(func.count(StockIndicator.symbol))
        .join(
            RSDaily,
            and_(
                RSDaily.symbol == StockIndicator.symbol,
                RSDaily.date == StockIndicator.date,
            ),
        )
        .filter(StockIndicator.date == target, _MONTH4_FILTERS)
        .scalar()
        or 0
    )

    alrayan = (
        db.query(func.count(StockIndicator.symbol))
        .filter(StockIndicator.date == target, StockIndicator.trend_signal == True)
        .scalar()
        or 0
    )

    alhussain = (
        db.query(func.count(StockIndicator.symbol))
        .filter(StockIndicator.date == target, _ALHUSSAIN_FILTERS)
        .scalar()
        or 0
    )

    return {
        "trend_1m": int(month1),
        "trend_4m": int(month4),
        "trend_5m_wide": int(wide),
        "alrayan": int(alrayan),
        "alhussain": int(alhussain),
    }


def upsert_daily_row(db: Session, target: date, counts: dict[str, int]) -> None:
    row = db.query(ScreenerDailyTrend).filter(ScreenerDailyTrend.date == target).first()
    if row is None:
        row = ScreenerDailyTrend(date=target)
        db.add(row)
    row.trend_1m = counts["trend_1m"]
    row.trend_4m = counts["trend_4m"]
    row.trend_5m_wide = counts["trend_5m_wide"]
    row.alrayan = counts["alrayan"]
    if "alhussain" in counts:
        row.alhussain = counts["alhussain"]


def update_market_date(db: Session, market_date: date) -> None:
    counts = compute_counts_for_date(db, market_date)
    upsert_daily_row(db, market_date, counts)
    db.commit()
    logger.info(
        "Screener daily trend updated for %s (1m=%s, 4m=%s, 5mw=%s, alr=%s, alh=%s)",
        market_date,
        counts["trend_1m"],
        counts["trend_4m"],
        counts["trend_5m_wide"],
        counts["alrayan"],
        counts["alhussain"],
    )


def row_count(db: Session) -> int:
    return db.query(func.count(ScreenerDailyTrend.date)).scalar() or 0


def load_series(db: Session, limit: int = 6000) -> list[dict[str, Any]]:
    rows = (
        db.query(ScreenerDailyTrend)
        .order_by(ScreenerDailyTrend.date.desc())
        .limit(limit)
        .all()
    )
    rows = list(reversed(rows))
    return [
        {
            "date": str(r.date),
            "trend_1m": r.trend_1m,
            "trend_4m": r.trend_4m,
            "trend_5m_wide": r.trend_5m_wide,
            "alrayan": r.alrayan,
            "alhussain": r.alhussain,
        }
        for r in rows
    ]


def load_alhussain_series(db: Session, limit: int = 6000) -> list[dict[str, Any]]:
    rows = (
        db.query(ScreenerDailyTrend)
        .order_by(ScreenerDailyTrend.date.desc())
        .limit(limit)
        .all()
    )
    rows = list(reversed(rows))
    return [
        {
            "time": str(r.date),
            "count": r.alhussain,
        }
        for r in rows
    ]


def build_payload(db: Session, limit: int = 6000) -> dict[str, Any]:
    series = load_series(db, limit)
    return {
        "title": "Minervini Trend",
        "series": series,
        "total_dates": len(series),
    }


def _counts_for_date_chunk(db: Session, chunk_dates: list[date]) -> dict[str, dict[str, int]]:
    """Aggregate four screeners for a small date window (avoids 20+ min mega-queries)."""
    if not chunk_dates:
        return {}

    date_filter = StockIndicator.date.in_(chunk_dates)

    wide_rows = (
        db.query(StockIndicator.date, func.count(StockIndicator.symbol).label("count"))
        .filter(date_filter, _WIDE_FILTERS)
        .group_by(StockIndicator.date)
        .all()
    )
    wide_map = {str(r.date): int(r.count) for r in wide_rows}

    month1_rows = (
        db.query(StockIndicator.date, func.count(StockIndicator.symbol).label("count"))
        .join(
            RSDaily,
            and_(
                RSDaily.symbol == StockIndicator.symbol,
                RSDaily.date == StockIndicator.date,
            ),
        )
        .filter(date_filter, _MONTH1_FILTERS)
        .group_by(StockIndicator.date)
        .all()
    )
    month1_map = {str(r.date): int(r.count) for r in month1_rows}

    month4_rows = (
        db.query(StockIndicator.date, func.count(StockIndicator.symbol).label("count"))
        .join(
            RSDaily,
            and_(
                RSDaily.symbol == StockIndicator.symbol,
                RSDaily.date == StockIndicator.date,
            ),
        )
        .filter(date_filter, _MONTH4_FILTERS)
        .group_by(StockIndicator.date)
        .all()
    )
    month4_map = {str(r.date): int(r.count) for r in month4_rows}

    alrayan_rows = (
        db.query(StockIndicator.date, func.count(StockIndicator.symbol).label("count"))
        .filter(date_filter, StockIndicator.trend_signal == True)
        .group_by(StockIndicator.date)
        .all()
    )
    alrayan_map = {str(r.date): int(r.count) for r in alrayan_rows}

    alhussain_rows = (
        db.query(StockIndicator.date, func.count(StockIndicator.symbol).label("count"))
        .filter(date_filter, _ALHUSSAIN_FILTERS)
        .group_by(StockIndicator.date)
        .all()
    )
    alhussain_map = {str(r.date): int(r.count) for r in alhussain_rows}

    out: dict[str, dict[str, int]] = {}
    for d in chunk_dates:
        key = str(d)
        out[key] = {
            "trend_1m": month1_map.get(key, 0),
            "trend_4m": month4_map.get(key, 0),
            "trend_5m_wide": wide_map.get(key, 0),
            "alrayan": alrayan_map.get(key, 0),
            "alhussain": alhussain_map.get(key, 0),
        }
    return out


def backfill_history(
    limit: int = 6000,
    *,
    chunk_size: int = 500,
    verbose: bool = True,
    force: bool = False,
) -> int:
    """
    Ultra-fast vectorized SQL backfill using single-pass conditional aggregation + COPY protocol.
    Calculates all screeners (1M, 4M, Wide, Alrayan, Alhussain) across history in seconds.
    """
    import time
    from io import StringIO
    import psycopg2
    from app.core.config import settings
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        date_rows = (
            db.query(StockIndicator.date)
            .distinct()
            .order_by(StockIndicator.date.desc())
            .limit(limit)
            .all()
        )
        all_dates = sorted(r[0] for r in date_rows)
        existing = {r[0] for r in db.query(ScreenerDailyTrend.date).all()}
    finally:
        db.close()

    pending = all_dates if force else [d for d in all_dates if d not in existing]
    if verbose:
        print(
            f"Backfill: {len(pending)} days to process "
            f"({len(existing)} already in table, fast SQL mode, force={force})",
            flush=True,
        )
    if not pending:
        return 0

    started = time.time()
    min_date = str(pending[0])
    max_date = str(pending[-1])

    # Direct connection for fast query and COPY
    db_url = str(settings.DATABASE_URL).replace("postgresql://", "").replace("postgresql+psycopg2://", "")
    user_pass, host_db = db_url.split("@")
    user, password = user_pass.split(":", 1)
    host_port, dbname = host_db.split("/", 1)
    if "?" in dbname:
        dbname = dbname.split("?")[0]
    host, port = (host_port.split(":") + ["5432"])[:2]

    pg_conn = psycopg2.connect(
        dbname=dbname, user=user, password=password,
        host=host, port=port, sslmode="require"
    )

    try:
        cur = pg_conn.cursor()
        print("⚡ Computing all 5 screeners in parallel directly in PostgreSQL...", flush=True)

        agg_query = f"""
            SELECT 
                si.date,
                COUNT(*) FILTER (WHERE 
                    r.rs_rating > 69 
                    AND si.sma_50 > si.sma_150 
                    AND si.sma_50 > si.sma_200 
                    AND si.sma_150 > si.sma_200 
                    AND si.sma_200 > si.sma_200_1m_ago 
                    AND si.percent_off_52w_low > 30 
                    AND si.percent_off_52w_high > -25 
                    AND si.price_vs_sma_50_percent > 0 
                    AND si.price_vs_sma_150_percent > 0 
                    AND si.price_vs_sma_200_percent > 0 
                    AND si.sma_30w IS NOT NULL AND si.close > si.sma_30w 
                    AND si.sma_40w IS NOT NULL AND si.close > si.sma_40w
                ) AS trend_1m,
                COUNT(*) FILTER (WHERE 
                    r.rs_rating > 69 
                    AND si.sma_50 > si.sma_150 
                    AND si.sma_50 > si.sma_200 
                    AND si.sma_150 > si.sma_200 
                    AND si.sma_200 > si.sma_200_1m_ago 
                    AND si.sma_200 > si.sma_200_2m_ago 
                    AND si.sma_200 > si.sma_200_3m_ago 
                    AND si.sma_200 > si.sma_200_4m_ago 
                    AND si.sma_200_1m_ago > si.sma_200_2m_ago 
                    AND si.sma_200_2m_ago > si.sma_200_3m_ago 
                    AND si.sma_200_3m_ago > si.sma_200_4m_ago 
                    AND si.percent_off_52w_high > -25 
                    AND si.percent_off_52w_low > 30 
                    AND si.price_vs_sma_50_percent > 0 
                    AND si.price_vs_sma_150_percent > 0 
                    AND si.price_vs_sma_200_percent > 0 
                    AND si.sma_30w IS NOT NULL AND si.close > si.sma_30w 
                    AND si.sma_40w IS NOT NULL AND si.close > si.sma_40w
                ) AS trend_4m,
                COUNT(*) FILTER (WHERE 
                    si.sma_50 > si.sma_200 
                    AND si.sma_200 > si.sma_200_5m_ago 
                    AND si.price_vs_sma_50_percent > 0 
                    AND si.price_vs_sma_150_percent > 0 
                    AND si.price_vs_sma_200_percent > 0 
                    AND si.sma_30w IS NOT NULL AND si.close > si.sma_30w 
                    AND si.sma_40w IS NOT NULL AND si.close > si.sma_40w
                ) AS trend_5m_wide,
                COUNT(*) FILTER (WHERE 
                    si.trend_signal IS TRUE
                ) AS alrayan,
                COUNT(*) FILTER (WHERE 
                    si.sma_50 > si.sma_150 
                    AND si.sma_50 > si.sma_200 
                    AND si.sma_150 > si.sma_200 
                    AND si.price_vs_sma_50_percent >= 0 
                    AND si.average_volume_50 >= 100000
                ) AS alhussain
            FROM stock_indicators si
            LEFT JOIN rs_daily_v2 r ON r.symbol = si.symbol AND r.date = si.date
            WHERE si.date >= '{min_date}' AND si.date <= '{max_date}'
            GROUP BY si.date
            ORDER BY si.date ASC;
        """

        cur.execute(agg_query)
        rows = cur.fetchall()
        print(f"📊 Calculated {len(rows)} dates in {time.time() - started:.1f}s. Uploading via COPY...", flush=True)

        buf = StringIO()
        for r in rows:
            # date, 1m, 4m, 5m_wide, alrayan, alhussain
            buf.write(f"{r[0]}\t{r[1]}\t{r[2]}\t{r[3]}\t{r[4]}\t{r[5]}\n")
        buf.seek(0)

        cur.execute("""
            CREATE TEMP TABLE tmp_trend_backfill (
                date DATE PRIMARY KEY,
                trend_1m INTEGER,
                trend_4m INTEGER,
                trend_5m_wide INTEGER,
                alrayan INTEGER,
                alhussain INTEGER
            );
        """)
        cur.copy_from(
            buf, 'tmp_trend_backfill', sep='\t',
            columns=['date', 'trend_1m', 'trend_4m', 'trend_5m_wide', 'alrayan', 'alhussain']
        )

        cur.execute("""
            INSERT INTO screener_daily_trend_counts (date, trend_1m, trend_4m, trend_5m_wide, alrayan, alhussain)
            SELECT date, trend_1m, trend_4m, trend_5m_wide, alrayan, alhussain FROM tmp_trend_backfill
            ON CONFLICT (date) DO UPDATE SET
                trend_1m = EXCLUDED.trend_1m,
                trend_4m = EXCLUDED.trend_4m,
                trend_5m_wide = EXCLUDED.trend_5m_wide,
                alrayan = EXCLUDED.alrayan,
                alhussain = EXCLUDED.alhussain;
        """)
        written = cur.rowcount
        pg_conn.commit()
        cur.close()
        print(f"✅ Fast Backfill complete: updated {written} rows in {time.time() - started:.1f}s!", flush=True)
        return written
    finally:
        pg_conn.close()


def bulk_upsert_series(db: Session, series: list[dict[str, Any]], *, batch: int = 200) -> int:
    """Insert/update many days (from one-off backfill compute)."""
    written = 0
    for i, point in enumerate(series, start=1):
        d = point["date"]
        if isinstance(d, str):
            from datetime import datetime

            d = datetime.strptime(d[:10], "%Y-%m-%d").date()
        upsert_daily_row(
            db,
            d,
            {
                "trend_1m": int(point.get("trend_1m", 0)),
                "trend_4m": int(point.get("trend_4m", 0)),
                "trend_5m_wide": int(point.get("trend_5m_wide", 0)),
                "alrayan": int(point.get("alrayan", 0)),
                "alhussain": int(point.get("alhussain", 0)),
            },
        )
        written += 1
        if i % batch == 0:
            db.commit()
            logger.info("Backfill progress: %s / %s days", i, len(series))
    db.commit()
    return written


def backfill_alhussain_only(
    limit: int = 6000,
    *,
    chunk_size: int = 60,
    verbose: bool = True,
) -> int:
    """Backfill alhussain counts for rows already in the table (or insert missing dates)."""
    import time
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        date_rows = (
            db.query(StockIndicator.date)
            .distinct()
            .order_by(StockIndicator.date.desc())
            .limit(limit)
            .all()
        )
        all_dates = sorted(r[0] for r in date_rows)
    finally:
        db.close()

    if not all_dates:
        return 0

    written = 0
    started = time.time()
    for start in range(0, len(all_dates), chunk_size):
        chunk = all_dates[start : start + chunk_size]
        db = SessionLocal()
        try:
            alhussain_rows = (
                db.query(StockIndicator.date, func.count(StockIndicator.symbol).label("count"))
                .filter(
                    StockIndicator.date.in_(chunk),
                    _ALHUSSAIN_FILTERS,
                )
                .group_by(StockIndicator.date)
                .all()
            )
            alhussain_map = {str(r.date): int(r.count) for r in alhussain_rows}

            for d in chunk:
                count = alhussain_map.get(str(d), 0)
                row = db.query(ScreenerDailyTrend).filter(ScreenerDailyTrend.date == d).first()
                if row is None:
                    row = ScreenerDailyTrend(date=d)
                    db.add(row)
                row.alhussain = count

            db.commit()
            written += len(chunk)
            if verbose:
                pct = min(100, int(100 * (start + len(chunk)) / len(all_dates)))
                print(
                    f"  alhussain chunk {start // chunk_size + 1}: "
                    f"{start + len(chunk)}/{len(all_dates)} days ({pct}%) "
                    f"— {time.time() - started:.0f}s elapsed",
                    flush=True,
                )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return written
