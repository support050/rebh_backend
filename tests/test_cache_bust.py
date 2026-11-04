"""Cache eviction contract (app/core/cache_bust.py).

Why this file exists
-------------------
Until 2026-11-04 the invalidation logic was copy-pasted four times —
daily_market_update step 10, run_analytics_pipeline.flush_redis_cache,
scheduler._clear_* and the admin /api/cache routes — and every copy reported a
failure as ``logger.info("Redis inactive locally — safe in dev/script mode")``.
A script that wrote to the PRODUCTION Postgres therefore silently skipped the
bust the PRODUCTION API needed, and the only move that worked was opening a
Render shell and running ``flush_all()`` by hand.

These tests pin the four things that must never regress:

1. bust by NAMESPACE, never ``"*"`` — and auth/session keys are untouchable;
2. a bust that reached nothing is reported as a failure, never raised and never
   hidden as an INFO;
3. the HTTP door (a script refreshing the deployed cache) is only knocked when
   this host's own Redis is unreachable — a job on the server must not call
   itself over HTTP;
4. the job→namespace table covers every runnable job, so no run falls through to
   "bust everything".
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from app.core import cache_bust
from app.core.cache_config import (
    APP_CACHE_PREFIXES,
    AUTH_KEY_PREFIXES,
    DAILY_MARKET_PREFIXES,
    PATTERN_BREADTH_SNAPSHOT,
)


# ─── helpers ───────────────────────────────────────────────────────────────
class FakeRedis:
    """Records every scan/unlink call, like a redis-py sync client."""

    def __init__(self, keys_by_pattern=None, *, reachable=True, keys=()):
        self.keys_by_pattern = keys_by_pattern or {}
        self.reachable = reachable
        self.scans = []
        self.unlinked = []
        self._all = list(keys)

    def ping(self):
        if not self.reachable:
            raise ConnectionError("Error 10061 connecting to localhost:7424")
        return True

    def scan(self, cursor=0, match=None, count=None):
        self.scans.append({"cursor": cursor, "match": match, "count": count})
        return 0, self.keys_by_pattern.get(match, [k for k in self._all
                                                   if self_glob(k, match)])

    def unlink(self, *keys):
        self.unlinked.extend(keys)
        return len(keys)

    def info(self, *args, **kwargs):
        return {"redis_version": "7.0.0", "keyspace_hits": 30, "keyspace_misses": 10,
                "expired_keys": 1, "evicted_keys": 0,
                "used_memory_human": "1.0M", "maxmemory_policy": "noeviction"}

    def close(self):
        pass


def self_glob(key, pattern):
    """Minimal glob for the fake: only a trailing '*' is used by the code."""
    if pattern == "*":
        return True
    if pattern.endswith(":*"):
        return key.startswith(pattern[:-2])
    return key == pattern


# ─── 1. pattern resolution ─────────────────────────────────────────────────
class TestPatternsFor:
    def test_breadth_job_is_narrow(self):
        """A breadth rerun must not rebuild every screener page."""
        patterns, _ = cache_bust.patterns_for(job="breadth_daily")
        assert patterns == [PATTERN_BREADTH_SNAPSHOT] == ["rebh:breadth:*"]

    def test_daily_market_update_busts_stock_and_terminal_namespaces(self):
        patterns, _ = cache_bust.patterns_for(job="daily_market_update")
        assert "rs:*" in patterns and "prices:*" in patterns
        assert "terminal:*" in patterns and "rs_hub:*" in patterns
        # not the whole instance: economic/scraper are untouched by this job
        assert "economic:*" not in patterns and "scraper:*" not in patterns

    def test_group_names_resolve_to_globs(self):
        patterns, _ = cache_bust.patterns_for(groups=["market"])
        assert patterns == [f"{p}:*" for p in DAILY_MARKET_PREFIXES[:6]]

    def test_raw_namespace_prefix_gets_a_glob(self):
        patterns, _ = cache_bust.patterns_for(namespaces=["prices", "rebh:breadth:*"])
        assert patterns == ["prices:*", "rebh:breadth:*"]

    def test_unknown_group_raises_not_falls_back(self):
        with pytest.raises(KeyError):
            cache_bust.patterns_for(groups=["evreything"])

    def test_unmapped_job_falls_back_to_all_and_says_so(self):
        patterns, notes = cache_bust.patterns_for(job="brand_new_job")
        assert patterns == [f"{p}:*" for p in APP_CACHE_PREFIXES]
        assert notes and "not in JOB_GROUPS" in notes[0]

    def test_family_rules_cover_new_fred_and_scraper_jobs(self):
        """A new fred:/scraper: key must not land on the heavyweight fallback."""
        for job in ("fred:GNP", "scraper:whatever", "manual:something"):
            patterns, notes = cache_bust.patterns_for(job=job)
            assert patterns == ["economic:*"], job
            assert any("matched the" in n for n in notes), job

    def test_nothing_specified_busts_app_namespaces_never_wildcard(self):
        patterns, notes = cache_bust.patterns_for()
        assert "*" not in patterns
        assert patterns == [f"{p}:*" for p in APP_CACHE_PREFIXES]
        assert notes and "nothing specified" in notes[0]


# ─── 2. eviction walk ──────────────────────────────────────────────────────
class TestEvict:
    def test_scan_with_count_and_unlink(self):
        fake = FakeRedis(keys_by_pattern={"rs:*": ["rs:latest:1", "rs:history:2"]})
        with patch.object(cache_bust, "_client", return_value=fake):
            report = cache_bust.evict(["rs:*"])
        assert report["reachable"] is True
        assert report["deleted"] == 2
        assert report["per_pattern"] == {"rs:*": 2}
        assert fake.unlinked == ["rs:latest:1", "rs:history:2"]
        assert [s["count"] for s in fake.scans] == [cache_bust.SCAN_COUNT]
        assert cache_bust.SCAN_COUNT == 500          # SCAN, never KEYS

    def test_auth_keys_are_never_deleted_even_under_a_wildcard(self):
        """The rule that stops a cache clear from logging everybody out."""
        keys = ["rs:latest:1", "refresh_gen:42", "loginfail:1001:abc", "oauth_state:9"]
        fake = FakeRedis(keys_by_pattern={"*": keys})
        with patch.object(cache_bust, "_client", return_value=fake):
            report = cache_bust.evict(["*"])
        assert fake.unlinked == ["rs:latest:1"]
        assert report["skipped_auth_keys"] == 3

    def test_unreachable_redis_is_a_report_not_an_exception(self):
        fake = FakeRedis(reachable=False)
        with patch.object(cache_bust, "_client", return_value=fake):
            report = cache_bust.evict(["rs:*"], url="redis://localhost:7424")
        assert report["reachable"] is False
        assert report["error"].startswith("ConnectionError")
        assert report["deleted"] == 0
        assert report["target"] == "redis://localhost:7424"

    def test_report_always_carries_target_and_elapsed(self):
        with patch.object(cache_bust, "_client", side_effect=ImportError("no redis")):
            report = cache_bust.evict(["rs:*"])
        assert report["reachable"] is False
        assert "ImportError" in report["error"]
        assert "elapsed_ms" in report and "channel" in report

    def test_empty_pattern_list_reports_instead_of_hitting_the_network(self):
        with patch.object(cache_bust, "_client", side_effect=AssertionError("used")):
            report = cache_bust.evict([])
        assert report["reachable"] is False and "no patterns" in report["error"]

    def test_password_never_leaks_into_the_report(self):
        with patch.object(cache_bust, "_client", return_value=FakeRedis(reachable=False)):
            report = cache_bust.evict(["rs:*"],
                                       url="redis://user:SEKRIT@cache.example:6379/0")
        assert "SEKRIT" not in json.dumps(report)
        assert report["target"].endswith("cache.example:6379/0")


class TestAuthKeyPrefixesAreOneList:
    def test_redis_and_cache_bust_share_the_same_list(self):
        from app.core.redis import RedisCache
        assert RedisCache._AUTH_KEY_PREFIXES is AUTH_KEY_PREFIXES

    def test_every_guarded_prefix_is_recognised(self):
        for prefix in AUTH_KEY_PREFIXES:
            assert cache_bust.is_auth_key(f"{prefix}whatever"), prefix
        assert not cache_bust.is_auth_key("rebh:breadth:2026-11-04")


# ─── 3. the network gate ───────────────────────────────────────────────────
class TestValidatePatterns:
    def test_wildcard_is_refused(self):
        accepted, rejected = cache_bust.validate_patterns(["*"])
        assert accepted == [] and rejected == ["*"]

    def test_unknown_root_is_refused(self):
        accepted, rejected = cache_bust.validate_patterns(["someotherapp:*"])
        assert accepted == [] and rejected == ["someotherapp:*"]

    def test_app_namespaces_are_accepted_and_normalised(self):
        accepted, rejected = cache_bust.validate_patterns(["rebh:breadth:*", "prices"])
        assert accepted == ["rebh:breadth:*", "prices:*"] and rejected == []

    def test_auth_root_cannot_be_smuggled_through(self):
        accepted, rejected = cache_bust.validate_patterns(["refresh_token:*"])
        assert accepted == [] and rejected == ["refresh_token:*"]

    def test_namespaces_whose_prefix_contains_a_colon_are_allowed(self):
        """``industry:groups`` and ``rebh:breadth`` are real namespaces.

        A gate keyed on the first word before the colon rejects them, which
        means a market-group bust over the door silently evicts less than the
        caller asked for — the same class of bug as the placebo routes.
        """
        accepted, rejected = cache_bust.validate_patterns(
            ["industry:groups:*", "rebh:breadth:*"])
        assert rejected == []
        assert accepted == ["industry:groups:*", "rebh:breadth:*"]

    def test_prefix_of_an_app_namespace_is_not_a_free_pass(self):
        """``pricess`` must not slip through because it starts like ``prices``."""
        accepted, rejected = cache_bust.validate_patterns(["pricess:*", "prices:latest:*"])
        assert accepted == ["prices:latest:*"] and rejected == ["pricess:*"]


# ─── 4. the HTTP door ──────────────────────────────────────────────────────
class TestNotifyApi:
    def test_unconfigured_door_reports_instead_of_raising(self, monkeypatch):
        for key in ("CACHE_BUST_API_URL", "API_BASE_URL", "BACKEND_URL"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(cache_bust, "_DOTENV_CACHE", {})
        report = cache_bust.notify_api(job="breadth_daily")
        assert report["reachable"] is False
        assert "CACHE_BUST_API_URL" in report["error"]

    def test_missing_internal_key_is_named(self, monkeypatch):
        monkeypatch.setenv("CACHE_BUST_API_URL", "https://api.example")
        monkeypatch.delenv("INTERNAL_API_KEY", raising=False)
        monkeypatch.setattr(cache_bust, "_DOTENV_CACHE", {})
        report = cache_bust.notify_api(job="breadth_daily")
        assert report["reachable"] is False
        assert "INTERNAL_API_KEY" in report["error"]

    def test_explicit_base_beats_the_configured_one(self, monkeypatch):
        """A caller-supplied URL wins over .env — otherwise no test, and no
        staging run, can avoid reaching production."""
        monkeypatch.setenv("CACHE_BUST_API_URL", "https://prod.example")
        assert cache_bust.api_base_url("https://staging.example/") == "https://staging.example"
        assert cache_bust.api_base_url() == "https://prod.example"

    def test_posts_to_the_internal_route_with_the_key_header(self, monkeypatch):
        monkeypatch.setenv("CACHE_BUST_API_URL", "https://api.example/")
        monkeypatch.setenv("INTERNAL_API_KEY", "k-123")
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"ok": True, "deleted": 7,
                                   "per_pattern": {"rebh:breadth:*": 7}}).encode()

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            return Response()

        monkeypatch.setattr(cache_bust.urllib.request, "urlopen", fake_urlopen)
        report = cache_bust.notify_api(job="breadth_daily")

        assert report["reachable"] is True and report["deleted"] == 7
        assert captured["method"] == "POST"
        assert captured["url"].startswith("https://api.example/api/internal/cache/bust?")
        assert "patterns=rebh:breadth:%2A" in captured["url"]
        assert captured["headers"].get("X-internal-key") == "k-123"
        assert report["target"] == "https://api.example"

    def test_http_error_becomes_the_report(self, monkeypatch):
        monkeypatch.setenv("CACHE_BUST_API_URL", "https://api.example")
        monkeypatch.setenv("INTERNAL_API_KEY", "wrong-key")
        import urllib.error

        def boom(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden",
                                         {}, None)

        monkeypatch.setattr(cache_bust.urllib.request, "urlopen", boom)
        report = cache_bust.notify_api(job="breadth_daily")
        assert report["reachable"] is False
        assert "HTTP 403" in report["error"]


class TestBustChannelSelection:
    """The rule that ends the Render-shell ritual — and does not double the work."""

    def _unreachable(self, patterns, **kw):
        return {"channel": "local", "target": "redis://localhost:7424",
                "reachable": False, "deleted": 0, "error": "ConnectionError",
                "per_pattern": {}, "skipped_auth_keys": 0}

    def _reachable(self, patterns, **kw):
        return {"channel": "local", "target": "redis://prod:6379",
                "reachable": True, "deleted": 3, "error": None,
                "per_pattern": {"rebh:breadth:*": 3}, "skipped_auth_keys": 0}

    def test_local_success_means_no_http_call(self):
        with patch.object(cache_bust, "evict", self._reachable), \
             patch.object(cache_bust, "notify_api",
                          side_effect=AssertionError("must not be used")):
            result = cache_bust.bust(job="breadth_daily")
        assert result["ok"] is True
        assert [c["channel"] for c in result["channels"]] == ["local"]

    def test_local_failure_knocks_the_door(self):
        door = {"channel": "remote", "target": "https://api.example",
                "reachable": True, "deleted": 12, "error": None}
        with patch.object(cache_bust, "evict", self._unreachable), \
             patch.object(cache_bust, "notify_api", return_value=door):
            result = cache_bust.bust(job="breadth_daily")
        assert result["ok"] is True
        assert result["deleted"] == 12
        assert [c["channel"] for c in result["channels"]] == ["local", "remote"]

    def test_nothing_reachable_is_ok_false_not_an_exception(self, caplog):
        dead = {"channel": "remote", "target": None, "reachable": False,
                "deleted": None, "error": "not configured"}
        with patch.object(cache_bust, "evict", self._unreachable), \
             patch.object(cache_bust, "notify_api", return_value=dead), \
             caplog.at_level(logging.WARNING, logger="app.core.cache_bust"):
            result = cache_bust.bust(job="breadth_daily")
        assert result["ok"] is False
        # The wording is the contract: a failed bust must name itself, never hide
        # as an informational "skipped, that is safe" line.
        assert any("CACHE NOT BUSTED" in r.getMessage() for r in caplog.records)

    def test_explicit_flags_override_the_heuristic(self):
        with patch.object(cache_bust, "evict", self._unreachable), \
             patch.object(cache_bust, "notify_api",
                          side_effect=AssertionError("must not be used")):
            result = cache_bust.bust(groups=["breadth"], remote=False)
        assert [c["channel"] for c in result["channels"]] == ["local"]


# ─── 5. CLI ────────────────────────────────────────────────────────────────
class TestCli:
    def test_exit_code_zero_when_busted(self):
        with patch.object(cache_bust, "bust",
                          return_value={"ok": True, "patterns": ["rs:*"], "notes": [],
                                        "channels": [{"channel": "local",
                                                      "reachable": True,
                                                      "target": "r", "deleted": 2}],
                                        "deleted": 2, "summary": "local=2"}):
            assert cache_bust.main(["--job", "daily_market_update"]) == 0

    def test_exit_code_one_when_nothing_was_reached(self):
        with patch.object(cache_bust, "bust",
                          return_value={"ok": False, "patterns": ["rs:*"], "notes": [],
                                        "channels": [{"channel": "local",
                                                      "reachable": False,
                                                      "target": "r", "deleted": 0,
                                                      "error": "down"}],
                                        "deleted": 0, "summary": "local=UNREACHABLE"}):
            assert cache_bust.main(["--groups", "market", "--local-only"]) == 1


# ─── 6. drift guards ───────────────────────────────────────────────────────
class TestTableDrift:
    def test_every_group_reference_is_a_real_group(self):
        for job, groups in cache_bust.JOB_GROUPS.items():
            for group in groups:
                assert group in cache_bust.GROUPS, f"{job} -> {group}"

    def test_every_rule_group_is_a_real_group(self):
        for _, groups in cache_bust.JOB_GROUP_RULES:
            for group in groups:
                assert group in cache_bust.GROUPS

    def test_no_group_contains_a_bare_wildcard(self):
        for name, prefixes in cache_bust.GROUPS.items():
            for prefix in prefixes:
                assert prefix != "*", name
                assert prefix in APP_CACHE_PREFIXES or ":" in prefix, (name, prefix)

    def test_every_runnable_job_resolves_without_the_fallback(self):
        """A Run Now whose name the table does not know would bust everything.

        JOB_CALLABLES is the list the Admin dashboard and the cron groups
        dispatch from (job_runner._ensure_targets copies it), so covering it —
        plus the scraper_service keys — means the 'all' fallback is unreachable
        in practice and stays a safety net only.
        """
        from app.core.scheduler import JOB_CALLABLES

        names = list(JOB_CALLABLES) + [
            "scraper:sp500_price", "scraper:sp500_pe", "scraper:sp500_ey",
            "scraper:treasury_gov", "scraper:tasi_components",
            "daily_market_update", "analytics_pipeline", "export_rs_hub_data",
        ]
        for name in names:
            _patterns, notes = cache_bust.patterns_for(job=name)
            assert not any("not in JOB_GROUPS" in n for n in notes), name

    def test_internal_door_is_mounted_and_csrf_exempt(self, monkeypatch):
        """The whole point of the door: a machine can knock without a cookie.

        Checked through the real app so mounting ("/api"), the CSRF exemption and
        the limiter wiring are all covered by one test — each of the three used
        to be wrong independently.
        """
        from fastapi.testclient import TestClient

        from app.main import app

        monkeypatch.setattr("app.core.security.settings.INTERNAL_API_KEY", "k-123")
        client = TestClient(app)

        # No CSRF cookie/header: the exemption must let the request reach the key
        # check (401), instead of the middleware answering 403 about CSRF.
        res = client.post("/api/internal/cache/bust?patterns=rebh:breadth:*")
        assert res.status_code == 401
        assert "csrf" not in res.text.lower()

        # Right shape, wrong key -> 403, and the key is never echoed back.
        res = client.post("/api/internal/cache/bust?patterns=rebh:breadth:*",
                          headers={"X-Internal-Key": "nope"})
        assert res.status_code == 403
        assert "k-123" not in res.text

    def test_internal_door_refuses_a_wildcard_before_touching_the_cache(self, monkeypatch):
        from fastapi.testclient import TestClient

        from app.main import app

        monkeypatch.setattr("app.core.security.settings.INTERNAL_API_KEY", "k-123")
        client = TestClient(app)
        res = client.post("/api/internal/cache/bust?patterns=*",
                          headers={"X-Internal-Key": "k-123"})
        assert res.status_code == 400
        detail = res.json()["detail"]
        assert "*" in detail["rejected"]
        assert set(APP_CACHE_PREFIXES) == set(detail["allowed"])

    def test_human_door_needs_an_admin_session(self):
        """The admin routes stay cookie-protected (only the machine door is not).

        These routes used to be a placebo: they evicted namespaces nothing
        writes, so every click answered "cleared (0 keys)" while the real
        payloads stayed cached.
        """
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        res = client.post("/api/cache/clear/stocks")
        assert res.status_code in (401, 403)

    def test_admin_route_namespaces_are_real_namespaces(self):
        """The placebo regression, asserted structurally.

        The old /api/cache routes evicted tadawul_stocks:*, tadawul:* and
        financials:* — namespaces nothing writes. Anything the routes can now
        touch has to be a namespace the application actually caches.
        """
        from app.api.routes import cache as cache_routes

        for group in cache_routes.cache_bust.GROUPS.values():
            for prefix in group:
                assert any(prefix == p or prefix.startswith(p + ":")
                           for p in APP_CACHE_PREFIXES), prefix

    def test_scripts_no_longer_hide_a_failed_bust_as_info(self):
        """The exact sentence that made the Render shell necessary."""
        backend = cache_bust.Path(cache_bust.__file__).resolve().parents[2]
        for rel in ("scripts/daily_market_update.py", "scripts/run_analytics_pipeline.py"):
            text = (backend / rel).read_text(encoding="utf-8")
            assert "safe in dev/script mode" not in text, rel
            assert "skipping cache flush." not in text, rel
            assert "cache_bust.bust(" in text, rel

    def test_breadth_job_busts_its_own_keyspace(self):
        """breadth_job.py cannot be imported in this venv (the vendored engine
        needs pyyaml), so the child side is checked against its source text —
        same convention as tests/test_job_run_recording.py."""
        backend = cache_bust.Path(cache_bust.__file__).resolve().parents[2]
        text = (backend / "app" / "services" / "breadth_job.py").read_text(
            encoding="utf-8")
        assert "CACHE_BUST_JOB_KEY = \"breadth_daily\"" in text
        assert "from app.core import cache_bust" in text
        assert "status[\"cache_bust\"] = _bust_cache()" in text
        # the job must be unbreakable by its own bookkeeping: guarded import
        assert "job beats cache" in text


class TestHumanDoorReportsItsOwnFailures:
    """A cache endpoint that answers 200 "cleared 0 keys" while the cache is dead
    is worse than a 503: it tells the operator the thing they wanted happened."""

    async def test_unreachable_cache_raises_503_not_a_quiet_success(self):
        from app.api.routes import cache as cache_routes

        dead = {"channel": "local", "reachable": False, "deleted": 0,
                "target": "redis://localhost:7424", "error": "ConnectionError",
                "per_pattern": {}, "skipped_auth_keys": 0}
        with patch.object(cache_bust, "evict", return_value=dead):
            with pytest.raises(Exception) as exc_info:
                await cache_routes.clear_stocks_cache(current_admin=MagicMock())
        assert exc_info.value.status_code == 503
        assert "nothing was evicted" in str(exc_info.value.detail["message"]).lower()
        assert exc_info.value.detail["target"] == "redis://localhost:7424"


class TestInfo:
    def test_hit_ratio_is_reported(self):
        with patch.object(cache_bust, "_client", return_value=FakeRedis()):
            out = cache_bust.info()
        assert out["reachable"] is True
        assert out["redis_version"] == "7.0.0"
        assert out["hit_ratio"] == 0.75
        assert out["maxmemory_policy"] == "noeviction"

    def test_unreachable_cache_is_named_not_silent(self):
        with patch.object(cache_bust, "_client", return_value=FakeRedis(reachable=False)):
            out = cache_bust.info()
        assert out["reachable"] is False
        assert out["hit_ratio"] is None
        assert "ConnectionError" in out["error"]
