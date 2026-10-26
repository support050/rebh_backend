"""Regression guard: the shape of backend/app/system_registry.py.

GET /api/admin/system-map/systems/<id> crashed with
`TypeError: string indices must be integers` because the `home` system declared
its tables as bare strings while every consumer (the Runbook freshness probe,
pipelines(), registry_stats() and checks_sql()) indexes them as dicts.
checks_sql() loops ALL systems, so one malformed entry took down every detail
page, not just its own.

These tests are pure-python: no DB, no HTTP, no Redis.
"""
from app import system_registry as registry

_T_KEYS = {"table", "date_column", "max_lag_trading_days", "source_job",
           "note", "verified", "pages"}


def test_every_system_has_the_expected_sections():
    for system in registry.SYSTEMS:
        for key in ("id", "name", "blurb", "built", "pages", "jobs", "tables", "issues"):
            assert key in system, f"system {system.get('id')} is missing '{key}'"


def test_no_table_entry_is_a_bare_string():
    offenders = [
        (s["id"], e)
        for s in registry.SYSTEMS
        for e in s["tables"]
        if not isinstance(e, dict)
    ]
    assert offenders == [], f"tables must be _t() dicts, got {offenders}"


def test_table_entries_carry_the_full_t_shape():
    for system in registry.SYSTEMS:
        for entry in system["tables"]:
            missing = _T_KEYS - set(entry)
            assert not missing, f"{system['id']}.{entry['table']} lacks {missing}"
            assert entry["table"].isidentifier(), \
                f"{system['id']} declares a non-identifier table name {entry['table']!r}"


def test_normaliser_is_idempotent_on_a_clean_registry():
    before = {s["id"]: [dict(e) for e in s["tables"]] for s in registry.SYSTEMS}
    registry._normalise_tables()
    for system in registry.SYSTEMS:
        assert [dict(e) for e in system["tables"]] == before[system["id"]], \
            f"{system['id']} tables changed on a second normalisation pass"


def test_checks_sql_survives_the_whole_registry():
    rows = registry.checks_sql()
    assert len(rows) > 4, "only the four static job_runs queries came back"
    for row in rows:
        assert set(row) == {"label", "sql"}
        assert row["sql"].strip().endswith(";")
        assert "None" not in row["sql"], f"an unset date column leaked into {row}"


def test_home_tables_are_probed_for_freshness():
    home = registry.get_system("home")
    assert home is not None
    probed = {t["table"] for t in home["tables"] if t["date_column"]}
    assert {"prices", "market_pulse", "industry_group_history"} <= probed, \
        "the home pipeline is invisible to the lag check again"


def test_pipelines_and_stats_build():
    assert registry.registry_stats()["systems"] == len(registry.SYSTEMS)
    rows = registry.pipelines()
    assert all(isinstance(r["table"], str) for r in rows)


# ── Schedule-runbook axes (cadence / role / storage / commands) ──────────────
_CMD_KEYS = {"label", "cli", "kind"}


def test_every_job_carrys_valid_schedule_axes():
    for system in registry.SYSTEMS:
        for job in system["jobs"]:
            assert job["cadence"] in registry.CADENCES, \
                f"{job['job_name']} has bad cadence {job['cadence']!r}"
            assert job["role"] in registry.ROLES, \
                f"{job['job_name']} has bad role {job['role']!r}"
            assert job["storage"] in registry.STORAGES, \
                f"{job['job_name']} has bad storage {job['storage']!r}"
            assert isinstance(job["commands"], list)
            for cmd in job["commands"]:
                assert set(cmd) == _CMD_KEYS, f"{job['job_name']} command shape {cmd}"
                assert cmd["kind"] in registry.COMMAND_KINDS, \
                    f"{job['job_name']} command kind {cmd['kind']!r}"
                assert cmd["cli"].strip(), f"{job['job_name']} has an empty cli"


def test_schedule_jobs_flattens_and_dedupes():
    jobs = registry.schedule_jobs()
    names = [j["job_name"] for j in jobs]
    assert len(names) == len(set(names)), "schedule_jobs() must yield one card per job"
    expected = {"job_name", "file", "cadence", "role", "storage", "commands",
                "writes", "expected_schedule", "trigger", "tracked", "note", "systems"}
    for j in jobs:
        assert expected <= set(j), f"{j['job_name']} lacks {expected - set(j)}"
        assert j["cadence"] in registry.CADENCES
        assert j["role"] in registry.ROLES
        assert j["storage"] in registry.STORAGES
        assert j["systems"], f"{j['job_name']} is not attached to any system"


def test_integrity_targets_point_at_real_jobs():
    targets = registry.integrity_targets()
    tables = {t["table"] for t in targets}
    assert len(targets) == 6, "the gap scanner must cover the six vital tables"
    # sukuk is the safety net now that it is off the daily pipeline (2026-10-26)
    assert "sukuk_market_data" in tables, "sukuk must be watched by the gap scanner"
    known = {j["job_name"] for j in registry.schedule_jobs()}
    for t in targets:
        assert t["table"].isidentifier() and t["date_column"].isidentifier()
        assert t["cadence"] in registry.CADENCES
        assert t["responsible_job"] in known, \
            f"{t['table']} names an unknown fix job {t['responsible_job']!r}"
        for cmd in t["fix_commands"]:
            assert set(cmd) == _CMD_KEYS and cmd["kind"] in registry.COMMAND_KINDS
