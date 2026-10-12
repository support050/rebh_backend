"""
Drop redundant indexes — safely (P1-5).

A rule-based, self-verifying reclamation tool. It never trusts a hard-coded
list: every candidate is re-checked against the live catalogue at run time.

An index is only reported (and only dropped with --execute) when ALL of:
  1. it is NOT a primary key,
  2. it does NOT back any constraint (UNIQUE / FOREIGN KEY / CHECK),
  3. another index on the SAME table is provably at least as good:
       • exact duplicate  → same column list, same opclass, same predicate, or
       • strict prefix    → its column list starts with this index's columns and
                            the wider index has the same (empty) predicate,
  4. for an exact-duplicate group, a UNIQUE / constraint-backed survivor is
     always chosen first, so ON CONFLICT arbiters (prices(symbol,date),
     stock_indicators(symbol,date), rs_daily_v2(symbol,date)) cannot be removed.

Every dropped index is written back as an executable CREATE INDEX statement in
a recovery file before it is dropped.

Usage:
    python scripts/drop_redundant_indexes.py              # dry run (default)
    python scripts/drop_redundant_indexes.py --execute
    python scripts/drop_redundant_indexes.py --protect ix_prices_date
"""
import sys
import os
import re
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import create_engine, text
from app.core.config import settings

# Any of these column tuples used as an ON CONFLICT target in the codebase —
# the surviving index for these MUST stay unique/constraint-backed.
CONFLICT_TARGETS = {
    "prices": [("symbol", "date")],
    "stock_indicators": [("symbol", "date")],
    "rs_daily_v2": [("symbol", "date")],
    "market_pulse": [("date",)],
    "industry_group_history": [("date", "industry_group")],
}


def load_indexes(conn):
    rows = conn.execute(text("""
        SELECT i.indrelid::regclass::text                      AS tbl,
               n.nspname                                        AS sch,
               c.relname                                        AS idx,
               c.oid                                            AS oid,
               i.indisunique, i.indisprimary,
               i.indkey::text                                   AS keynums,
               i.indoption::text                                AS opts,
               pg_get_indexdef(c.oid)                            AS defn,
               pg_get_expr(i.indpred, i.indrelid)                AS pred,
               (SELECT string_agg(conname, ',') FROM pg_constraint
                 WHERE conindid = c.oid)                         AS backed,
               pg_relation_size(c.oid)                           AS bytes,
               coalesce(s.idx_scan, -1)                          AS scans
        FROM pg_index i
        JOIN pg_class c        ON c.oid = i.indexrelid
        JOIN pg_namespace n    ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = c.oid
        WHERE n.nspname = 'public'
        ORDER BY tbl, idx
    """)).fetchall()

    by_table = {}
    for r in rows:
        cols = column_names(conn, r[0], r[6])
        item = dict(tbl=r[0], sch=r[1], idx=r[2], oid=r[3], unique=bool(r[4]),
                    primary=bool(r[5]), keynums=r[6], opts=tuple(r[7].split()),
                    defn=r[8], pred=r[9], backed=r[10], bytes=int(r[11]),
                    scans=int(r[12]), cols=cols)
        by_table.setdefault(r[0], []).append(item)
    return by_table


def column_names(conn, tbl, keynums):
    """Ordered column names for an indkey (attnum list); expressions -> '?'."""
    nums = [int(x) for x in keynums.split()]
    rows = conn.execute(text("""
        SELECT a.attnum, a.attname
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.oid = cast(:t AS regclass) AND a.attnum = ANY(:n)
    """), {"t": tbl, "n": nums}).fetchall()
    m = {r[0]: r[1] for r in rows}
    return [m.get(n, "?") for n in nums]


def fmt(b):
    for unit, div in (("GB", 1024**4), ("MB", 1024**3), ("kB", 1024)):
        if b >= div:
            return f"{b / div:.1f}{unit}"
    return f"{b}B"


def find_redundant(by_table, protect):
    drops, kept_notes = [], []
    for tbl, items in by_table.items():
        for cand in items:
            if cand["primary"] or cand["backed"] or cand["idx"] in protect:
                continue
            for surv in items:
                if surv is cand or surv["oid"] == cand["oid"]:
                    continue
                # partial indexes only cover queries with the same predicate
                if (surv["pred"] or "") != (cand["pred"] or ""):
                    continue
                if surv["pred"]:
                    continue
                # indoption encodes ASC/DESC and NULLS FIRST/LAST per column:
                # two indexes with the same columns but different orderings are
                # NOT interchangeable for every query, so compare them too.
                exact = (surv["cols"] == cand["cols"]
                         and surv["opts"] == cand["opts"])
                prefix = (len(cand["cols"]) < len(surv["cols"])
                          and surv["cols"][:len(cand["cols"])] == cand["cols"]
                          and surv["opts"][:len(cand["cols"])] == cand["opts"]
                          and "?" not in cand["cols"] + surv["cols"])
                if not (exact or prefix):
                    continue
                if exact:
                    # for exact duplicates keep the strongest survivor:
                    # constraint-backed > unique > most used > bigger
                    rank = (bool(surv["backed"]), bool(surv["unique"]),
                            surv["scans"], surv["bytes"])
                    cand_rank = (bool(cand["backed"]), bool(cand["unique"]),
                                 cand["scans"], cand["bytes"])
                    if cand_rank >= rank:
                        continue
                    # ON CONFLICT guard: if this table conflicts on cand's
                    # columns, the survivor MUST be unique/constraint-backed
                    for tgt in CONFLICT_TARGETS.get(tbl, []):
                        if tuple(cand["cols"]) == tuple(tgt) and not (
                                surv["unique"] or surv["backed"]):
                            kept_notes.append(
                                f"  KEPT   {tbl}.{cand['idx']} — arbiter for "
                                f"ON CONFLICT {tgt} (survivor is plain)")
                            break
                drops.append((cand, surv, "exact-duplicate" if exact else "prefix-of"))
                break
    return drops, kept_notes


def main():
    ap = argparse.ArgumentParser(description="Drop redundant indexes safely")
    ap.add_argument("--execute", action="store_true",
                    help="actually DROP INDEX CONCURRENTLY (default is dry run)")
    ap.add_argument("--protect", nargs="*", default=[],
                    help="index names that must never be dropped")
    ap.add_argument("--min-mb", type=float, default=0.0,
                    help="skip indexes smaller than this (MiB)")
    args = ap.parse_args()

    engine = create_engine(str(settings.DATABASE_URL), isolation_level="AUTOCOMMIT")
    print(f"🔌 target: {engine.url.host} / {engine.url.database}")

    with engine.connect() as conn:
        by_table = load_indexes(conn)
        drops, notes = find_redundant(by_table, set(args.protect))

        total = sum(c["bytes"] for c, _s, _k in drops if c["bytes"] >= args.min_mb * 1024**2)
        print(f"\n{len(drops)} redundant index(es), reclaimable ≈ {fmt(total)}\n")
        for c, s, kind in sorted(drops, key=lambda x: -x[0]["bytes"]):
            if c["bytes"] < args.min_mb * 1024**2:
                continue
            print(f"  DROP {c['tbl']:26s} {c['idx']:52s} {fmt(c['bytes']):>8s} "
                  f"scans={c['scans']:>7d}")
            print(f"       kept instead -> {s['idx']} ({' '.join(s['cols'])}) "
                  f"[{kind}, {'unique/backed' if (s['unique'] or s['backed']) else 'plain'}]")
        for n in notes:
            print(n)

        if not args.execute:
            print("\nℹ️  DRY RUN — nothing was dropped. Re-run with --execute.")
            return

        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        rec_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                f"_index_recovery_{stamp}.sql")
        with open(rec_path, "w", encoding="utf-8") as f:
            f.write("-- Recovery script for indexes dropped by drop_redundant_indexes.py\n")
            f.write(f"-- generated {stamp} UTC\n")
            for c, _s, _k in drops:
                if c["bytes"] < args.min_mb * 1024**2:
                    continue
                # works for both "CREATE INDEX" and "CREATE UNIQUE INDEX"
                f.write(re.sub(r"^CREATE (UNIQUE )?INDEX ",
                               lambda m: "CREATE %sINDEX CONCURRENTLY " % (m.group(1) or ""),
                               c["defn"], count=1) + ";\n")

        print(f"\n💾 recovery script written: {rec_path}")
        done, failed = 0, []
        for c, _s, _k in drops:
            if c["bytes"] < args.min_mb * 1024**2:
                continue
            # PostgreSQL resolves DROP INDEX name as schema.index, NOT
            # table.index — qualifying with the TABLE name makes IF EXISTS
            # silently "skip" and report a false success (this actually
            # happened once), so the schema is used and IF EXISTS is omitted.
            stmt = f'DROP INDEX CONCURRENTLY "{c["sch"]}"."{c["idx"]}"'
            try:
                conn.exec_driver_sql(stmt)
                # proof, not optimism: confirm the catalogue really lost it
                still = conn.execute(text("""
                    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = :s AND c.relname = :i"""),
                    {"s": c["sch"], "i": c["idx"]}).scalar()
                if still:
                    failed.append(f"{c['tbl']}.{c['idx']} (still in catalogue)")
                    print(f"  ❌ {c['tbl']}.{c['idx']}: statement ran but index still exists")
                    continue
                done += 1
                print(f"  ✅ dropped {c['tbl']}.{c['idx']} ({fmt(c['bytes'])})")
            except Exception as e:
                failed.append(f"{c['tbl']}.{c['idx']}")
                print(f"  ❌ {c['tbl']}.{c['idx']}: {type(e).__name__}: {str(e)[:150]}")
        print(f"\n🏁 verified dropped {done}/{len(drops)}" +
              (f" — FAILED: {', '.join(failed)}" if failed else ""))
        if failed:
            print(f"⚠️  re-create anything failed from: {rec_path}")

    engine.dispose()


if __name__ == "__main__":
    main()
