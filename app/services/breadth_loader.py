# -*- coding: utf-8 -*-
"""breadth_loader.py — engine parquet workshop -> PostgreSQL breadth_* tables (idempotent upsert).

SINGLE ENVIRONMENT (2026-10-07): runs under the project venv — the same interpreter that
breadth_job.py itself runs on (the job spawns it via sys.executable). From backend/:
  ..\venv\Scripts\python.exe -X utf8 app/services/breadth_loader.py [--only composite,bpi]

- Reads: backend/app/services/breadth_engine/storage/features/*.parquet
- Writes: the SQLAlchemy models in app/models/breadth.py (ON CONFLICT DO UPDATE — safe to re-run)
- Writes the breadth_data JSONB snapshot too when given --snapshot <dashboard.html>; this
  lives HERE, not in breadth_job, on purpose: every Postgres write goes through one
  code path. (The 8 MB jsonb push dies on SQLAlchemy>=2.1 over this SSL link and lands
  in ~28 s on the project stack 2.0.x — measured 2026-10-07; another reason the project
  venv is the only interpreter this pipeline ever uses.)
- Logs every run into breadth_runs (status success/failed)
- DATABASE_URL is read from backend/.env
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

import pandas as pd

BACKEND = Path(__file__).resolve().parents[2]          # backend/
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, text                          # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert      # noqa: E402

from app.models.breadth import (                                    # noqa: E402
    BreadthAdvanceDecline, BreadthBpi, BreadthClassic, BreadthComposite,
    BreadthCompositePercentiles, BreadthConcentration, BreadthDivergences,
    BreadthMomentum, BreadthParticipation, BreadthRun, BreadthSectors,
    BreadthSectorsSummary, BreadthStagesWeekly, BreadthThrusts, BreadthWidget,
)

ENGINE_ROOT = BACKEND / "app" / "services" / "breadth_engine"
FEATURES = ENGINE_ROOT / "storage" / "features"

# parquet name -> SQLAlchemy model (14 result tables)
PARQUET_MODELS = {
    "participation": BreadthParticipation,
    "advance_decline": BreadthAdvanceDecline,
    "momentum": BreadthMomentum,
    "thrusts": BreadthThrusts,
    "classic": BreadthClassic,
    "divergences": BreadthDivergences,
    "concentration": BreadthConcentration,
    "widget": BreadthWidget,
    "stages_weekly": BreadthStagesWeekly,
    "bpi": BreadthBpi,
    "sectors": BreadthSectors,
    "sectors_summary": BreadthSectorsSummary,
    "composite": BreadthComposite,
    "composite_percentiles": BreadthCompositePercentiles,
}
# composite PKs per model (owner rule: sectors = date+sector)
PKS = {m.__tablename__: (["date", "sector"] if m is BreadthSectors else ["date"])
       for m in PARQUET_MODELS.values()}


def db_url() -> str:
    for line in (BACKEND / ".env").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*DATABASE_URL\s*=\s*(.+)", line)
        if m:
            url = m.group(1).strip().strip('"').strip("'")
            if url.startswith("postgresql://"):
                url = "postgresql+psycopg2://" + url[len("postgresql://"):]
            return url
    raise SystemExit(f"DATABASE_URL not found in {BACKEND / '.env'}")


def _clean(v):
    """parquet/python value -> DB-safe value (NULL for None/NaN/NaT/pd.NA)."""
    if v is None:
        return None
    if v.__class__.__name__ == "NaTType":        # pd.to_datetime(errors="coerce") writes NaT
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple, dict)) or type(v).__module__.startswith("numpy") \
            and not isinstance(v, np.generic):
        return v                                  # pd.isna() is elementwise on arrays — never here
    if bool(pd.isna(v)):                          # NaN, NaT, pandas <NA> (2nd arg = useNaT default)
        return None
    if isinstance(v, pd.Timestamp):
        return v.date()
    if isinstance(v, bool) or v.__class__.__name__ == "bool_":
        return int(v)                   # engine flags bool -> 0/1 (Numeric cols)
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _records(df: pd.DataFrame) -> list[dict]:
    if "date" in df.columns:
        df = df.assign(date=pd.to_datetime(df["date"]))
    return [{k: _clean(v) for k, v in row.items()} for row in df.to_dict("records")]


def upsert(bind, model, df: pd.DataFrame) -> int:
    table = model.__table__
    cols = [c.name for c in table.columns]
    extra = [c for c in df.columns if c not in cols]
    if extra:
        print(f"[warn] {table.name}: parquet cols not in model (ignored): {extra}")
    df = df[[c for c in df.columns if c in cols]]
    pk = PKS[table.name]
    upd = [c for c in cols if c not in pk]
    recs = _records(df)
    CH = 5000
    for i in range(0, len(recs), CH):
        chunk = recs[i:i + CH]
        ins = pg_insert(table).values(chunk)
        bind.execute(ins.on_conflict_do_update(index_elements=pk,
                                               set_={c: ins.excluded[c] for c in upd}))
    return len(recs)


DATA_RETENTION_DAYS = 14


def extract_data(html_path) -> dict:
    """Pull the embedded `const DATA={...}` object out of a generated dashboard.

    Brace matcher (not a regex) because the object contains strings with braces.
    Reimplemented here instead of imported from breadth_job so that this process never
    needs the vendored engine dir on sys.path (keeps the loader standalone).
    """
    txt = Path(html_path).read_text(encoding="utf-8")
    i = txt.index("const DATA=")
    start = txt.index("{", i)
    depth = 0
    in_str = esc = False
    for j in range(start, len(txt)):
        ch = txt[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(txt[start:j + 1])
    raise ValueError(f"DATA object not found in {html_path}")


def store_data_snapshot(bind, trading_date: str, data: dict) -> dict:
    """UPSERT the DATA JSONB row, then prune to the rolling window (no extra jobs).
    Idempotent: rerunning a date overwrites its snapshot."""
    blob = json.dumps(data, separators=(",", ":"))
    bind.execute(text(
        "INSERT INTO breadth_data (trading_date, engine_version, data) "
        "VALUES (:d, :v, CAST(:j AS jsonb)) "
        "ON CONFLICT (trading_date) DO UPDATE SET "
        "engine_version = EXCLUDED.engine_version, data = EXCLUDED.data, created_at = now()"),
        {"d": trading_date, "v": "breadth-v3", "j": blob})
    pruned = bind.execute(text(
        f"DELETE FROM breadth_data WHERE trading_date < "
        f"CURRENT_DATE - INTERVAL '{DATA_RETENTION_DAYS} days'")).rowcount
    return {"trading_date": trading_date, "json_bytes": len(blob), "pruned_rows": pruned}


def write_snapshot(eng, html_path: str, retries: int = 6) -> dict:
    """Separate transaction + retries for the 8 MB jsonb.

    It must not share the tables' transaction: a dropped socket on the snapshot would
    otherwise roll back the 14 feature tables that already succeeded. The payload takes
    ~30 s to cross this link, so a retry loop is the difference between an intermittent
    failure and a finished run. Retries are generous (6, linear backoff) because on
    2026-10-07 a transient Render blip (SSL drop + FATAL "database system is in
    recovery") ate 3 quick attempts while a manual push 2 minutes later landed on
    attempt 1 — the server needed >9 s, not a faster loop.
    """
    data = extract_data(html_path)
    asof = data["today"]["date"]
    last = None
    for attempt in range(1, retries + 1):
        try:
            with eng.begin() as bind:
                info = store_data_snapshot(bind, asof, data)
            info["attempts"] = attempt
            info["source_html"] = str(html_path)
            return info
        except Exception as exc:
            last = exc
            print(f"[snapshot] attempt {attempt}/{retries} failed: "
                  f"{type(exc).__name__}: {str(exc)[:160]}", file=sys.stderr)
            eng.dispose()
            time.sleep(5 * attempt)
    raise SystemExit(f"breadth_data snapshot failed after {retries} attempts: {last}")


def main() -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma list of parquet names")
    ap.add_argument("--engine-root", default=str(ENGINE_ROOT))
    ap.add_argument("--snapshot", help="dashboard HTML whose `const DATA` goes into breadth_data")
    ap.add_argument("--snapshot-only", action="store_true",
                    help="write breadth_data only; skip the parquet tables")
    a = ap.parse_args()

    feat = Path(a.engine_root) / "storage" / "features"
    eng = create_engine(db_url(), pool_pre_ping=True, pool_recycle=180)

    if a.snapshot_only:
        # used by `breadth_job.py --snapshot-only`: refresh breadth_data from an already
        # generated dashboard, no recompute and no table upserts.
        if not a.snapshot:
            raise SystemExit("--snapshot-only requires --snapshot <dashboard html>")
        return {"breadth_data": write_snapshot(eng, a.snapshot)}

    if not feat.exists():
        raise SystemExit(f"no features dir: {feat} — run breadth_job first")
    wanted = {k: v for k, v in PARQUET_MODELS.items() if not a.only or k in a.only.split(",")}
    report, t0 = {}, time.time()

    with eng.begin() as bind:
        for fname, model in wanted.items():
            pq = feat / f"{fname}.parquet"
            tname = model.__tablename__
            if not pq.exists():
                report[tname] = "MISSING parquet"
                continue
            df = pd.read_parquet(pq)
            report[tname] = upsert(bind, model, df)

        asof = max((d for d in pd.to_datetime(pd.read_parquet(feat / "composite.parquet",
                                                           columns=["date"])["date"])), default=None)
        failed = [k for k, v in report.items() if not isinstance(v, int)]
        bind.execute(BreadthRun.__table__.insert().values(
            asof=asof.date() if asof is not None else None,
            status="failed" if failed else "success",
            source_mode="db-direct",
            seconds=round(time.time() - t0, 1),
            tables_loaded=str(report),
            error=str(failed) if failed else None,
            finished_at=pd.Timestamp.now().to_pydatetime(),
        ))

    # breadth_data runs AFTER the feature tables committed, in its own transaction.
    if a.snapshot:
        report["breadth_data"] = write_snapshot(eng, a.snapshot)
    eng.dispose()
    return report


if __name__ == "__main__":
    rep = main()
    # JSON, not repr: breadth_job parses this stdout with json.loads, and a Python dict
    # repr (single quotes) is not valid JSON.
    print(json.dumps(rep, ensure_ascii=False, default=str))
    if any(not isinstance(v, int) and k != "breadth_data" for k, v in rep.items()):
        sys.exit(1)
