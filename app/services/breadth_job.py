# -*- coding: utf-8 -*-
"""breadth_job.py — the DB-DIRECT daily job (our architecture decision, 2026-10-01).

No CSV files anywhere: pulls `prices` + `historical_reports` straight from our Postgres and
feeds the vendored engine at runtime by overriding ONLY its two raw-loader functions
(breadth.data.build.load_prices_csv / load_index_csv / file_sha256). Zero edits to vendor
files; all calculation code untouched.

Run in the ONE project venv (single-environment rule, 2026-10-07) — from backend/ :
  ..\venv\Scripts\python.exe -X utf8 app/services/breadth_job.py
The engine's pandas-2-only call sites are version-detected inside the vendored code
(see breadth/indicators/sectors.py), so pandas 1.5.x and 2.x both work.

Equivalent of `python -m breadth.daily`, but input = live database.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

HERE = Path(__file__).resolve().parent                 # backend/app/services
ENGINE_DIR = HERE / "breadth_engine"
BACKEND_DIR = HERE.parents[1]                           # .../backend
LOADER_SCRIPT = HERE / "breadth_loader.py"
LOADER_MARKER = "__BREADTH_JSON__ "        # must match breadth_loader.JSON_MARKER
sys.path.insert(0, str(ENGINE_DIR))                    # make `breadth` importable

try:
    from breadth.data.loader import RAW_COLUMNS            # noqa: E402  (shape contract, read-only)
except ModuleNotFoundError as exc:
    # A raw "ModuleNotFoundError: No module named 'yaml'" from inside a vendored file
    # reads like a code defect (it cost a full diagnosis cycle on 2026-10-07). It is
    # always an interpreter choice: the engine needs pyyaml + pyarrow (+scipy, +matplotlib
    # for the report path) in whichever python runs this file.
    missing = getattr(exc, "name", "?")
    raise SystemExit(
        f"breadth_job: this interpreter has no '{missing}'.\n"
        f"  running as: {sys.executable}\n"
        f"  the vendored engine imports '{missing}' at breadth/__init__.py — install it"
        f" into the project venv:\n"
        f'    "{sys.executable}" -m pip install {missing}\n'
        "  (the engine stack also needs: pyarrow, and scipy/matplotlib for the report path)")

ENV_FILE = HERE.parents[1] / ".env"


def db_url() -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*DATABASE_URL\s*=\s*(.+)", line)
        if m:
            url = m.group(1).strip().strip('"').strip("'")
            if url.startswith("postgresql://"):
                url = "postgresql+psycopg2://" + url[len("postgresql://"):]
            return url
    raise SystemExit(f"DATABASE_URL not found in {ENV_FILE}")


# SQL -> exactly the 17 contract column names the engine's rename step expects.
PRICE_SQL = text('''
    SELECT industry_group   AS "Industry Group",
           symbol           AS "Symbol",
           company_name     AS "Company Name",
           date             AS "Date",
           open             AS "Open",
           high             AS "High",
           low              AS "Low",
           close            AS "Close",
           change           AS "Change",
           change_percent   AS "% Change",
           volume_traded    AS "Volume Traded",
           value_traded_sar AS "Value Traded (SAR)",
           no_of_trades     AS "No. of Trades",
           market_cap       AS "Market Cap",
           sector           AS "Sector",
           industry         AS "Industry",
           sub_industry     AS "Sub Industry"
    FROM prices ORDER BY symbol, date
''')

INDEX_SQL = text('''
    SELECT report_date    AS "Date",
           open_price     AS "Open",
           high_price     AS "High",
           low_price      AS "Low",
           close_price    AS "Close",
           volume_traded  AS "Volume Traded",
           value_traded   AS "Value Traded",
           no_of_trades   AS "No. of Trades"
    FROM historical_reports ORDER BY report_date
''')


def db_prices(bind) -> pd.DataFrame:
    """Replicates loader.load_prices_csv parsing 1:1 (typed columns, sorted, source_row_id)."""
    df = pd.read_sql(PRICE_SQL, bind)
    df.columns = [str(c).strip() for c in df.columns]
    missing = set(RAW_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"price query missing columns: {sorted(missing)}")
    df = df.rename(columns=RAW_COLUMNS)
    df["date"] = pd.to_datetime(df["date"], errors="raise")
    df["symbol"] = pd.to_numeric(df["symbol"], errors="raise").astype("int32")
    for c in ["open", "high", "low", "close", "volume", "value", "trades", "market_cap",
              "src_change", "src_pct_change"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    for c in ["industry_group", "name", "sector", "industry", "sub_industry"]:
        df[c] = df[c].astype("string").str.strip()
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df["source_row_id"] = df.index.astype("int64")
    if df.duplicated(["symbol", "date"]).any():
        raise ValueError("Duplicate (symbol, date) rows in prices table")
    return df


def db_index(bind) -> pd.DataFrame:
    """Replicates loader.load_index_csv (handles stored strings with thousands separators)."""
    df = pd.read_sql(INDEX_SQL, bind)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.rename(columns=RAW_COLUMNS)          # 'Date'->date, 'Open'->open, ...
    df = df.rename(columns={"Value Traded": "value", "src_trades": "trades"})
    df["date"] = pd.to_datetime(df["date"].astype("string").str.replace(",", "", regex=False),
                                errors="raise")
    for c in ["open", "high", "low", "close", "volume", "value", "trades"]:
        s = df[c].astype("string").str.replace(",", "", regex=False).str.strip()
        s = s.replace({"-": None, "": None})
        df[c] = pd.to_numeric(s, errors="coerce").astype("float64")
    df = df.sort_values("date").reset_index(drop=True)
    if df["date"].duplicated().any():
        raise ValueError("Duplicate dates in historical_reports")
    df["index_code"] = "TASI"
    return df


def _db_engine():
    """Engine with pre-ping for the READ side.

    The run reads ~1M rows, then computes for ~10 minutes before touching the DB again;
    a pooled connection left that long is dead and the next statement fails as
    'psycopg2.OperationalError: SSL connection has been closed unexpectedly'. pre_ping
    discards/reshapes it before use, recycle stays well under Render's idle cutoff.
    """
    return create_engine(db_url(), pool_pre_ping=True, pool_recycle=180)


def _read_df(fn, attempts: int = 3):
    """Run one db_* reader on a FRESH connection, retrying transient drops.

    pre-ping only protects a connection BEFORE it is used; a 1M-row fetch can still
    kill the SSL link mid-transfer and every later statement on that connection then
    dies ('server closed the connection unexpectedly' — crashed the 2026-10-07 15:05
    run inside db_index). One connection per query + bounded retries fixes that.
    """
    last: Exception | None = None
    for i in range(1, attempts + 1):
        eng = _db_engine()
        try:
            with eng.connect() as bind:
                df = fn(bind)
            return df
        except Exception as exc:  # psycopg2 OperationalError / InterfaceError etc.
            last = exc
            print(f"[db] {fn.__name__} attempt {i}/{attempts} failed: "
                  f"{exc.__class__.__name__}: {str(exc)[:160]}", flush=True)
            time.sleep(3 * i)
        finally:
            eng.dispose()
    raise SystemExit(f"breadth_job: cannot read from DB after {attempts} attempts: {last}")


def _err_head(text: str, limit: int = 1200) -> str:
    """The readable part of the loader's output: short lines, no parameter dumps.

    A SQLAlchemy StatementError ends by printing every bound value, so slicing the TAIL
    of the child's output (what this used to do) returned only that dump — the actual
    Postgres message was thrown away and the 2026-10-25 failure could not be diagnosed
    from the job's own log. The loader now reports via `_err_brief()`; this is the net.
    """
    keep = [l.rstrip() for l in (text or "").splitlines()
            if l.strip() and len(l) <= 300 and "Background on this error" not in l]
    body = keep if len(keep) <= 16 else keep[:10] + ["…"] + keep[-6:]
    return "\n".join(body)[:limit]


def _parse_loader_report(stdout: str):
    """Read the loader's marker line; fall back to the legacy bare-JSON line."""
    for line in (stdout or "").splitlines():
        if line.startswith(LOADER_MARKER):
            return json.loads(line[len(LOADER_MARKER):])
    m = re.search(r"\{[^\n]*\}\s*$", stdout or "", re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _run_loader(snapshot_html=None, snapshot_only=False) -> dict:
    """Every Postgres write happens in breadth_loader.py, under THIS same interpreter.

    Single-environment rule (2026-10-07): no second venv — the loader is spawned with
    sys.executable, so compute and DB writes share the project's pandas/SQLAlchemy.
    (Historical note: the 8 MB breadth_data jsonb push fails on SQLAlchemy>=2.1 over
    this SSL link and lands in ~28 s on the project stack 2.0.x — measured 2026-10-07.
    That is a reason to stay on the project interpreter, never to add another one.)

    A table failure is NO LONGER fatal here: the loader isolates each table, always
    writes breadth_data and breadth_runs, and returns the per-table report, so one
    command completes the pipeline (see breadth_loader.load_table).
    """
    cmd = [sys.executable, "-X", "utf8", str(LOADER_SCRIPT)]
    if snapshot_html:
        cmd += ["--snapshot", str(snapshot_html)]
    if snapshot_only:
        cmd += ["--snapshot-only"]
    proc = subprocess.run(cmd, cwd=str(BACKEND_DIR), capture_output=True, text=True)
    rep = _parse_loader_report(proc.stdout)
    if rep is None:
        # nothing parseable = the loader itself died before reporting; show the head
        raise SystemExit(f"breadth_loader failed (rc={proc.returncode}):\n"
                         + _err_head((proc.stderr or "") + "\n" + (proc.stdout or "")))
    bad = {k: v for k, v in rep.items() if not isinstance(v, (int, dict))}
    if bad:
        # loud, but the run is done: breadth_data landed, the rest is already committed
        print(f"[loader] partial — {json.dumps(bad, ensure_ascii=False, default=str)}",
              file=sys.stderr, flush=True)
    if proc.stderr:
        for line in proc.stderr.splitlines():
            if line.startswith(("[fix]", "[warn]", "[snapshot]")):
                print(line, flush=True)
    return rep


def snapshot_only() -> dict:
    """Refresh breadth_data from the latest generated dashboard (no recompute)."""
    html = ENGINE_DIR / "storage" / "reports" / "breadth_dashboard.html"
    if not html.exists():
        raise SystemExit(f"no dashboard to snapshot at {html} — run breadth_job without --snapshot-only")
    return _run_loader(snapshot_html=html, snapshot_only=True)


def run(skip_load: bool = False) -> dict:
    import breadth.daily as daily
    import breadth.data.build as bld

    prices = _read_df(db_prices)
    index_df = _read_df(db_index)
    asof = str(prices["date"].max().date())
    stamp = f"db:prices@{asof}"
    print(f"[db] prices rows={len(prices)} symbols={prices['symbol'].nunique()} "
          f"index rows={len(index_df)} asof={asof}", flush=True)

    # --- runtime input rewire: the ONLY deviation from the CSV flow ---
    bld.load_prices_csv = lambda path: prices
    bld.load_index_csv = lambda path: index_df
    bld.file_sha256 = lambda path: stamp

    status = daily.run(prices="db", index="db")   # args feed cfg paths only; loaders are patched
    status["source"] = {"mode": "db-direct", "rows_prices": int(len(prices)),
                        "rows_index": int(len(index_df)), "asof": asof}
    print("[engine] done: " + json.dumps(status.get("summary", {}), ensure_ascii=False,
                                         default=str), flush=True)

    # --- parquet + DATA snapshot -> Postgres, in the project venv (see _run_loader) ---
    if not skip_load:
        pro_html = status.get("pro_dashboard")
        snap = pro_html if pro_html and Path(pro_html).exists() else None
        fallback = ENGINE_DIR / "storage" / "reports" / "breadth_dashboard.html"
        if not snap and fallback.exists():
            # the engine reports the path it wrote; if that key ever drifts, the dashboard
            # on disk is still the right snapshot source — never make the operator run a
            # second command for it
            snap = str(fallback)
            print(f"[warn] pro_dashboard={pro_html} not found — snapshotting {fallback.name}",
                  file=sys.stderr)
        if not snap:
            # this module logs with print(); breadth_data feeds the API, so a missing
            # dashboard must be loud, not a silent skip
            print(f"[warn] no dashboard HTML at all — breadth_data not refreshed",
                  file=sys.stderr)
        status["loader"] = _run_loader(snapshot_html=snap)
        status["tables_loaded"] = {k: v for k, v in status["loader"].items()
                                   if k != "breadth_data"}
        status["data_snapshot"] = status["loader"].get("breadth_data")
        print("[loader] " + json.dumps(status["loader"], default=str), flush=True)
    return status


if __name__ == "__main__":
    if "--snapshot-only" in sys.argv:
        print(json.dumps(snapshot_only(), ensure_ascii=False))
    else:
        out = run(skip_load="--skip-load" in sys.argv)
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
