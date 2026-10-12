"""
READ-ONLY parity check: old path vs batched path.
Verifies the N+1 refactors produce byte-identical results. Writes nothing.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import date
from sqlalchemy import text
from app.core.database import SessionLocal

OUT = []
def p(x): OUT.append(x); print(x)

db = SessionLocal()
try:
    target_date = db.execute(text("SELECT MAX(date) FROM prices")).scalar()
    p(f"target_date={target_date}")

    symbols = [r[0] for r in db.execute(text(
        "SELECT DISTINCT symbol FROM prices WHERE date = :d ORDER BY symbol LIMIT 8"
    ), {"d": target_date}).fetchall()]
    p(f"symbols={symbols}")

    # ═══ 1. indicators: batched rows == per-symbol LIMIT 600 rows ═══
    from scripts.calculate_stock_indicators import (
        _preload_price_rows, calculate_all_indicators_for_stock,
    )
    pre = _preload_price_rows(db, symbols, target_date)
    p("\n[A] price-row parity (batched window query vs per-symbol LIMIT 600)")
    for sym in symbols:
        old = db.execute(text("""
            SELECT date, open, high, low, close FROM prices
            WHERE symbol = :s AND date <= :d ORDER BY date DESC LIMIT 600
        """), {"s": sym, "d": target_date}).fetchall()
        old_dicts = list(reversed([dict(r._mapping) for r in old]))
        new = pre.get(sym, [])
        same = (len(old_dicts) == len(new)
               and all((o["date"] == n["date"] and str(o["close"]) == str(n["close"])
                        and str(o["open"]) == str(n["open"]) and str(o["high"]) == str(n["high"])
                        and str(o["low"]) == str(n["low"]) for o, n in zip(old_dicts, new))))
        p(f"   {sym}: old={len(old_dicts)} new={len(new)} identical={same}")
        assert same, f"ROW MISMATCH {sym}"

    # ═══ 2. indicators: computed dict parity ═══
    p("\n[B] calculate_all_indicators_for_stock parity (fetched vs preloaded)")
    for sym in symbols[:5]:
        a = calculate_all_indicators_for_stock(db, sym, target_date)
        b = calculate_all_indicators_for_stock(db, sym, target_date, rows_dicts=pre.get(sym))
        ka, kb = set(a), set(b)
        diffs = [k for k in (ka & kb) if str(a[k]) != str(b[k])]
        p(f"   {sym}: keys_old={len(ka)} keys_new={len(kb)} value_diffs={len(diffs)} "
          f"{('-> ' + str(diffs[:5])) if diffs else ''}")
        assert ka == kb and not diffs, f"INDICATOR MISMATCH {sym}"

    # ═══ 3. RS-line: preloaded benchmark/stock vs per-symbol fetch ═══
    from app.services.rs_line import (
        calculate_rs_line, df_to_response, _fetch_stock_close, _fetch_tasi_close,
    )
    start_str, end_str = "2020-01-01", target_date.strftime("%Y-%m-%d")
    bench = _fetch_tasi_close(db, start_str, end_str)
    p(f"\n[C] RS-line summary parity (benchmark loaded once, series preloaded)")
    for sym in symbols[:5]:
        df_old = calculate_rs_line(db=db, symbol=sym, benchmark="^TASI.SR",
                                   start_date=start_str, end_date=end_str, scale_factor=3000)
        sc = _fetch_stock_close(db, sym, start_str, end_str)
        df_new = calculate_rs_line(db=db, symbol=sym, benchmark="^TASI.SR",
                                   start_date=start_str, end_date=end_str, scale_factor=3000,
                                   stock_close=sc, bench_close=bench)
        s_old = df_to_response(df_old, sym, "^TASI.SR")["summary"]
        s_new = df_to_response(df_new, sym, "^TASI.SR", include_series=False)["summary"]
        # include_series=False must not alter summary either:
        s_new2 = df_to_response(df_new, sym, "^TASI.SR")["summary"]
        bars_ok = len(df_old) == len(df_new)
        ident = all(str(s_old[k]) == str(s_new[k]) == str(s_new2[k]) for k in s_old)
        p(f"   {sym}: bars={len(df_old)}/{len(df_new)} identical={ident}")
        if not ident:
            for k in s_old:
                if str(s_old[k]) != str(s_new[k]):
                    p(f"      DIFF {k}: old={s_old[k]} new={s_new[k]}")
        assert bars_ok and ident, f"RS MISMATCH {sym}"

    p("\nPARITY: ALL CHECKS PASSED")
finally:
    db.close()

with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "_parity_out.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(OUT))
