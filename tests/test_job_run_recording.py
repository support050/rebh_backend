"""Regression guard: one run must produce exactly one job_runs row.

breadth_daily is dispatched three ways, and two of them spawn
app/services/breadth_job.py as a SUBPROCESS of the very file that now records
its own terminal runs:

  · cron      scheduler.job_breadth_daily  → track_job(…"cron")   → subprocess
  · Run Now   job_runner._execute          → track_job(…"admin")  → subprocess
  · terminal  the command printed on the card                     → __main__

Since 2026-11-03 the script writes its own row (triggered_by="manual"), which is
what a terminal run always should have done. The subprocesses must therefore be
told NOT to — otherwise every nightly cron fire and every Run Now click writes a
duplicate row, and the card's "last run" becomes whichever of the two closed
last. These tests pin the marker and the shared constant, the two things that
silently break if either side is edited.

breadth_job.py itself cannot be imported here: the backend test venv has no
pyyaml (the vendored engine refuses to load without it — its own guard prints
that), so the child side is checked against the source text.
"""
import subprocess as _subprocess_mod
from pathlib import Path

from app.core import scheduler
from app.services import job_tracking

BREADTH_SCRIPT = (Path(scheduler.__file__).resolve().parents[2]
                  / "app" / "services" / "breadth_job.py")


class _Proc:
    returncode = 0
    stdout = "breadth done"
    stderr = ""


def _capture_run(monkeypatch, store):
    def fake_run(cmd, **kwargs):
        store["cmd"] = cmd
        store.update(kwargs)
        return _Proc()

    monkeypatch.setattr(_subprocess_mod, "run", fake_run)
    monkeypatch.setattr(scheduler, "_clear_breadth_cache", lambda: None)


def test_the_spawned_child_is_told_the_parent_already_recorded(monkeypatch):
    seen = {}
    _capture_run(monkeypatch, seen)
    scheduler._breadth_daily_body()
    assert seen["env"][job_tracking.PARENT_RECORDED_ENV] == "breadth_daily"


def test_the_marker_is_added_to_the_inherited_environment(monkeypatch):
    # env= REPLACES the child's environment, so dropping anything the scrapers
    # need (DATABASE_URL is read from .env, but PATH/BREADTH_ENGINE_PY are not)
    # would break the nightly run while looking perfectly healthy here.
    seen = {}
    _capture_run(monkeypatch, seen)
    monkeypatch.setenv("BREADTH_ENGINE_PY", "D:/somewhere/python.exe")
    scheduler._breadth_daily_body()
    assert seen["env"]["BREADTH_ENGINE_PY"] == "D:/somewhere/python.exe"
    assert seen["env"][job_tracking.PARENT_RECORDED_ENV] == "breadth_daily"


def test_the_child_side_literal_matches_the_shared_constant():
    src = BREADTH_SCRIPT.read_text(encoding="utf-8")
    assert f'PARENT_RECORDED_ENV = "{job_tracking.PARENT_RECORDED_ENV}"' in src, (
        "breadth_job.py hard-codes the marker name (it deliberately imports no "
        "app.* at module level) — the two definitions must stay identical.")


def test_the_terminal_door_records_and_honours_the_marker():
    src = BREADTH_SCRIPT.read_text(encoding="utf-8")
    # __main__ must go through the wrapper, not straight to run().
    assert "out = _run_logged()" in src
    assert 'track_job("breadth_daily", "manual")' in src
    # The marker short-circuits before any row is opened.
    assert "if _parent_owns_the_ledger_row():" in src
    # "manual" must stay a legal trigger, else it is silently rewritten.
    assert "manual" in job_tracking.VALID_TRIGGER


def test_a_partial_run_is_not_recorded_as_a_success():
    # --skip-load computes without writing the breadth tables; a success row for
    # it would tell the admin card the data landed.
    src = BREADTH_SCRIPT.read_text(encoding="utf-8")
    assert "if skip_load:" in src and "not recorded in job_runs" in src
