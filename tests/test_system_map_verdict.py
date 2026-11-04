"""Regression guard: the job verdict rule in app/api/routes/system_map.py.

Two production bugs lived here, both the same class — the card trusting the
bookkeeping over the data:

  1. A run started from a terminal (the command printed on the card itself)
     opens no ``job_runs`` row, so a months-old "failed" line stayed on a job
     whose tables had been rebuilt a hundred times.
  2. ``Run Now`` runs in a daemon thread, and a server reload kills it before it
     closes its row. The orphan 'running' row then read "Running for 2.1h" on a
     weekly job for six days, because the only escape hatch was the cadence
     budget.

The rule is a pure function over (ledger row, table freshness), so it is testable
without a DB, HTTP or Redis. The tests below pin BOTH directions: data must be
able to overturn the ledger, and must never be able to hide a real failure.
"""
from datetime import date, datetime, timedelta, timezone

from app import system_registry as registry
from app.api.routes import system_map as sm
from app.services import job_runner

NOW = datetime.now(timezone.utc)
TODAY = NOW.date().isoformat()
STALE_H = job_runner.STALE_RUNNING_MINUTES / 60.0


def _job(**overrides):
    """A real registry job, so the test cannot pass against a shape the
    production registry stopped using."""
    base = next(j for s in registry.list_systems() for j in s["jobs"]
                if j["job_name"] == "sukuk_importer")
    return {**base, **overrides}


def _run(hours_ago, status="failed", rows=62, error=None):
    return {"status": status, "started_at": NOW - timedelta(hours=hours_ago),
            "finished_at": None, "rows_written": rows, "error_message": error,
            "triggered_by": "admin"}


def _table(latest=TODAY, status="ok"):
    return {"table": "sukuk_market_data", "date_column": "updated_at",
            "latest": latest, "status": status}


def _verdict(last, owned, **job_overrides):
    return sm._build_job(_job(**job_overrides), last, 0, 0, False, NOW, owned)


# ── Bug 1: a stale ledger line, overturned by the data ───────────────────────
def test_failed_run_is_overturned_when_every_table_is_current():
    out = _verdict(_run(74), [_table()])
    assert out["status"] == "ok" and out["superseded"] is True
    assert "Superseded" in out["message"] and TODAY in out["message"]


def test_never_recorded_run_is_overturned_when_every_table_is_current():
    out = _verdict(None, [_table()])
    assert out["status"] == "ok" and out["superseded"] is True
    assert "outside the ledger" in out["message"]


def test_zero_rows_success_is_overturned_because_nothing_was_missing():
    out = _verdict(_run(2, status="success", rows=0), [_table()])
    assert out["status"] == "ok" and out["superseded"] is True


def test_one_stale_table_blocks_the_overturn():
    # Strict on purpose: the job writes several tables, so one rebuilt series
    # cannot speak for the table that really is missing days.
    out = _verdict(_run(74), [_table(), _table(latest="2020-01-01", status="late")])
    assert out["status"] == "bad" and out["superseded"] is False


def test_data_older_than_the_failed_run_never_exonerates_it():
    # The manual:gurufocus case: its series is 10 days old and the run that
    # failed is 10 MINUTES old. Hiding that would be the opposite bug.
    out = _verdict(_run(0.1), [_table(latest=(NOW - timedelta(days=10)).date().isoformat())])
    assert out["status"] == "bad" and out["superseded"] is False


def test_a_table_with_no_measured_date_is_not_evidence():
    out = _verdict(_run(74), [_table(latest=None, status="unknown")])
    assert out["status"] == "bad" and out["superseded"] is False


# ── Bug 2: the abandoned 'running' row ───────────────────────────────────────
def test_running_past_the_runner_limit_is_a_dead_process():
    out = _verdict(_run(STALE_H + 0.1, status="running", rows=None), [])
    assert out["status"] == "bad"
    assert "abandoned" in out["message"]


def test_dead_process_row_is_still_overturned_by_fresh_tables():
    out = _verdict(_run(STALE_H + 0.1, status="running", rows=None), [_table()])
    assert out["status"] == "ok" and out["superseded"] is True


def test_a_run_inside_the_limit_is_still_reported_as_running():
    # The other direction: a job that really is mid-run must not be declared
    # dead just because its cadence budget is long.
    out = _verdict(_run(0.5, status="running", rows=None), [_table()])
    assert out["status"] == "late" and out["message"].startswith("Running for")


# The 2026-11-03 case: a reload killed breadth_daily 23 min into its run, and
# the card read "running" for the full STALE_RUNNING_MINUTES afterwards. The row
# predating this process PROVES no live thread owns it, so the wait is unnecessary.
def _verdict_corpse(last, owned, **job_overrides):
    return sm._build_job(_job(**job_overrides), last, 0, 0, False, NOW, owned,
                         corpse=True)


def test_a_corpse_row_is_dead_at_once_without_waiting_two_hours():
    out = _verdict_corpse(_run(0.5, status="running", rows=None), [])
    assert out["status"] == "bad"
    assert "server generation" in out["message"]
    assert "abandoned past" not in out["message"]   # not the age rule talking


def test_a_corpse_row_whose_tables_are_current_reads_ok():
    # The re-run happened from a terminal, which opens no row: the data is the
    # only witness, and it must win over the corpse.
    out = _verdict_corpse(_run(0.5, status="running", rows=None), [_table()])
    assert out["status"] == "ok" and out["superseded"] is True
    assert TODAY in out["message"]


def test_a_live_run_inside_the_limit_is_still_running_with_the_flag_off():
    # Guard the new default: forgetting corpse=False would turn every honest
    # mid-run card red.
    out = _verdict(_run(0.5, status="running", rows=None), [])
    assert out["status"] == "late" and "server generation" not in out["message"]


# ── job_runner.row_is_a_corpse: the liveness test itself ────────────────────
BOOT = NOW


def _before_boot():
    return BOOT - timedelta(minutes=20)


def test_an_admin_row_older_than_this_process_is_a_corpse(monkeypatch):
    monkeypatch.setattr(job_runner, "BOOT_AT", BOOT)
    monkeypatch.setattr(job_runner, "_ACTIVE", {})
    assert job_runner.row_is_a_corpse("breadth_daily", _before_boot(), "admin")


def test_a_row_in_this_process_active_table_is_never_a_corpse(monkeypatch):
    monkeypatch.setattr(job_runner, "BOOT_AT", BOOT)
    monkeypatch.setattr(job_runner, "_ACTIVE", {"breadth_daily": BOOT})
    assert not job_runner.row_is_a_corpse("breadth_daily", _before_boot(), "admin")


def test_a_row_younger_than_this_process_is_trusted(monkeypatch):
    # A sibling worker in a multi-worker deploy owns those: declaring them dead
    # would unlock a button while a real run is in flight.
    monkeypatch.setattr(job_runner, "BOOT_AT", BOOT)
    monkeypatch.setattr(job_runner, "_ACTIVE", {})
    assert not job_runner.row_is_a_corpse(
        "breadth_daily", BOOT + timedelta(minutes=1), "admin")


def test_cron_and_terminal_rows_are_not_judged_by_boot_time(monkeypatch):
    # They belong to OTHER processes, which this one cannot inspect: guessing
    # about them is what STALE_RUNNING_MINUTES stays for.
    monkeypatch.setattr(job_runner, "BOOT_AT", BOOT)
    monkeypatch.setattr(job_runner, "_ACTIVE", {})
    for trigger in ("cron", "manual", None):
        assert not job_runner.row_is_a_corpse("breadth_daily", _before_boot(), trigger)
    assert not job_runner.row_is_a_corpse("breadth_daily", None, "admin")


def test_a_naive_timestamp_is_read_as_utc(monkeypatch):
    monkeypatch.setattr(job_runner, "BOOT_AT", BOOT)
    monkeypatch.setattr(job_runner, "_ACTIVE", {})
    assert job_runner.row_is_a_corpse("j", _before_boot().replace(tzinfo=None), "admin")


def test_is_in_flight_unlocks_a_corpse_but_locks_a_live_row(monkeypatch):
    class _FakeDB:
        def close(self):
            pass

    monkeypatch.setattr(job_runner, "SessionLocal", lambda: _FakeDB())
    monkeypatch.setattr(job_runner, "BOOT_AT", BOOT)
    monkeypatch.setattr(job_runner, "_ACTIVE", {})
    monkeypatch.setattr(job_runner, "_read",
                        lambda db, sql, **p: [(_before_boot(), "admin")])
    assert not job_runner.is_in_flight("breadth_daily")
    monkeypatch.setattr(job_runner, "_read",
                        lambda db, sql, **p: [(BOOT + timedelta(minutes=1), "admin")])
    assert job_runner.is_in_flight("breadth_daily")
    monkeypatch.setattr(job_runner, "_read", lambda db, sql, **p: [])
    assert not job_runner.is_in_flight("breadth_daily")


# ── Paths that must stay untouched by the override ───────────────────────────
def test_a_genuine_recent_success_is_plain_ok():
    out = _verdict(_run(0.2, status="success"), [_table()])
    assert out["status"] == "ok" and out["superseded"] is False


def test_unreadable_ledger_says_so_instead_of_never_recorded():
    out = sm._build_job(_job(), None, 0, 0, True, NOW, [_table()])
    assert out["status"] == "bad" and "alembic upgrade head" in out["message"]


def test_untracked_job_is_never_judged_on_the_ledger():
    out = _verdict(_run(500), [], tracked=False)
    assert out["status"] == "untracked"


# ── Shared-table jobs: judged by their own series, not by the table's owner ──
# economic_indicators is written by nine registered jobs but the registry can
# attribute its table row to one, so the other eight had no evidence at all and
# a stale ledger line on them could never be overturned.
def _series(**overrides):
    base = {"SP500_EY": date(2026, 10, 30), "SP500_PE": date(2026, 10, 30),
            "UNRATE": date(2026, 9, 1)}
    return {**base, **overrides}


def test_every_shared_table_job_declares_its_own_series():
    gaps = [
        j["job_name"]
        for s in registry.list_systems() for j in s["jobs"]
        if registry.SERIES_TABLE in j["writes"]
        and not registry.series_evidence(j["job_name"])
    ]
    assert gaps == [], f"jobs writing {registry.SERIES_TABLE} with no evidence: {gaps}"


def test_series_evidence_uses_the_lowest_of_its_own_codes():
    entry = sm._series_evidence(["SP500_EY", "UNRATE"], _series(),
                                date(2026, 11, 1), None)
    assert entry["latest"] == "2026-09-01", "UNRATE must not be carried by EY"
    assert entry["status"] == "unknown"      # the table sets no lag threshold


def test_series_evidence_refuses_a_code_that_was_never_written():
    assert sm._series_evidence(["SP500_EY", "GONE"], _series(),
                               date(2026, 11, 1), None) is None
    assert sm._series_evidence([], _series(), date(2026, 11, 1), None) is None
    assert sm._series_evidence(["SP500_EY"], {}, date(2026, 11, 1), None) is None


def test_series_evidence_honours_the_table_threshold():
    # Same data date, but a month later and with a 3-trading-day budget on the
    # table → the series is now genuinely stale and must not exonerate anything.
    stale = sm._series_evidence(["SP500_EY"], _series(), date(2026, 12, 1), 3)
    assert stale["status"] in ("late", "bad")
    assert _verdict(_run(24 * 11), [stale])["status"] == "bad"


def test_a_stale_ledger_line_on_a_shared_table_job_is_overturned():
    gurufocus = next(j for s in registry.list_systems() for j in s["jobs"]
                     if j["job_name"] == "manual:gurufocus")
    entry = sm._series_evidence(registry.series_evidence("manual:gurufocus"),
                                _series(), date(2026, 11, 1), None)
    out = sm._build_job(gurufocus, _run(24 * 11), 1, 0, False, NOW, [entry])
    assert out["status"] == "ok" and out["superseded"] is True
    assert "2026-10-30" in out["message"]


def test_series_data_older_than_the_failure_still_keeps_it_red():
    gurufocus = next(j for s in registry.list_systems() for j in s["jobs"]
                     if j["job_name"] == "manual:gurufocus")
    entry = sm._series_evidence(registry.series_evidence("manual:gurufocus"),
                                _series(), NOW.date(), None)
    out = sm._build_job(gurufocus, _run(0.5), 1, 0, False, NOW, [entry])
    assert out["status"] == "bad" and out["superseded"] is False


# ── The live "already up to date" case (job_runs row 193, 2026-11-01) ─────────
# A correct GuruFocus scrape on 2026-11-01 wrote 0 rows: the source's last
# published observation is 2026-10-30, and report_date is the SOURCE date, not a
# run timestamp. The old code demanded through >= run-date to overturn a 0-rows
# line, so a fresh, working run stayed late forever. Its OWN lag window is the
# only freshness test that makes sense here.
def _gurufocus_job():
    return next(j for s in registry.list_systems() for j in s["jobs"]
                if j["job_name"] == "manual:gurufocus")


def test_zero_rows_success_before_the_run_date_is_ok():
    entry = sm._series_evidence(registry.series_evidence("manual:gurufocus"),
                                _series(), date(2026, 11, 1), 7)
    assert entry["status"] == "ok"                # inside the table's own 7-day bar
    out = sm._build_job(_gurufocus_job(), _run(0.2, status="success", rows=0),
                        0, 0, False, NOW, [entry])
    assert out["status"] == "ok" and out["superseded"] is True
    assert "nothing was missing" in out["message"]


def test_zero_rows_success_stays_late_when_its_series_is_genuinely_stale():
    # The other direction: 0 rows must not become a free pass. A series a month
    # old against a 3-day bar is stale, and its status keeps the card amber.
    stale = sm._series_evidence(registry.series_evidence("manual:gurufocus"),
                                _series(), date(2026, 12, 1), 3)
    assert stale["status"] in ("late", "bad")
    out = sm._build_job(_gurufocus_job(), _run(0.2, status="success", rows=0),
                        0, 0, False, NOW, [stale])
    assert out["status"] == "late" and out["superseded"] is False


# ── Concurrency cap (2026-11-03 blank-admin-pages incident) ──────────────────
# Three heavy run-now jobs at once from one home machine saturated the
# home↔Render Postgres uplink and black-holed every API request until the jobs
# finished. job_runner.start() must refuse the 3rd reservation, and the refusal
# must stay race-safe (slot reserved in start(), not inside the thread).
class _NoDB:
    """Any unpatched SessionLocal() usage fails the test loudly."""

    def __call__(self):
        raise AssertionError("cap test must not touch the DB")


def _cap_env(monkeypatch, active, cap=2, in_flight=False):
    monkeypatch.setattr(job_runner, "SessionLocal", _NoDB())
    monkeypatch.setattr(job_runner, "_ACTIVE", dict(active))
    monkeypatch.setattr(job_runner, "_registered", True)
    monkeypatch.setattr(job_runner, "_TARGETS",
                        {"one_more": {"fn": lambda: None, "label": "x",
                                      "self_tracked": False}})
    monkeypatch.setattr(job_runner, "is_in_flight",
                        lambda name: in_flight, raising=False)
    monkeypatch.setattr(job_runner.settings, "MAX_CONCURRENT_JOBS", cap,
                        raising=False)


def test_third_job_is_refused_at_the_cap(monkeypatch):
    _cap_env(monkeypatch, {"breadth_daily": NOW, "engine_vintages": NOW})
    out = job_runner.start("one_more")
    assert out["status"] == "already_running"
    assert "Concurrency cap reached" in out["message"]
    assert "one_more" not in job_runner._ACTIVE     # refusal reserved nothing


def test_slot_frees_as_soon_as_a_run_finishes(monkeypatch):
    _cap_env(monkeypatch, {"breadth_daily": NOW})
    thread = _cap_thread(monkeypatch)
    out = job_runner.start("one_more")
    assert out["status"] == "started"
    assert thread.start.called
    assert job_runner._ACTIVE.get("one_more") is not None


def test_reserved_slot_name_is_listed_in_the_refusal(monkeypatch):
    # The admin needs to know WHICH running job to wait for.
    _cap_env(monkeypatch, {"aaa_job": NOW, "zzz_job": NOW})
    out = job_runner.start("one_more")
    assert "aaa_job, zzz_job" in out["message"]


def test_in_flight_takes_priority_over_the_cap_message(monkeypatch):
    _cap_env(monkeypatch, {"breadth_daily": NOW, "engine_vintages": NOW},
             in_flight=True)
    out = job_runner.start("one_more")
    assert out["status"] == "already_running"
    assert "already_running" not in out["message"] or "in flight" in out["message"]
    assert "Concurrency cap" not in out["message"]


def _cap_thread(monkeypatch):
    """Patch job_runner.threading; return the fake Thread CLASS mock.
    Thread(...) constructs a child mock, so assert via
    fake.Thread.return_value (the constructed instance)."""
    import unittest.mock as mock

    fake = mock.MagicMock()
    monkeypatch.setattr(job_runner, "threading", fake)
    return fake.Thread.return_value


def test_failed_thread_start_rolls_the_reservation_back(monkeypatch):
    import unittest.mock as mock

    _cap_env(monkeypatch, {"breadth_daily": NOW})
    boom = mock.MagicMock()
    boom.start.side_effect = RuntimeError("no more threads")
    monkeypatch.setattr(job_runner, "threading",
                        mock.MagicMock(Thread=lambda **_k: boom))
    out = job_runner.start("one_more")
    assert out["status"] == "not_runnable"
    assert "one_more" not in job_runner._ACTIVE     # reservation rolled back
