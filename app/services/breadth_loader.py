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
- Logs every run into breadth_runs (status success/partial/failed)
- DATABASE_URL is read from backend/.env

TABLE ISOLATION (2026-10-25): every table gets its OWN transaction and its own retries,
and the breadth_data snapshot + the breadth_runs row are written even when a table fails.
Before that, one `with eng.begin()` wrapped all 14 upserts: a single bad chunk rolled back
the other 13 tables AND skipped the snapshot, which is why a failed run had to be finished
by hand with `breadth_job.py --snapshot-only`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import sqlalchemy as sa

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

# --- write sizing ---------------------------------------------------------------------
# psycopg2 interpolates client-side, so a 5000 x 26 chunk never reaches the Postgres
# protocol's 65 535-bind-parameter cap (measured 2026-10-25: a 155 000-value VALUES list
# is accepted by this server). But it IS a ~1.5 MB SQL string crossing the Render SSL
# link, and when the server rejects anything in it SQLAlchemy dumps all 130 000 bound
# values into the traceback — unreadable, and it buried the real error on 2026-10-25.
# A bounded statement keeps both the write and its errors sane.
PARAM_BUDGET = 20_000
CH_MIN, CH_MAX = 200, 2_000
RETRIES = 3
JSON_MARKER = "__BREADTH_JSON__ "        # stdout contract with breadth_job.py


def chunk_rows(ncols: int) -> int:
    """Rows per INSERT so that rows * cols stays inside PARAM_BUDGET."""
    return max(CH_MIN, min(CH_MAX, PARAM_BUDGET // max(1, int(ncols))))


def db_url() -> str:
    for line in (BACKEND / ".env").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*DATABASE_URL\s*=\s*(.+)", line)
        if m:
            url = m.group(1).strip().strip('"').strip("'")
            if url.startswith("postgresql://"):
                url = "postgresql+psycopg2://" + url[len("postgresql://"):]
            return url
    raise SystemExit(f"DATABASE_URL not found in {BACKEND / '.env'}")


def _note(col, why: str) -> None:
    """Count one value that had to be dropped/truncated, per (table.column, reason)."""
    key = f"{getattr(col, 'table', None) and col.table.name or '?'}.{col.name} [{why}]"
    _FIXES[key] = _FIXES.get(key, 0) + 1


def _cast(v, col):
    """Fit a python-native value to the Postgres type of `col`.

    Guards the three server-side rejections that all surface as StatementError (e3q8):
    `numeric field overflow`, `invalid input syntax for type numeric` and
    `value too long for type character varying(n)`.
    """
    if col is None:
        return v
    t = col.type
    if isinstance(t, (sa.Integer, sa.BigInteger)):
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            _note(col, "not a number")
            return None
    if isinstance(t, sa.Numeric):                       # Float/Decimal/PG DOUBLE too
        try:
            x = float(v)
        except (TypeError, ValueError):
            _note(col, "not a number")
            return None
        if math.isnan(x) or math.isinf(x):
            _note(col, "nan/inf")
            return None
        p = int(t.precision or 18)
        s = int(t.scale if t.scale is not None else 6)
        x = round(x, s)
        if abs(x) >= 10 ** max(0, p - s):               # would raise numeric field overflow
            _note(col, f"outside numeric({p},{s})")
            return None
        return x
    if isinstance(t, sa.Date):                          # Date subclasses DateTime — check 1st
        if isinstance(v, dt.datetime):
            return v.date()
        if isinstance(v, dt.date):
            return v
        try:
            return pd.Timestamp(v).date()
        except (TypeError, ValueError):
            _note(col, "not a date")
            return None
    if isinstance(t, sa.DateTime):
        if isinstance(v, dt.datetime):
            return v
        if isinstance(v, dt.date):
            return dt.datetime(v.year, v.month, v.day)
        try:
            return pd.Timestamp(v).to_pydatetime()
        except (TypeError, ValueError):
            _note(col, "not a timestamp")
            return None
    if isinstance(t, sa.String):                        # String covers Text; Text has no length
        s = v if isinstance(v, str) else str(v)
        if t.length and len(s) > t.length:
            _note(col, f"truncated to {t.length}")
            s = s[:t.length]
        return s
    return v


_FIXES: dict[str, int] = {}


def _clean(v, col=None):
    """parquet/python value -> DB-safe value for `col` (NULL for None/NaN/NaT/pd.NA)."""
    if v is None:
        return None
    if isinstance(v, np.generic):
        v = v.item()                        # np.float64/int64/bool_ -> python native
    elif isinstance(v, np.ndarray):
        v = v.tolist()                      # never expected; keeps pd.isna() elementwise
    if v.__class__.__name__ in ("NaTType", "NAType"):   # to_datetime(coerce) / pd.NA
        return None
    if isinstance(v, (list, tuple, dict, set)):
        return v
    if bool(pd.isna(v)):
        return None
    if isinstance(v, pd.Timestamp):
        v = v.to_pydatetime()
    if isinstance(v, bool) or v.__class__.__name__ == "bool_":
        v = int(v)                          # engine flags bool -> 0/1 (Numeric cols)
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return _cast(v, col)


def _records(df: pd.DataFrame, colmap: dict | None = None) -> list[dict]:
    if "date" in df.columns:
        df = df.assign(date=pd.to_datetime(df["date"]))
    colmap = colmap or {}
    return [{k: _clean(v, colmap.get(k)) for k, v in row.items()}
            for row in df.to_dict("records")]


def upsert(bind, model, df: pd.DataFrame, ch: int | None = None) -> int:
    table = model.__table__
    cols = [c.name for c in table.columns]
    colmap = {c.name: c for c in table.columns}
    extra = [c for c in df.columns if c not in cols]
    if extra:
        print(f"[warn] {table.name}: parquet cols not in model (ignored): {extra}",
              file=sys.stderr)
    df = df[[c for c in df.columns if c in cols]]
    pk = [c for c in PKS[table.name] if c in cols]
    # two identical PKs inside ONE statement is fatal ("ON CONFLICT ... cannot affect
    # row a second time"), so keep the last reading of a day instead of losing the table
    n0 = len(df)
    df = df.drop_duplicates(subset=pk, keep="last")
    if len(df) != n0:
        print(f"[fix] {table.name}: {n0 - len(df)} duplicate {'+'.join(pk)} rows collapsed",
              file=sys.stderr)
    upd = [c for c in cols if c not in pk]
    recs = _records(df, colmap)
    ch = ch or chunk_rows(len(cols))
    for i in range(0, len(recs), ch):
        chunk = recs[i:i + ch]
        ins = pg_insert(table).values(chunk)
        bind.execute(ins.on_conflict_do_update(index_elements=pk,
                                               set_={c: ins.excluded[c] for c in upd}))
    return len(recs)


def _err_brief(exc: BaseException) -> str:
    """The server's own message, WITHOUT SQLAlchemy's bound-values dump."""
    orig = getattr(exc, "orig", None) or exc
    msg = getattr(orig, "pgerror", None) or str(orig)
    lines = [l.strip() for l in str(msg).splitlines() if l.strip()][:3]
    diag = getattr(orig, "diag", None)
    detail = getattr(diag, "message_detail", None) if diag is not None else None
    out = f"{exc.__class__.__name__}/{orig.__class__.__name__}: " + " | ".join(lines)
    if detail:
        out += f" | {str(detail)[:160]}"
    return out[:400]


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
            print(f"[snapshot] attempt {attempt}/{retries} failed: {_err_brief(exc)}",
                  file=sys.stderr, flush=True)
            eng.dispose()
            time.sleep(5 * attempt)
    raise SystemExit(f"breadth_data snapshot failed after {retries} attempts: "
                     f"{_err_brief(last) if last is not None else 'unknown'}")


def load_table(eng, fname: str, model, feat: Path) -> tuple:
    """One parquet -> one table, in its OWN transaction, retried on a dead link.

    Returns (report_value, error): an int row count on success, a short error string on
    failure. A table that cannot load costs itself only — the other 13 tables, the
    breadth_data snapshot and the breadth_runs row still go through.
    """
    tname = model.__tablename__
    pq = feat / f"{fname}.parquet"
    if not pq.exists():
        return "MISSING parquet", f"no {pq.name} in {pq.parent}"
    df = pd.read_parquet(pq)
    _FIXES.clear()
    err = None
    for attempt in range(1, RETRIES + 1):
        try:
            with eng.begin() as bind:
                rows = upsert(bind, model, df)
            for k, n in sorted(_FIXES.items()):
                print(f"[fix] {k}: {n} values", file=sys.stderr, flush=True)
            return rows, None
        except Exception as exc:                       # noqa: BLE001 — isolation is the point
            err = _err_brief(exc)
            print(f"[{tname}] attempt {attempt}/{RETRIES} failed: {err}",
                  file=sys.stderr, flush=True)
            eng.dispose()                              # drop any half-dead pooled link
            if attempt < RETRIES:
                time.sleep(3 * attempt)
    return (err or "failed"), (err or "failed")


def run_asof(feat: Path):
    """Last trading date of this run — never None.

    breadth_runs.asof is NOT NULL: an empty/absent composite.parquet used to make the
    ledger INSERT fail, and because the ledger shared its transaction with all 14 upserts
    that rolled the whole load back (which is why breadth_runs stopped at 2026-10-21 while
    the feature tables kept moving — the ledger and the data were never in agreement).
    """
    for name in ("composite.parquet", "participation.parquet", "widget.parquet"):
        p = feat / name
        if not p.exists():
            continue
        try:
            d = pd.to_datetime(pd.read_parquet(p, columns=["date"])["date"]).max()
            if not pd.isna(d):
                return d.date()
        except Exception:                                  # noqa: BLE001 — log field only
            continue
    return dt.date.today()


def log_run(eng, report: dict, errors: dict, t0: float, feat: Path) -> None:
    """breadth_runs row, in its own transaction: a failed log must never fail the run."""
    if not errors:
        status = "success"
    elif all(not isinstance(v, int) for v in report.values()):
        status = "failed"
    else:
        status = "partial"
    try:
        with eng.begin() as bind:
            bind.execute(BreadthRun.__table__.insert().values(
                asof=run_asof(feat),
                status=status,
                source_mode="db-direct",
                seconds=round(time.time() - t0, 1),
                tables_loaded=report,                 # real JSON object, not str(dict)
                error="; ".join(f"{k}: {v}" for k, v in errors.items()) or None,
                finished_at=dt.datetime.now(),
            ))
    except Exception as exc:                           # noqa: BLE001
        eng.dispose()
        print(f"[warn] breadth_runs row not written: {_err_brief(exc)}",
              file=sys.stderr, flush=True)


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
        rep = {"breadth_data": write_snapshot(eng, a.snapshot)}
        eng.dispose()
        return rep

    if not feat.exists():
        raise SystemExit(f"no features dir: {feat} — run breadth_job first")
    wanted = {k: v for k, v in PARQUET_MODELS.items() if not a.only or k in a.only.split(",")}
    report, errors, t0 = {}, {}, time.time()

    for fname, model in wanted.items():
        val, err = load_table(eng, fname, model, feat)
        report[model.__tablename__] = val
        if err:
            errors[model.__tablename__] = err

    # breadth_data runs even when a table failed: it is the payload the API reads, and a
    # table failure used to force a second manual run with --snapshot-only.
    if a.snapshot:
        try:
            report["breadth_data"] = write_snapshot(eng, a.snapshot)
        except (SystemExit, Exception) as exc:         # noqa: BLE001
            msg = (str((getattr(exc, "args", None) or ("",))[0]) or _err_brief(exc))[:300]
            errors["breadth_data"] = msg
            report["breadth_data"] = f"FAILED: {msg}"
            eng.dispose()

    log_run(eng, report, errors, t0, feat)
    eng.dispose()
    return report


def _ok(v) -> bool:
    """Report values: int row count for a table, info dict for breadth_data."""
    return isinstance(v, (int, dict))


if __name__ == "__main__":
    rep = main()
    # One marker-prefixed JSON line is the contract with breadth_job.py: [warn]/[fix] lines
    # on stdout can no longer be confused with the report (the old greedy
    # re.search(r"\\{.*\\}") on the parent side could), and JSON not a dict repr because
    # json.loads cannot read single quotes.
    print(JSON_MARKER + json.dumps(rep, ensure_ascii=False, default=str), flush=True)
    if any(not _ok(v) for v in rep.values()):
        sys.exit(1)
