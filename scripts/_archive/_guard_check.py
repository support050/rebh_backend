"""Read-only test of the new B1/B2 guards. Writes nothing to the DB."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import logging
logging.disable(logging.CRITICAL)  # keep the report readable

from datetime import date
from app.core.database import SessionLocal
import scripts.daily_market_update as dmu

out = []
db = SessionLocal()
try:
    # ── 1) _guard records an exception and lets the run continue ──
    dmu.reset_run_report(date(2026, 10, 5))
    reached_after = False
    with dmu._guard("test stage", critical=True, save_db=db):
        raise ValueError("boom")
    reached_after = True                      # must NOT have aborted the run
    rep = dmu.get_run_report()
    out.append(f"[1] run continued={reached_after} failures={len(rep['stage_failures'])} "
               f"critical_stages={rep['critical_stages']} ok={rep['ok']}")

    # ── 2) string failures (no exception object) ──
    dmu._record_stage_failure("test coverage", "0/272 rows written", critical=True)
    rep = dmu.get_run_report()
    out.append(f"[2] str failure stored={rep['stage_failures'][-1]['error']!r} "
               f"critical_count={len(rep['critical_stages'])}")

    # ── 3) _stamp_run_date must NOT erase earlier failures ──
    before = len(dmu.RUN_REPORT["stage_failures"])
    dmu._stamp_run_date(date(2026, 10, 6))
    after = len(dmu.RUN_REPORT["stage_failures"])
    out.append(f"[3] failures preserved across stamp: before={before} after={after} "
               f"date={dmu.RUN_REPORT['market_date']}")

    # ── 4) quality gate on synthetic rows (old 272-NULL incident shape) ──
    rows_bad = [{"symbol": f"S{i}", "open": 10, "high": 11, "low": 12, "close": 10.5,
                 "volume_traded": 1000, "market_cap": None} for i in range(50)]
    q = dmu.assess_price_quality(db, rows_bad, date(2026, 10, 5))
    out.append(f"[4] all-NULL market_cap -> anomaly={q['market_cap_anomaly']} "
               f"missing={q['market_cap_missing']}/{q['rows']} "
               f"rows_vs_prev={q['rows_vs_prev_day']} count_anomaly={q['row_count_anomaly']} "
               f"high<low={q['high_below_low']}")

    # ── 5) quality gate on healthy rows (no false alarm) ──
    rows_ok = [{"symbol": f"S{i}", "open": 10, "high": 11, "low": 9, "close": 10.5,
                "volume_traded": 1000, "market_cap": 5e8} for i in range(272)]
    q2 = dmu.assess_price_quality(db, rows_ok, date(2026, 10, 5))
    out.append(f"[5] healthy batch -> anomaly={q2['market_cap_anomaly']} "
               f"count_anomaly={q2['row_count_anomaly']} high<low={q2['high_below_low']} "
               f"prev_day_rows={q2['prev_day_rows']}")

    # ── 6) market_cap write guard renders COALESCE(NULLIF(...)) ──
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.models.price import Price
    import sqlalchemy as sa
    stmt = pg_insert(Price).values(symbol="1010", date=date(2026, 10, 5), close=10.0)
    set_ = {c: getattr(stmt.excluded, c) for c in dmu._PRICE_UPDATE_COLS if c != "market_cap"}
    set_["market_cap"] = stmt.excluded.market_cap
    stmt = stmt.on_conflict_do_update(index_elements=["symbol", "date"], set_=set_)
    guarded = pg_insert(Price).values(symbol="1010", date=date(2026, 10, 5), close=10.0)
    gset = {c: getattr(guarded.excluded, c) for c in dmu._PRICE_UPDATE_COLS if c != "market_cap"}
    gset["market_cap"] = sa.func.coalesce(sa.func.nullif(guarded.excluded.market_cap, 0), Price.market_cap)
    guarded = guarded.on_conflict_do_update(index_elements=["symbol", "date"], set_=gset)
    sql_plain = str(stmt.compile(dialect=sa.dialects.postgresql.dialect()))
    sql_guard = str(guarded.compile(dialect=sa.dialects.postgresql.dialect()))
    out.append(f"[6] plain SET has COALESCE={'coalesce' in sql_plain}")
    out.append(f"[6] guarded SET contains: "
               + [p for p in sql_guard.split("SET ")[1].split(", ") if "market_cap" in p][0])

    # ── 7) strict finalize flag is read from env with a safe default ──
    out.append(f"[7] PIPELINE_STRICT_FINALIZE={dmu.PIPELINE_STRICT_FINALIZE} "
               f"MARKET_CAP_GUARD_RATIO={dmu.MARKET_CAP_GUARD_RATIO} "
               f"SCRAPER_MIN_ROWS_RATIO={dmu.SCRAPER_MIN_ROWS_RATIO}")
finally:
    db.close()
    # اكتب اللي اتجمع حتى لو الاختبار نفسه وقِف في نصه
    text = "\n".join(out)
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_guard_out.txt"),
              "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)
