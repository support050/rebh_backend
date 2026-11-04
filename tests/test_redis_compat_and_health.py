"""Part 1 of the cache overhaul — Redis version compatibility, health, memory safety.

Covers:
  Phase 1  atomic primitives are Lua (no GETDEL 6.2, no EXPIRE NX 7.0 dependency)
  Phase 2  /health probes Redis without ever 500ing; startup warning names the
           target without credentials; per-namespace hit/miss/set/error counters
  Phase 7  blocking KEYS quarantined, every cache key written with a TTL,
           REDIS_AUTH_URL splits the auth keyspace, circuit breaker gates the
           DATA path only (auth keeps failing closed)

No server is contacted: the fakes below speak just enough of the async redis
client API to exercise the real code paths (fakeredis is not a project dep).
"""

from __future__ import annotations

import asyncio
import glob
import inspect
import json
import os
import warnings
from typing import Any, Dict, List, Optional

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core import redis as redis_module
from app.core.cache_config import AUTH_KEY_PREFIXES, DEFAULT_CACHE_TTL
from app.core.redis import (
    LUA_GETDEL,
    LUA_INCR_WITH_EXPIRE,
    RedisCache,
    RedisCircuitBreaker,
    RedisUnavailableError,
    mask_target,
    namespace_of,
)


# ═══════════════════════════════════════════════════════════════════════════
# Test doubles
# ═══════════════════════════════════════════════════════════════════════════

def run_lua(body: str, keys: List[str], argv: List[Any],
           store: Dict[str, Any], ttls: Dict[str, int],
           counters: Dict[str, int]) -> Any:
    """Interpret the two scripts this module ships — nothing more.

    An unknown body is a test failure on purpose: it means production code
    started relying on a script these tests do not model.
    """
    if "INCR" in body:
        key = keys[0]
        counters[key] = counters.get(key, 0) + 1
        value = counters[key]
        # Mirrors LUA_INCR_WITH_EXPIRE: TTL on creation, or if it has none.
        if value == 1 or ttls.get(key, -1) < 0:
            ttls[key] = int(argv[0])
        return value
    if "GET" in body and "DEL" in body:
        key = keys[0]
        value = store.get(key)
        if value is not None:
            store.pop(key)
            ttls.pop(key, None)
        return value
    raise AssertionError(f"unexpected Lua body in tests: {body[:60]!r}")


class FakeRedis:
    """Async redis client stand-in. Records everything, connects to nothing."""

    def __init__(self, store: Optional[Dict[str, Any]] = None,
                 ping_ok: bool = True, version: str = "5.0.14.1"):
        self.store: Dict[str, Any] = dict(store or {})
        self.ttls: Dict[str, int] = {}
        self.counters: Dict[str, int] = {}
        self.ping_ok = ping_ok
        self.version = version
        self.pings = 0
        self.set_calls: List[dict] = []
        self.get_calls: List[str] = []
        self.eval_calls: List[dict] = []
        self.expire_calls: List[dict] = []
        self.getdel_calls: List[str] = []
        self.scan_keys: List[str] = []
        self.unlinked: List[str] = []
        self.closed = False

    # ── connection ───────────────────────────────────────────────────────
    async def ping(self):
        self.pings += 1
        if not self.ping_ok:
            raise ConnectionError("fake: redis is down")
        return True

    async def info(self, section=None):
        return {"redis_version": self.version}

    async def aclose(self):
        self.closed = True

    # ── commands the module must NOT reach for any more ──────────────────
    async def getdel(self, key):
        self.getdel_calls.append(key)
        raise AssertionError("native GETDEL must not be used (needs Redis 6.2)")

    async def expire(self, key, seconds, nx=False, **kwargs):
        self.expire_calls.append({"key": key, "seconds": seconds, "nx": nx})
        raise AssertionError("EXPIRE ... NX must not be used (needs Redis 7.0)")

    # ── commands that are version-safe and stay ──────────────────────────
    async def set(self, key, value, ex=None, nx=False, **kwargs):
        self.set_calls.append({"key": key, "ex": ex, "nx": nx})
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex:
            self.ttls[key] = ex
        return True

    async def get(self, key):
        self.get_calls.append(key)
        return self.store.get(key)

    async def delete(self, key):
        self.ttls.pop(key, None)
        return 1 if self.store.pop(key, None) is not None else 0

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def unlink(self, *keys):
        self.unlinked.extend(keys)
        return len(keys)

    async def keys(self, pattern):
        return list(self.scan_keys)

    def scan_iter(self, match="*"):
        async def _gen():
            for key in self.scan_keys:
                yield key
        return _gen()

    async def eval(self, body, numkeys, *args):
        self.eval_calls.append({"body": body, "numkeys": numkeys, "args": list(args)})
        keys, argv = list(args[:numkeys]), list(args[numkeys:])
        return run_lua(body, keys, argv, self.store, self.ttls, self.counters)


class FakeRedisWithScripts(FakeRedis):
    """Same, but supports register_script → the EVALSHA path."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.registered: List[str] = []
        self.handle_calls: List[dict] = []
        self.fail_script_once = False

    def register_script(self, body):
        self.registered.append(body)
        fake = self

        class _Handle:
            async def __call__(self, keys=None, args=None):
                fake.handle_calls.append({"keys": list(keys or []), "args": list(args or [])})
                if fake.fail_script_once:
                    fake.fail_script_once = False
                    raise Exception("NOSCRIPT No matching script")
                return run_lua(body, list(keys or []), list(args or []),
                               fake.store, fake.ttls, fake.counters)

        return _Handle()


def wired_cache(fake: FakeRedis, auth_fake: Optional[FakeRedis] = None) -> RedisCache:
    """A RedisCache pointed at fakes — no init, so no socket."""
    cache = RedisCache()
    cache.redis_client = fake
    cache.is_connected = True
    cache.auth_client = auth_fake
    return cache


def run(coro):
    return asyncio.run(coro)


# backend/ — two levels up from tests/.
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1 — Redis version compatibility
# ═══════════════════════════════════════════════════════════════════════════

class TestIncrWithExpireIsLua:
    def test_counts_and_expires_in_one_round_trip(self):
        fake = FakeRedis()
        cache = wired_cache(fake)
        value = run(cache.incr_with_expire("loginfail:1:abc", 900))

        assert value == 1
        assert fake.counters["loginfail:1:abc"] == 1
        assert fake.ttls["loginfail:1:abc"] == 900
        assert len(fake.eval_calls) == 1
        # The version-gated commands are gone from this path entirely.
        assert fake.expire_calls == []
        assert fake.getdel_calls == []

    def test_ttl_is_only_armed_on_the_first_increment(self):
        fake = FakeRedis()
        cache = wired_cache(fake)
        run(cache.incr_with_expire("loginfail:1:abc", 900))
        run(cache.incr_with_expire("loginfail:1:abc", 900))

        assert fake.counters["loginfail:1:abc"] == 2
        # The fake re-creates the real script's condition: TTL stays 900 because
        # the counter was created (and expired) on call one.
        assert fake.ttls["loginfail:1:abc"] == 900

    def test_rearms_a_ttl_that_is_missing(self):
        """A counter left without a TTL by any path gets one — never immortal."""
        fake = FakeRedis()
        fake.counters["refresh_gen:9"] = 4      # already exists ...
        # ... with no TTL, i.e. what an old failed EXPIRE would have left behind.
        cache = wired_cache(fake)
        value = run(cache.incr_with_expire("refresh_gen:9", 86400))

        assert value == 5
        assert fake.ttls["refresh_gen:9"] == 86400

    def test_fail_closed_when_redis_is_down(self):
        fake = FakeRedis(ping_ok=False)
        cache = wired_cache(fake)
        with pytest.raises(RedisUnavailableError):
            run(cache.incr_with_expire("loginfail:1:abc", 900))

    def test_script_is_registered_once_and_reused(self):
        fake = FakeRedisWithScripts()
        cache = wired_cache(fake)
        run(cache.incr_with_expire("loginfail:1:abc", 900))
        run(cache.incr_with_expire("loginfail:1:abc", 900))

        assert fake.registered == [LUA_INCR_WITH_EXPIRE]     # loaded once
        assert len(fake.handle_calls) == 2
        assert fake.eval_calls == []                          # EVALSHA path taken

    def test_noscript_falls_back_to_eval(self):
        """A server restart flushes the script cache — that must not 503 a login."""
        fake = FakeRedisWithScripts()
        fake.fail_script_once = True
        cache = wired_cache(fake)
        value = run(cache.incr_with_expire("loginfail:1:abc", 900))

        assert value == 1
        assert len(fake.eval_calls) == 1                      # reload via EVAL

    def test_client_without_register_script_uses_eval(self):
        fake = FakeRedis()                                    # no register_script
        assert not hasattr(fake, "register_script")
        cache = wired_cache(fake)
        assert run(cache.incr_with_expire("loginfail:1:abc", 900)) == 1
        assert len(fake.eval_calls) == 1


class TestAuthGetdelIsLuaAlways:
    def test_reads_and_removes_atomically(self):
        fake = FakeRedis(store={"refresh_token:jti-1": json.dumps("7")})
        cache = wired_cache(fake)
        value = run(cache.auth_getdel("refresh_token:jti-1"))

        assert value == "7"
        assert "refresh_token:jti-1" not in fake.store
        assert fake.getdel_calls == []                        # native GETDEL unused
        assert len(fake.eval_calls) == 1

    def test_a_token_cannot_be_consumed_twice(self):
        fake = FakeRedis(store={"refresh_token:jti-1": json.dumps("7")})
        cache = wired_cache(fake)
        assert run(cache.auth_getdel("refresh_token:jti-1")) == "7"
        assert run(cache.auth_getdel("refresh_token:jti-1")) is None

    def test_missing_key_is_none_not_an_error(self):
        cache = wired_cache(FakeRedis())
        assert run(cache.auth_getdel("refresh_token:nope")) is None

    def test_fail_closed_when_redis_is_down(self):
        cache = wired_cache(FakeRedis(ping_ok=False))
        with pytest.raises(RedisUnavailableError):
            run(cache.auth_getdel("refresh_token:jti-1"))


class TestAuthPrimitivesStillFailClosed:
    """The strict rule: fail-open for data, fail-closed for auth — unchanged."""

    def test_auth_set_get_delete(self):
        fake = FakeRedis()
        cache = wired_cache(fake)
        run(cache.auth_set("access_jti:1:abc", "1", expire=900))
        assert run(cache.auth_get("access_jti:1:abc")) == "1"
        assert run(cache.auth_delete("access_jti:1:abc")) is True
        assert run(cache.auth_get("access_jti:1:abc")) is None

    def test_auth_set_nx_lock_semantics(self):
        fake = FakeRedis()
        cache = wired_cache(fake)
        assert run(cache.auth_set_nx("oauth_state:google:x", "y", 600)) is True
        assert run(cache.auth_set_nx("oauth_state:google:x", "y", 600)) is False

    def test_auth_scan_iter(self):
        fake = FakeRedis()
        fake.scan_keys = ["session_index:1:a", "session_index:1:b"]
        cache = wired_cache(fake)
        assert run(cache.auth_scan_iter("session_index:1:*")) == [
            "session_index:1:a", "session_index:1:b",
        ]

    def test_data_path_falls_open(self):
        down = wired_cache(FakeRedis(ping_ok=False))
        assert run(down.get("rs:latest:1")) is None
        assert run(down.set("rs:latest:1", {"a": 1}, expire=60)) is False
        assert run(down.unlink("rs:latest:1")) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1 (flows) — the three security paths that used to break on old Redis
# ═══════════════════════════════════════════════════════════════════════════

class TestSecurityFlowsOverLua:
    def test_bump_refresh_generation(self):
        from app.core import auth as auth_core

        fake = FakeRedis()
        cache = wired_cache(fake)
        with patch.object(auth_core, "redis_cache", cache):
            assert run(auth_core.bump_refresh_generation(9)) == 1
            assert run(auth_core.bump_refresh_generation(9)) == 2
        # The epoch counter expires — an immortal refresh_gen would outlive the
        # family it is supposed to kill, and revocation would silently stop work.
        assert fake.ttls["refresh_gen:9"] > 0

    def test_loginfail_counter_used_by_the_login_route(self):
        from app.api.routes import auth as auth_routes

        cache = wired_cache(FakeRedis())
        with patch.object(auth_routes, "redis_cache", cache):
            first = run(auth_routes._note_failed_login_for_ip(3, "203.0.113.9"))
            second = run(auth_routes._note_failed_login_for_ip(3, "203.0.113.9"))
        assert (first, second) == (1, 2)
        assert second < 5  # below the lockout threshold, no revocation expected

    def test_login_counter_degrades_to_none_instead_of_500(self):
        """A Redis outage must not turn the login form into a 500 (fail-soft by design)."""
        from app.api.routes import auth as auth_routes

        cache = wired_cache(FakeRedis(ping_ok=False))
        with patch.object(auth_routes, "redis_cache", cache):
            assert run(auth_routes._note_failed_login_for_ip(3, "203.0.113.9")) is None

    def test_rotation_consumes_then_revokes_on_replay(self):
        from app.core import auth as auth_core
        from app.core.auth import create_refresh_token, decode_token

        raw = create_refresh_token({"sub": "7"})
        jti = decode_token(raw)["jti"]
        fake = FakeRedis(store={f"refresh_token:{jti}": json.dumps("7")})
        cache = wired_cache(fake)

        async def scenario():
            with patch.object(auth_core, "redis_cache", cache), patch.object(
                auth_core, "invalidate_all_sessions", new_callable=AsyncMock
            ) as invalidate:
                # First use: consumed atomically, jti returned, tombstone written.
                assert await auth_core.consume_refresh_token(7, raw) == jti
                assert f"refresh_used:7:{jti}" in fake.store

                # Second use of the same token = reuse → 401 + family revoked.
                with pytest.raises(Exception) as exc:
                    await auth_core.consume_refresh_token(7, raw)
                assert getattr(exc.value, "status_code", None) == 401
                invalidate.assert_awaited_once_with(7)
                assert fake.counters["refresh_gen:7"] == 1

        run(scenario())

    def test_rotation_replay_bumps_the_generation(self):
        from app.core import auth as auth_core
        from app.core.auth import create_refresh_token, decode_token

        raw = create_refresh_token({"sub": "7"})
        jti = decode_token(raw)["jti"]
        fake = FakeRedis(store={
            f"refresh_used:7:{jti}": json.dumps("1"),   # already consumed once
        })
        cache = wired_cache(fake)

        async def scenario():
            with patch.object(auth_core, "redis_cache", cache), patch.object(
                auth_core, "invalidate_all_sessions", new_callable=AsyncMock
            ) as invalidate:
                with pytest.raises(Exception) as exc:
                    await auth_core.consume_refresh_token(7, raw)
                assert getattr(exc.value, "status_code", None) == 401
                invalidate.assert_awaited_once_with(7)
        run(scenario())
        # Reuse revokes the family: refresh_gen went up through the Lua script.
        assert fake.counters.get(f"refresh_gen:7") == 1


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2 — health, startup warning, counters
# ═══════════════════════════════════════════════════════════════════════════

class TestHealthProbe:
    def test_ok_reports_version_and_latency(self):
        cache = wired_cache(FakeRedis(version="7.0.15"))
        info = run(cache.health())

        assert info["status"] == "ok"
        assert info["version"] == "7.0.15"
        assert info["ping_ms"] is not None and info["ping_ms"] >= 0
        assert "redis://localhost" in info["target"] or "://" in info["target"]

    def test_never_raises_when_redis_is_down(self):
        fake = FakeRedis(ping_ok=False)
        cache = wired_cache(fake)
        cache.is_connected = True
        info = run(cache.health())

        assert info["status"] == "unavailable"
        assert info["error"] == "ConnectionError"
        assert info["ping_ms"] is None

    def test_probe_builds_and_closes_its_own_client(self, monkeypatch):
        created = []

        def _fake_connect(url):
            fake = FakeRedis()
            created.append(fake)
            return fake

        monkeypatch.setattr(RedisCache, "_connect", staticmethod(_fake_connect))
        cache = RedisCache()
        info = run(cache.health())

        assert info["status"] == "ok"
        assert created[0].closed is True      # no leaked pool per liveness poll

    def test_version_is_cached_after_the_first_probe(self):
        fake = FakeRedis()
        calls = []
        original = fake.info

        async def counting_info(section=None):
            calls.append(section)
            return await original(section)

        fake.info = counting_info
        cache = wired_cache(fake)
        run(cache.health())
        run(cache.health())
        assert len(calls) == 1


class TestMasking:
    def test_password_never_survives_masking(self):
        masked = mask_target("redis://:s3cr3t@cache.example.com:6379/0")
        assert masked == "redis://cache.example.com:6379/0"
        assert "s3cr3t" not in masked

    def test_unparseable_or_missing_urls_are_safe_placeholders(self):
        assert mask_target(None) == "redis://(not configured)"
        assert "secret" not in str(mask_target("redis://http://bad:"))


class TestHealthEndpoint:
    def _client(self):
        from fastapi.testclient import TestClient
        import app.main as main_module
        return TestClient(main_module.app), main_module

    def test_redis_outage_is_degraded_not_500(self):
        client, main_module = self._client()
        with patch.object(main_module.redis_cache, "health",
                          new_callable=AsyncMock,
                          return_value={"status": "unavailable", "target": "redis://x:1/0",
                                        "version": None, "ping_ms": None,
                                        "breaker": "open"}):
            response = client.get("/health")

        assert response.status_code == 200          # Render must not restart us
        assert response.json()["status"] == "degraded"
        assert response.json()["redis_connected"] is False

    def test_healthy_reports_the_probe(self):
        client, main_module = self._client()
        probe = {"status": "ok", "target": "redis://x:1/0", "version": "7.2.4",
                 "ping_ms": 1.5, "breaker": "closed"}
        with patch.object(main_module.redis_cache, "health",
                          new_callable=AsyncMock, return_value=probe):
            payload = client.get("/health").json()

        assert payload["status"] == "healthy"
        assert payload["redis"] == probe
        assert payload["redis_connected"] is True

    def test_startup_warning_names_the_target_without_credentials(self):
        """The silent fail-open that hid a dead port for weeks must be loud."""
        import app.main as main_module

        source = inspect.getsource(main_module.startup_event)
        assert "Redis UNREACHABLE" in source
        assert "describe_target()" in source      # host:port, never the password
        assert "FAILS CLOSED" in source
        # The raw setting is never interpolated — that is where the AUTH lives.
        assert "settings.REDIS_URL" not in source

    def test_describe_target_strips_credentials(self, monkeypatch):
        monkeypatch.setattr(redis_module.settings, "REDIS_URL",
                            "redis://:hunter2@redis.example.com:7424/0")
        assert RedisCache().describe_target() == "redis://redis.example.com:7424/0"


class TestCacheCounters:
    def test_hit_miss_set_and_error_are_per_namespace(self):
        fake = FakeRedis(store={"rs:latest:min_rs:none:limit:50": json.dumps({"x": 1})})
        cache = wired_cache(fake)

        run(cache.get("rs:latest:min_rs:none:limit:50"))      # hit
        run(cache.get("prices:latest:limit:10"))               # miss
        run(cache.set("prices:latest:limit:10", {"a": 1}, expire=60))   # set
        run(cache.get("refresh_token:jti"))                    # hit (auth bucket)
        fake.store["refresh_token:jti"] = json.dumps("1")
        run(cache.get("refresh_token:jti"))

        snapshot = cache.stats_snapshot()
        assert snapshot["namespaces"]["rs"]["hit"] == 1
        assert snapshot["namespaces"]["prices"]["miss"] == 1
        assert snapshot["namespaces"]["prices"]["set"] == 1
        # First read missed (the key was written after it), second one hit.
        assert snapshot["namespaces"]["auth"]["hit"] == 1
        assert snapshot["namespaces"]["auth"]["miss"] == 1
        assert 0.0 <= snapshot["hit_ratio"] <= 1.0
        assert "totals" in snapshot and "breaker" in snapshot

    def test_unreachable_counts_as_error_not_miss(self):
        cache = wired_cache(FakeRedis(ping_ok=False))
        run(cache.get("rs:latest:1"))
        assert cache.stats_snapshot()["namespaces"]["rs"]["error"] == 1

    def test_reset_and_module_helper(self):
        cache = wired_cache(FakeRedis())
        run(cache.get("rs:latest:1"))
        cache.reset_stats()
        assert cache.stats_snapshot()["namespaces"] == {}
        assert "totals" in redis_module.cache_stats()

    def test_periodic_log_is_throttled_and_key_free(self, monkeypatch, caplog):
        cache = wired_cache(FakeRedis())
        run(cache.get("refresh_token:super-secret-jti"))
        cache.reset_stats()

        # Default interval (5 min): nothing is logged on a normal request.
        with caplog.at_level("INFO", logger="app.core.redis"):
            run(cache.get("refresh_token:super-secret-jti"))
        assert [r for r in caplog.records if "cache_stats" in r.getMessage()] == []

        monkeypatch.setattr(redis_module, "CACHE_STATS_LOG_INTERVAL_SECONDS", 0.0)
        with caplog.at_level("INFO", logger="app.core.redis"):
            run(cache.get("refresh_token:super-secret-jti"))

        lines = [r.getMessage() for r in caplog.records if "cache_stats" in r.getMessage()]
        assert len(lines) == 1
        payload = json.loads(lines[0].split("cache_stats ", 1)[1])
        # 2 = the silent read above plus the one that tripped the (now 0s) timer.
        assert payload["namespaces"]["auth"]["miss"] == 2   # THIS cache, not the singleton
        assert "super-secret-jti" not in lines[0]           # never the raw key

    def test_namespace_labels(self):
        assert namespace_of("industry:groups:latest") == "industry:groups"
        assert namespace_of("rebh:breadth:overview") == "rebh"
        assert namespace_of("terminal:quant_lab") == "terminal"
        assert namespace_of("refresh_token:abc") == "auth"
        assert namespace_of("loginfail:1:deadbeef") == "auth"
        assert namespace_of("tadawul_stocks:1") == "tadawul_stocks"
        assert namespace_of(b"rs:latest:1") == "rs"
        # Stats never echo the raw key back for an unknown namespace.
        assert namespace_of("unknown:prefix:key") == "unknown"


# ═══════════════════════════════════════════════════════════════════════════
# Phase 7 — KEYS quarantine, TTL discipline, breaker, auth instance split
# ═══════════════════════════════════════════════════════════════════════════

class TestBlockingKeysQuarantined:
    def test_renamed_method_warns_and_works(self):
        fake = FakeRedis()
        fake.scan_keys = ["tadawul_stocks:1150"]
        cache = wired_cache(fake)
        with pytest.warns(DeprecationWarning):
            got = run(cache.keys("tadawul_stocks:*"))
        assert got == ["tadawul_stocks:1150"]

    def test_alias_and_implementation_both_exist_for_backward_compat(self):
        assert hasattr(RedisCache, "keys_blocking_deprecated")
        assert hasattr(RedisCache, "keys")

    def test_no_application_module_calls_the_blocking_keys_helper(self):
        root = BACKEND_DIR
        offenders = []
        for path in glob.glob(os.path.join(root, "**", "*.py"), recursive=True):
            if os.path.basename(path) == "redis.py" or f"tests{os.sep}" in path:
                continue
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            if "redis_cache.keys(" in source:
                offenders.append(os.path.relpath(path, root))
        assert offenders == []

    def test_admin_stats_endpoint_uses_the_cursor_scan(self):
        from app.api.routes import admin as admin_routes

        source = inspect.getsource(admin_routes)
        assert 'scan_iter("tadawul_stocks:*")' in source
        assert "redis_cache.keys(" not in source


class TestTTLDiscipline:
    def test_missing_ttl_falls_back_and_warns_once(self, caplog):
        fake = FakeRedis()
        cache = wired_cache(fake)
        with caplog.at_level("WARNING", logger="app.core.redis"):
            assert run(cache.set("rs:latest:1", {"a": 1})) is True
            assert run(cache.set("rs:latest:2", {"a": 1})) is True

        assert fake.set_calls[0]["ex"] == DEFAULT_CACHE_TTL
        warned = [r for r in caplog.records if "without an explicit TTL" in r.getMessage()]
        assert len(warned) == 1                      # once per namespace, not per write

    def test_zero_ttl_does_not_create_an_immortal_key(self):
        fake = FakeRedis()
        cache = wired_cache(fake)
        run(cache.set("prices:latest:limit:5", {"a": 1}, expire=0))
        assert fake.set_calls[0]["ex"] == DEFAULT_CACHE_TTL

    def test_explicit_configured_ttl_is_untouched(self):
        """The strict rule: no existing TTL changes."""
        from app.core.cache_config import CACHE_TTL_SCREENERS

        fake = FakeRedis()
        cache = wired_cache(fake)
        run(cache.set("rs:latest:1", {"a": 1}, expire=CACHE_TTL_SCREENERS))
        assert fake.set_calls[0]["ex"] == CACHE_TTL_SCREENERS

    def test_positional_expire_still_works(self):
        fake = FakeRedis()
        cache = wired_cache(fake)
        run(cache.set("rs:latest:1", {"a": 1}, 120))
        assert fake.set_calls[0]["ex"] == 120

    def test_read_through_helper_resolves_a_missing_ttl(self):
        from app.core import cache_helpers

        fake = FakeRedis()
        cache = wired_cache(fake)

        async def fetch():
            return {"rows": 1}

        with patch.object(cache_helpers, "redis_cache", cache):
            run(cache_helpers.cache_read_through("terminal:quant_lab", None, fetch))
        # Part 2 / Phase 3 turned the resolved fallback into a FLOOR: a TTL of at
        # least TTL_JITTER_THRESHOLD_SECONDS is extended by a random 0-10 % so a
        # namespace cannot expire in one second. The fallback still applies, and
        # it is never shortened — so the assertion is a band, not an equality.
        stored = fake.set_calls[-1]["ex"]
        assert DEFAULT_CACHE_TTL <= stored <= int(DEFAULT_CACHE_TTL * 1.1)

        # A different key, so this is a miss again: an explicit ttl passes
        # through untouched (the strict "no TTL changes" rule) because 60 s is
        # below the jitter threshold.
        fake.set_calls.clear()
        with patch.object(cache_helpers, "redis_cache", cache):
            run(cache_helpers.cache_read_through("terminal:audit_summary", 60, fetch))
        assert fake.set_calls[-1]["ex"] == 60


class TestInvalidatePatternsRefusesAuthKeys:
    def test_auth_keys_are_never_unlinked(self):
        from app.core import cache_helpers

        keys = ["rs:latest:1", "refresh_token:jti-1", "session_index:1:a"]
        fake = FakeRedis(store={k: "v" for k in keys})
        fake.scan_keys = list(keys)
        cache = wired_cache(fake)

        with patch.object(cache_helpers, "redis_cache", cache):
            run(cache_helpers.invalidate_patterns(["*"]))

        assert fake.unlinked == ["rs:latest:1"]
        assert "refresh_token:jti-1" in fake.store
        assert "session_index:1:a" in fake.store

    def test_every_documented_auth_prefix_is_refused(self):
        for prefix in AUTH_KEY_PREFIXES:
            assert cache_helpers_is_auth(prefix + "anything") is True

    def test_cache_prefixes_are_not_mistaken_for_auth(self):
        assert cache_helpers_is_auth("rs:latest:1") is False
        assert cache_helpers_is_auth("rebh:breadth:x") is False


def cache_helpers_is_auth(key: str) -> bool:
    from app.core import cache_helpers
    return cache_helpers._is_auth_key(key)


class TestCircuitBreaker:
    def test_opens_after_three_failures_and_stops_calling_redis(self, monkeypatch):
        attempts = []

        class Down:
            async def ping(self):
                attempts.append(1)
                raise ConnectionError("down")

            async def aclose(self):
                pass

        def _connect(url):
            return Down()

        monkeypatch.setattr(RedisCache, "_connect", staticmethod(_connect))
        cache = RedisCache()

        for _ in range(3):
            assert run(cache.get("rs:latest:1")) is None
        assert len(attempts) == 3
        assert cache.breaker_state()["state"] == RedisCircuitBreaker.OPEN

        # Now the breaker is open: no more connect attempts, no 5s timeout paid.
        before = len(attempts)
        for _ in range(50):
            assert run(cache.get("rs:latest:1")) is None
        assert len(attempts) == before

    def test_half_open_probe_closes_the_breaker_on_success(self, monkeypatch):
        down, up = FakeRedis(ping_ok=False), FakeRedis()
        state = {"client": down}
        monkeypatch.setattr(RedisCache, "_connect", staticmethod(lambda url: state["client"]))
        cache = RedisCache()
        cache._breaker = RedisCircuitBreaker(failure_threshold=1, reset_timeout=0.01)

        assert run(cache.get("rs:latest:1")) is None            # 1 failure -> open
        assert cache.breaker_state()["state"] == RedisCircuitBreaker.OPEN

        import time
        time.sleep(0.02)                                        # reset window elapsed
        state["client"] = up                                    # Redis came back
        assert run(cache.get("rs:latest:1")) is None            # probe ran, key absent
        assert cache.breaker_state()["state"] == RedisCircuitBreaker.CLOSED
        assert run(cache.set("rs:latest:1", {"a": 1}, expire=60)) is True
        assert up.store                                         # writes work again

    def test_auth_path_is_never_gated_by_the_breaker(self):
        """Fail-closed must keep TRYING: a breaker meant for latency cannot be
        the reason a token allowlist read is skipped."""
        fake = FakeRedis()
        cache = wired_cache(fake)
        cache._breaker = RedisCircuitBreaker(failure_threshold=1, reset_timeout=3600)
        cache._breaker.record_failure()
        assert cache.breaker_state()["state"] == RedisCircuitBreaker.OPEN

        # Auth still reaches Redis (and answers None = the token is unknown).
        # A successful auth ping re-closes the breaker on a shared instance,
        # which is honest: the same box just answered.
        assert run(cache.auth_get("access_jti:1:abc")) is None
        assert len(fake.get_calls) == 1

        # With the breaker open again, the data path is short-circuited — it does
        # not even touch the client, which is the whole point (no 5s timeout).
        cache._breaker.record_failure()
        assert cache.breaker_state()["state"] == RedisCircuitBreaker.OPEN
        assert run(cache.get("rs:latest:1")) is None
        assert len(fake.get_calls) == 1

        # ... while auth still tries, and fails closed rather than answering
        # "no such token" from a skipped read.
        cache._breaker.record_failure()
        with patch.object(redis_module.settings, "REDIS_AUTH_URL", "redis://auth-box:6379/0"):
            cache.auth_client = FakeRedis(ping_ok=False)
            with pytest.raises(RedisUnavailableError):
                run(cache.auth_get("access_jti:1:abc"))

    def test_breaker_thresholds_come_from_config(self):
        from app.core import cache_config

        breaker = RedisCircuitBreaker()
        assert breaker.failure_threshold == cache_config.REDIS_BREAKER_FAILURE_THRESHOLD
        assert breaker.reset_timeout == cache_config.REDIS_BREAKER_RESET_SECONDS


class TestAuthInstanceSplit:
    def test_unset_keeps_the_single_shared_instance(self, monkeypatch):
        monkeypatch.setattr(redis_module.settings, "REDIS_AUTH_URL", None)
        fake = FakeRedis()
        cache = wired_cache(fake)

        assert run(cache._require_auth_client()) is fake

    def test_auth_keys_go_to_the_auth_instance(self, monkeypatch):
        monkeypatch.setattr(redis_module.settings, "REDIS_AUTH_URL",
                            "redis://auth-box:6379/0")
        data, auth = FakeRedis(), FakeRedis()
        cache = wired_cache(data, auth_fake=auth)

        run(cache.auth_set("refresh_token:jti-1", "7", expire=60))
        assert run(cache.auth_get("refresh_token:jti-1")) == "7"
        assert run(cache.get("rs:latest:1")) is None

        assert auth.store == {"refresh_token:jti-1": json.dumps("7")}
        assert "refresh_token:jti-1" not in data.store
        assert data.set_calls == []                       # data box untouched

    def test_unreachable_auth_box_fails_closed_without_fallback(self, monkeypatch):
        monkeypatch.setattr(redis_module.settings, "REDIS_AUTH_URL",
                            "redis://auth-box:6379/0")
        data, auth = FakeRedis(), FakeRedis(ping_ok=False)
        cache = wired_cache(data, auth_fake=auth)

        with pytest.raises(RedisUnavailableError):
            run(cache.auth_get("refresh_token:jti-1"))
        # Never silently reads the shared cache box instead.
        assert data.get_calls == []

    def test_data_path_still_works_when_the_auth_box_is_down(self, monkeypatch):
        monkeypatch.setattr(redis_module.settings, "REDIS_AUTH_URL",
                            "redis://auth-box:6379/0")
        data, auth = FakeRedis(store={"rs:latest:1": json.dumps({"a": 1})}), FakeRedis(ping_ok=False)
        cache = wired_cache(data, auth_fake=auth)

        assert run(cache.get("rs:latest:1")) == {"a": 1}       # fail-open intact
        with pytest.raises(RedisUnavailableError):
            run(cache.auth_getdel("refresh_token:jti-1"))      # fail-closed intact

    def test_docs_describe_the_split(self):
        path = os.path.join(BACKEND_DIR, "docs", "redis-production.md")
        assert os.path.exists(path), path
        with open(path, encoding="utf-8") as handle:
            text = handle.read()

        assert "volatile-lru" in text
        assert "allkeys-lru" in text and "forbidden" in text.lower()
        assert "REDIS_AUTH_URL" in text
        assert "GETDEL" in text and "6.2" in text
        assert "EXPIRE" in text and "7.0" in text
        assert "CONFIG SET" in text       # documented as an ops task, not app code

    def test_application_code_never_sends_config_set(self):
        root = BACKEND_DIR
        for path in glob.glob(os.path.join(root, "app", "**", "*.py"), recursive=True):
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            assert "config_set(" not in source
            assert "CONFIG SET" not in source
            assert "flushall()" not in source.lower()
            assert "flushdb()" not in source.lower()
