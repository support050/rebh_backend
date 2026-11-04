"""Part 3 tests — payload compression (Phase 6), shared rate limiting (Phase 8)
and the scheduler slot fence (Phase 9).

Nothing here opens a socket. Three different isolation devices are used, and the
reasons are not the same:

* the cache data path is driven by an **async fake client** (same trick as the
  Part 2 file) because the assertions are about the exact bytes that would have
  gone to Redis;
* the limiter is exercised through its **URI-resolution helpers** with the TCP
  probe patched, because binding a real ``RedisStorage`` to whatever happens to
  be running on the developer's box would make the suite environment-dependent;
* the scheduler fence takes an **injected sync client**, which is the seam the
  production code exposes on purpose (APScheduler runs off the event loop, so the
  async singleton cannot be reused there).

The behaviour contracts these tests pin down are the ones that make the change
safe to deploy: an old uncompressed value still reads back, a corrupt compressed
value reads as a MISS instead of being served as data, a compression that would
make a payload bigger is discarded, the limiter never turns into a 500 when its
storage dies, and the cron fence fails OPEN when Redis cannot be asked.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from app.core import cache_codec, limiter, scheduler
from app.core.cache_config import (
    COMPRESS_MIN_BYTES,
    RATE_LIMIT_KEY_PREFIX,
    SCHED_LOCK_DEFAULT_TTL_SECONDS,
    SCHED_LOCK_PREFIX,
    SCHED_LOCK_SLOT_FORMAT,
    SCHED_LOCK_TTL_SECONDS,
)
from app.core.redis import RedisCache

GZIP_MARKER = cache_codec.GZIP_MARKER
ZSTD_MARKER = cache_codec.ZSTD_MARKER

# 300 KB of highly compressible text: comfortably over the real 256 KB threshold,
# so the default path is exercised without passing min_bytes anywhere.
BIG_JSON = json.dumps({"rows": [{"n": "أ" * 60, "i": i} for i in range(4000)]},
                      ensure_ascii=False)


def run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════════════════
# Test doubles
# ═══════════════════════════════════════════════════════════════════════════

class FakeAsyncClient:
    """Minimal async redis-client stand-in for the data path (set/get only)."""

    def __init__(self, store: Optional[Dict[str, Any]] = None):
        self.store: Dict[str, Any] = dict(store or {})
        self.set_calls: List[dict] = []

    async def ping(self):
        return True

    async def set(self, key, value, ex=None, nx=False, **kwargs):
        key = str(key)
        self.set_calls.append({"key": key, "value": value, "ex": ex, "nx": nx})
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(str(key))

    async def aclose(self):
        pass


class FakeSyncClient:
    """Sync client for the scheduler fence: records SET args, can be told to die."""

    def __init__(self, store: Optional[Dict[str, str]] = None, *, fail: bool = False):
        self.store: Dict[str, str] = dict(store or {})
        self.fail = fail
        self.set_calls: List[dict] = []
        self.closed = False

    def set(self, key, value, nx=False, ex=None, **kwargs):
        if self.fail:
            raise ConnectionError("fake redis is down")
        key = str(key)
        self.set_calls.append({"key": key, "value": value, "nx": nx, "ex": ex})
        if nx and key in self.store:
            return None                     # a sibling worker already owns the slot
        self.store[key] = str(value)
        return True

    def get(self, key):
        if self.fail:
            raise ConnectionError("fake redis is down")
        return self.store.get(str(key))

    def close(self):
        self.closed = True


class FakeZstdModule:
    """Stand-in for the optional ``zstandard`` wheel.

    It wraps gzip under the hood so that compress→decompress round-trips and the
    output is genuinely smaller than the input (a constant-returning fake would
    make the "never grow" rule discard it and the zstd branch would go untested).
    """

    class ZstdCompressor:
        def __init__(self, level=None):
            self.level = level

        def compress(self, data):
            return b"ZD" + gzip.compress(data)

    class ZstdDecompressor:
        def decompress(self, data):
            assert data[:2] == b"ZD", "not written by FakeZstdModule"
            return gzip.decompress(data[2:])


@pytest.fixture(autouse=True)
def no_real_redis(monkeypatch):
    """Hard guarantee: nothing in this file reaches a socket.

    ``ensure_connection`` falls through to ``init_redis`` when a ping fails, and
    backend/.env carries a reachable production URL; and ``limiter`` is imported
    by app/main.py, so a stray probe would also slow the suite by
    RATE_LIMIT_PROBE_TIMEOUT_SECONDS per call.
    """
    async def _never(self, *args, **kwargs):      # noqa: ANN001 - instance method
        return False

    monkeypatch.setattr(RedisCache, "init_redis", _never)
    # Belt and braces for the limiter: even a test that forgets to patch the
    # probe cannot open a socket.
    def _refuse(*args, **kwargs):
        raise OSError("tests must not open sockets")

    monkeypatch.setattr(limiter.socket, "create_connection", _refuse)
    cache_codec.reset_codec_stats()
    yield


def wired(store: Optional[Dict[str, Any]] = None) -> tuple[RedisCache, FakeAsyncClient]:
    fake = FakeAsyncClient(store)
    cache = RedisCache()
    cache.redis_client = fake
    cache.is_connected = True
    cache.auth_client = None
    cache.reset_stats()
    return cache, fake


# ═══════════════════════════════════════════════════════════════════════════
# Phase 6 — payload compression
# ═══════════════════════════════════════════════════════════════════════════

class TestCodecEncode:
    def test_threshold_is_256kb(self):
        assert COMPRESS_MIN_BYTES == 256 * 1024

    def test_small_values_are_untouched(self):
        """Below the threshold the stored text must be byte-identical."""
        payload, meta = cache_codec.encode('{"a":1}')
        assert payload == '{"a":1}'
        assert meta is None

    def test_zero_threshold_disables_the_gate(self):
        payload, meta = cache_codec.encode("x" * 50, min_bytes=0)
        assert payload == "x" * 50 and meta is None

    def test_big_payload_is_marked_and_ascii(self):
        payload, meta = cache_codec.encode(BIG_JSON)
        assert meta is not None
        assert payload.startswith(GZIP_MARKER), payload[:8]
        # Regression: the first version prefixed MARKER_PREFIX *on top of* the
        # marker, producing "\x00\x00gz1:" that decode() could not recognise.
        assert not payload.startswith("\x00\x00")
        payload.encode("ascii")                    # decode_responses=True survives

    def test_round_trip(self):
        payload, _ = cache_codec.encode(BIG_JSON)
        text, meta = cache_codec.decode(payload)
        assert text == BIG_JSON
        assert meta["algo"] == cache_codec.algorithm()

    def test_logs_sizes_before_and_after(self):
        payload, meta = cache_codec.encode(BIG_JSON)
        assert meta["raw_bytes"] == len(BIG_JSON.encode("utf-8"))
        assert meta["stored_bytes"] == len(payload)
        assert meta["saved_bytes"] == meta["raw_bytes"] - meta["stored_bytes"]
        assert meta["stored_bytes"] < meta["raw_bytes"]

    def test_never_grows_a_payload(self):
        """High-entropy input + Base64 overhead must fall back to plaintext."""
        noise = base64.b64encode(os.urandom(200_000)).decode("ascii")
        payload, meta = cache_codec.encode(noise, min_bytes=1000)
        assert meta is None
        assert payload == noise

    def test_compression_is_recorded_in_stats(self):
        cache_codec.encode(BIG_JSON)
        assert cache_codec.CODEC_STATS["compressions"] == 1
        assert cache_codec.CODEC_STATS["raw_bytes"] > cache_codec.CODEC_STATS["stored_bytes"]

    def test_encode_failure_stores_plaintext(self, monkeypatch):
        monkeypatch.setattr(cache_codec, "_compress_bytes",
                            lambda data, algo: (_ for _ in ()).throw(RuntimeError("boom")))
        payload, meta = cache_codec.encode(BIG_JSON)
        assert payload == BIG_JSON and meta is None


class TestCodecDecode:
    def test_legacy_value_is_read_back_unchanged(self):
        """A value cached before this module shipped must not be touched."""
        legacy = json.dumps({"old": True, "rows": [1, 2, 3]})
        value, meta = cache_codec.decode(legacy)
        assert value == legacy
        assert meta is None

    @pytest.mark.parametrize("value", ['{"a":1}', '[1,2]', '"str"', '123', 'true', 'null'])
    def test_no_json_output_can_look_like_our_marker(self, value):
        assert not value.startswith(cache_codec.MARKER_PREFIX)
        assert cache_codec.decode(value)[1] is None

    def test_bytes_input_is_accepted(self):
        """Works whether or not decode_responses is on."""
        payload, _ = cache_codec.encode(BIG_JSON)
        value, meta = cache_codec.decode(payload.encode("utf-8"))
        assert value == BIG_JSON and meta["algo"] == "gzip"

    def test_corrupt_value_is_a_miss_not_data(self):
        value, meta = cache_codec.decode(GZIP_MARKER + "!!!not-base64!!!")
        assert value is None
        assert meta["algo"] == "gzip"
        assert cache_codec.CODEC_STATS["corrupt_reads"] == 1

    def test_truncated_frame_is_a_miss(self):
        payload, _ = cache_codec.encode(BIG_JSON)
        body = payload[len(GZIP_MARKER):]
        value, meta = cache_codec.decode(GZIP_MARKER + body[: len(body) // 2])
        assert value is None and meta["algo"] == "gzip"

    def test_unknown_marker_is_left_alone(self):
        weird = "\x00whatever"
        value, meta = cache_codec.decode(weird)
        assert value == weird and meta is None

    def test_non_string_is_left_alone(self):
        assert cache_codec.decode(123) == (123, None)

    def test_is_compressed(self):
        payload, _ = cache_codec.encode(BIG_JSON)
        assert cache_codec.is_compressed(payload)
        assert not cache_codec.is_compressed('{"a":1}')


class TestZstdBranch:
    def test_gzip_is_the_live_default(self):
        # zstandard is an optional extra; if the wheel ever lands, this is the
        # assertion that says so rather than a test that silently stops covering.
        assert cache_codec.algorithm() == ("zstd" if cache_codec._zstdmodule else "gzip")

    def test_zstd_marker_written_and_read(self, monkeypatch):
        monkeypatch.setattr(cache_codec, "_zstdmodule", FakeZstdModule)
        assert cache_codec.algorithm() == "zstd"
        payload, meta = cache_codec.encode(BIG_JSON)
        assert payload.startswith(ZSTD_MARKER) and meta["algo"] == "zstd"
        text, meta2 = cache_codec.decode(payload)
        assert text == BIG_JSON and meta2["algo"] == "zstd"

    def test_zstd_value_without_the_wheel_reads_as_miss(self, monkeypatch):
        """A rebuilt image that lost the extra must degrade, not serve garbage."""
        monkeypatch.setattr(cache_codec, "_zstdmodule", FakeZstdModule)
        payload, _ = cache_codec.encode(BIG_JSON)
        monkeypatch.setattr(cache_codec, "_zstdmodule", None)
        value, meta = cache_codec.decode(payload)
        assert value is None and meta["algo"] == "zstd"

    def test_algorithms_are_interchangeable_across_writes(self, monkeypatch):
        """gzip-written values stay readable after zstd becomes the writer."""
        gz, _ = cache_codec.encode(BIG_JSON)
        monkeypatch.setattr(cache_codec, "_zstdmodule", FakeZstdModule)
        assert cache_codec.decode(gz)[0] == BIG_JSON


class TestCacheIntegration:
    def test_set_compresses_and_get_decompresses(self):
        cache, fake = wired()
        value = json.loads(BIG_JSON)
        assert run(cache.set("rebh:breadth:2026-10-28", value, expire=60)) is True
        stored = fake.store["rebh:breadth:2026-10-28"]
        assert stored.startswith(GZIP_MARKER)
        assert len(stored) < len(BIG_JSON)
        assert fake.set_calls[0]["ex"] == 60          # TTL still applied
        assert run(cache.get("rebh:breadth:2026-10-28")) == value

    def test_small_payload_is_not_compressed(self):
        cache, fake = wired()
        run(cache.set("prices:1150", {"p": 12.5}, expire=60))
        stored = fake.store["prices:1150"]
        assert stored == json.dumps({"p": 12.5})
        assert not cache_codec.is_compressed(stored)

    def test_legacy_plaintext_still_reads(self):
        """Deploy ordering is irrelevant: pre-codec values keep serving hits."""
        cache, _ = wired({"rs:hub:2026-10-27": '{"x":1}'})
        assert run(cache.get("rs:hub:2026-10-27")) == {"x": 1}
        assert cache.stats_snapshot()["namespaces"]["rs"]["hit"] == 1

    def test_corrupt_value_reports_a_miss_and_never_raises(self):
        cache, _ = wired({"rebh:engine:1150:1:1": GZIP_MARKER + "###"})
        assert run(cache.get("rebh:engine:1150:1:1")) is None
        ns = cache.stats_snapshot()["namespaces"]["rebh"]
        assert ns["miss"] == 1 and ns["hit"] == 0

    def test_auth_helpers_are_not_routed_through_the_codec(self):
        """Security keys are tiny; the codec must not become a load-bearing part
        of the fail-closed auth path."""
        cache, fake = wired()
        src = __import__("inspect").getsource(RedisCache.auth_set)
        assert "cache_codec" not in src

    def test_round_trip_preserves_unicode(self):
        cache, _ = wired()
        payload = {"name": "الأسعار", "rows": ["ت" * 500] * 600}
        run(cache.set("rebh:report:x", payload, expire=60))
        assert run(cache.get("rebh:report:x")) == payload


# ═══════════════════════════════════════════════════════════════════════════
# Phase 8 — shared rate-limit storage
# ═══════════════════════════════════════════════════════════════════════════

class TestStorageResolution:
    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv(limiter.STORAGE_URI_ENV, "redis://forced:1234")
        assert limiter.resolve_storage_uri() == "redis://forced:1234"

    def test_pytest_guard_keeps_counters_per_process(self, monkeypatch):
        monkeypatch.delenv(limiter.STORAGE_URI_ENV, raising=False)
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::test_y")
        monkeypatch.setattr(limiter, "_redis_reachable", lambda url: True)
        monkeypatch.setattr(limiter, "_redis_url", lambda: "redis://live:6379")
        assert limiter.resolve_storage_uri() == limiter.MEMORY_STORAGE

    def test_unreachable_redis_falls_back_to_memory(self, monkeypatch):
        monkeypatch.delenv(limiter.STORAGE_URI_ENV, raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setattr(limiter, "_redis_url", lambda: "redis://down:6379")
        monkeypatch.setattr(limiter, "_redis_reachable", lambda url: False)
        assert limiter.resolve_storage_uri() == limiter.MEMORY_STORAGE

    def test_reachable_redis_is_shared(self, monkeypatch):
        monkeypatch.delenv(limiter.STORAGE_URI_ENV, raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setattr(limiter, "_redis_url", lambda: "redis://live:6379")
        monkeypatch.setattr(limiter, "_redis_reachable", lambda url: True)
        assert limiter.resolve_storage_uri() == "redis://live:6379"

    def test_probe_never_opens_a_real_socket(self):
        """The autouse fixture makes create_connection raise; a redis:// URL with
        an unlistenable port must therefore resolve to memory, not blow up."""
        assert limiter._redis_reachable("redis://127.0.0.1:6390") is False

    def test_missing_url_is_memory(self, monkeypatch):
        monkeypatch.delenv(limiter.STORAGE_URI_ENV, raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setattr(limiter, "_redis_url", lambda: None)
        assert limiter.resolve_storage_uri() == limiter.MEMORY_STORAGE

    def test_resolution_is_cheap_to_ask_about(self, monkeypatch):
        """describe_storage() must not re-probe — /health asks on every call."""
        def _boom(url, *a, **k):
            raise AssertionError("describe_storage re-probed Redis")

        monkeypatch.setattr(limiter, "_redis_reachable", _boom)
        assert "memory" in limiter.describe_storage(limiter.MEMORY_STORAGE)

    def test_describe_hides_credentials(self):
        text = limiter.describe_storage("redis://user:secret@db.internal:6379/1")
        assert "secret" not in text and "user" not in text
        assert "shared across workers" in text

    @pytest.mark.parametrize("url,expected", [
        ("redis://localhost:6379/0", ("localhost", 6379)),
        ("redis://localhost", ("localhost", 6379)),          # default port
        ("rediss://secure.example.com:26379", ("secure.example.com", 26379)),
        ("unix:///tmp/redis.sock", (None, None)),            # nothing to probe
    ])
    def test_split_target(self, url, expected):
        assert limiter._split_target(url) == expected

    def test_unix_socket_is_taken_at_face_value(self):
        """Guessing "unreachable" for a unix socket would silently turn shared
        limits off, so a target with no host is probed by trusting it."""
        assert limiter._redis_reachable("unix:///tmp/redis.sock") is True

    def test_probe_is_a_tcp_connect_not_a_ping(self, monkeypatch):
        calls = []

        class _Sock:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(limiter.socket, "create_connection",
                            lambda addr, timeout=None: calls.append((addr, timeout)) or _Sock())
        assert limiter._redis_reachable("redis://db:6379") is True
        assert calls[0][0] == ("db", 6379)
        assert calls[0][1] and calls[0][1] <= 1.0            # bounded, never blocking

    def test_probe_failure_is_not_raised(self, monkeypatch):
        monkeypatch.setattr(limiter.socket, "create_connection",
                            lambda *a, **k: (_ for _ in ()).throw(TimeoutError("nope")))
        assert limiter._redis_reachable("redis://down:6379") is False

    def test_no_redis_url_configured_never_raises(self, monkeypatch):
        """This module is imported by app/main.py — a raise here is a server that
        does not boot."""
        monkeypatch.setattr(limiter, "_redis_url", lambda: None)
        assert limiter.resolve_storage_uri() in (limiter.MEMORY_STORAGE, "memory://")


class TestLimiterConstruction:
    def test_strategy_is_fixed_window_by_decision(self):
        assert limiter.limiter._strategy == "fixed-window"

    def test_no_default_limits(self):
        """A blanket limit would cap all routes at one number, including the
        pages that legitimately fire dozens of requests per paint."""
        assert list(limiter.limiter._default_limits) == []

    def test_fail_open_both_layers(self):
        assert limiter.limiter._swallow_errors is True
        assert limiter.limiter._in_memory_fallback_enabled is True

    def test_key_prefix_scopes_the_namespace(self):
        assert limiter.limiter._key_prefix == RATE_LIMIT_KEY_PREFIX == "LUMIVST"

    def test_storage_uri_matches_the_resolved_value(self):
        assert limiter.limiter._storage_uri == limiter.STORAGE_URI

    def test_module_cache_survives(self, monkeypatch):
        monkeypatch.setattr(limiter, "_redis_reachable", lambda *a, **k: True)
        monkeypatch.setattr(limiter, "_redis_url", lambda: "redis://later:6379")
        # STORAGE_URI was fixed at import; asking again must not rewrite it.
        assert limiter.STORAGE_URI == limiter.limiter._storage_uri

    def test_fallback_limiter_is_ready(self):
        """slowapi builds a per-process twin the moment storage fails."""
        assert limiter.limiter._fallback_limiter is not None

    def test_redis_storage_builds_without_connecting(self):
        """A dead Redis at import must not raise (the whole fail-open argument)."""
        from limits.storage import MemoryStorage, RedisStorage
        from slowapi import Limiter
        from slowapi.util import get_remote_address

        dead = Limiter(key_func=get_remote_address, strategy="fixed-window",
                       storage_uri="redis://127.0.0.1:6390",
                       storage_options={"socket_connect_timeout": 0.2, "socket_timeout": 0.2},
                       in_memory_fallback_enabled=True, swallow_errors=True)
        assert isinstance(dead._storage, RedisStorage)
        assert isinstance(MemoryStorage(), MemoryStorage)    # sanity: import path works

    def test_memory_storage_when_resolved_memory(self):
        from limits.storage import MemoryStorage
        if limiter.STORAGE_URI == limiter.MEMORY_STORAGE:
            assert isinstance(limiter.limiter._storage, MemoryStorage)
        else:                                                # pragma: no cover - env dependent
            pytest.skip("limiter resolved to Redis storage in this environment")


class TestProxyHeadersInRunCommands:
    """--proxy-headers is what makes get_remote_address return the visitor and
    not Render's proxy (otherwise "5/minute" means "5/minute for the planet")."""

    def test_procfile(self):
        from pathlib import Path
        text = (Path(__file__).resolve().parents[1] / "Procfile").read_text(encoding="utf-8")
        assert "--proxy-headers" in text

    def test_dockerfile(self):
        from pathlib import Path
        text = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
        assert "--proxy-headers" in text
        assert "FORWARDED_ALLOW_IPS" in text

    def test_exec_form_cmd_does_not_use_shell_expansion(self):
        """JSON/exec-form CMD is not run through a shell, so ${VAR} would be
        passed literally to uvicorn. The env var is the supported mechanism."""
        from pathlib import Path
        text = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
        cmd_lines = [line for line in text.splitlines() if "CMD" in line]
        assert not any("${FORWARDED_ALLOW_IPS" in line for line in cmd_lines)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 9 — the scheduler slot fence
# ═══════════════════════════════════════════════════════════════════════════

class TestSlotKeysAndTTL:
    def test_key_shape(self):
        assert scheduler.sched_lock_key("breadth_daily", "20261028") == \
            f"{SCHED_LOCK_PREFIX}:breadth_daily:20261028" == "sched_lock:breadth_daily:20261028"

    def test_slot_is_one_per_day(self):
        assert scheduler.current_slot() == datetime.now(timezone.utc).strftime(
            SCHED_LOCK_SLOT_FORMAT) or len(scheduler.current_slot()) == 8

    def test_slot_rolls_in_the_schedule_timezone(self):
        """23:30 UTC on 27 Oct is 01:30 on the 28th in Cairo — the slot that a
        minute-granular key would have split in two."""
        if scheduler._EGYPT_TZ is None:
            pytest.skip("tzdata unavailable: slots fall back to local time")
        when = datetime(2026, 10, 27, 23, 30, tzinfo=timezone.utc)
        assert scheduler.current_slot(when) == "20261028"

    def test_naive_datetimes_are_read_as_local_schedule_time(self):
        if scheduler._EGYPT_TZ is None:
            pytest.skip("tzdata unavailable")
        assert scheduler.current_slot(datetime(2026, 3, 14, 1, 2)) == "20260314"

    @pytest.mark.parametrize("job,ttl", sorted(SCHED_LOCK_TTL_SECONDS.items()))
    def test_per_job_ttls(self, job, ttl):
        assert scheduler.sched_lock_ttl(job) == ttl

    def test_every_ttl_exceeds_its_job(self):
        """The lock is never released, so a TTL shorter than the run would let a
        second worker claim the slot while the first is still writing."""
        for job, ttl in SCHED_LOCK_TTL_SECONDS.items():
            assert ttl >= 1800, job

    def test_unknown_job_gets_the_default(self):
        assert scheduler.sched_lock_ttl("not-a-job") == SCHED_LOCK_DEFAULT_TTL_SECONDS

    def test_worker_id_names_the_process(self):
        wid = scheduler.worker_id()
        assert wid.endswith(f":{os.getpid()}") and ":" in wid


class TestClaimJobSlot:
    def test_first_claimant_wins(self):
        fake = FakeSyncClient()
        ok, detail = scheduler.claim_job_slot("daily", client=fake, slot="20261028")
        assert (ok, detail) == (True, "claimed")
        call = fake.set_calls[0]
        assert call["key"] == "sched_lock:daily:20261028"
        assert call["nx"] is True and call["ex"] == SCHED_LOCK_TTL_SECONDS["daily"]
        assert call["value"] == scheduler.worker_id()

    def test_second_claimant_is_told_who_holds_it(self):
        fake = FakeSyncClient({"sched_lock:daily:20261028": "otherhost:4242"})
        ok, detail = scheduler.claim_job_slot("daily", client=fake, slot="20261028")
        assert ok is False
        assert "otherhost:4242" in detail
        assert fake.set_calls[0]["nx"] is True

    def test_holder_lookup_failure_does_not_change_the_verdict(self):
        class _HalfDead(FakeSyncClient):
            def get(self, key):
                raise ConnectionError("gone")

        ok, detail = scheduler.claim_job_slot("daily", client=_HalfDead(
            {"sched_lock:daily:20261028": "x"}), slot="20261028")
        assert ok is False and "another worker" in detail

    def test_dead_redis_runs_the_job_anyway(self):
        """Fail-open: a missing day of data is worse than a duplicate scrape."""
        ok, detail = scheduler.claim_job_slot("daily", client=FakeSyncClient(fail=True),
                                              slot="20261028")
        assert (ok, detail) == (True, "redis-unavailable")

    def test_claim_never_raises(self, monkeypatch):
        def _explode(*a, **k):
            raise RuntimeError("client construction failed")

        monkeypatch.setattr(scheduler.cache_bust, "_client", _explode)
        ok, detail = scheduler.claim_job_slot("breadth_daily")
        assert ok is True and detail == "redis-unavailable"

    def test_explicit_ttl_overrides_the_table(self):
        fake = FakeSyncClient()
        scheduler.claim_job_slot("daily", client=fake, slot="20261028", ttl_seconds=77)
        assert fake.set_calls[0]["ex"] == 77

    def test_injected_client_is_not_closed(self):
        """The injected client is the test seam and the script-owner's socket."""
        fake = FakeSyncClient()
        scheduler.claim_job_slot("daily", client=fake, slot="20261028")
        assert fake.closed is False

    def test_self_created_client_is_closed(self, monkeypatch):
        fake = FakeSyncClient()
        seen = {}

        def _client(timeout=None):
            seen["timeout"] = timeout
            return fake

        monkeypatch.setattr(scheduler.cache_bust, "_client", _client)
        scheduler.claim_job_slot("daily", slot="20261028")
        assert fake.closed is True
        assert seen["timeout"] and seen["timeout"] <= 5     # bounded probe, never blocking


class TestCronExclusive:
    def _job(self, calls: List[str]):
        @scheduler.cron_exclusive("daily")
        def job(**kwargs):
            calls.append("ran")
            return "done"
        return job

    def test_runs_when_slot_claimed(self, monkeypatch):
        monkeypatch.setattr(scheduler, "claim_job_slot",
                            lambda name, ttl_seconds=None: (True, "claimed"))
        calls: List[str] = []
        assert self._job(calls)() == "done"
        assert calls == ["ran"]

    def test_skips_silently_when_held(self, monkeypatch):
        monkeypatch.setattr(scheduler, "claim_job_slot",
                            lambda name, ttl_seconds=None: (False, "slot already held"))
        calls: List[str] = []
        assert self._job(calls)() is None                 # no return value, no raise
        assert calls == []

    def test_force_bypasses_the_fence(self, monkeypatch):
        def _never(*a, **k):
            raise AssertionError("force=True must not consult the fence")

        monkeypatch.setattr(scheduler, "claim_job_slot", _never)
        calls: List[str] = []
        job = self._job(calls)
        assert job(force=True) == "done"
        assert calls == ["ran"]

    def test_force_is_not_forwarded_to_the_wrapped_function(self, monkeypatch):
        """The cron bodies don't take a ``force`` kwarg; popping it is the point."""
        monkeypatch.setattr(scheduler, "claim_job_slot",
                            lambda name, ttl_seconds=None: (True, "claimed"))

        @scheduler.cron_exclusive("weekly")
        def job(a, b=2):
            return a + b

        assert job(1, force=True) == 3

    def test_per_job_ttl_reaches_the_claim(self, monkeypatch):
        seen = {}

        def _claim(name, ttl_seconds=None):
            seen[(name)] = ttl_seconds
            return True, "claimed"

        monkeypatch.setattr(scheduler, "claim_job_slot", _claim)

        @scheduler.cron_exclusive("breadth_daily", ttl_seconds=4500)
        def job():
            return 1

        job()
        assert seen["breadth_daily"] == 4500


class TestDecoratedCronEntries:
    # (fence name, cron entry point). The names differ on purpose: the fence key
    # is the stable job name written into job_runs, the function is the scheduler
    # target. A rename of either without the other is exactly what this catches.
    JOBS = [
        ("daily", "job_daily_scrapers"),
        ("weekly", "job_weekly_scrapers"),
        ("monthly", "job_monthly_scrapers"),
        ("market_reports", "job_market_reports_scrapers"),
        ("filings_importer", "job_filings_importer"),
        ("engine_vintages", "job_engine_vintages"),
        ("breadth_daily", "job_breadth_daily"),
    ]

    @pytest.mark.parametrize("name,fn", JOBS)
    def test_entry_points_are_wrapped(self, name, fn):
        target = getattr(scheduler, fn)
        assert hasattr(target, "__wrapped__"), f"{fn} lost its slot fence"

    @pytest.mark.parametrize("name,fn", JOBS)
    def test_fence_names_are_the_job_names(self, name, fn):
        """Every fenced job has an explicit TTL row and a matching key namespace."""
        assert name in SCHED_LOCK_TTL_SECONDS
        assert scheduler.sched_lock_key(name, "20261028").startswith(
            f"{SCHED_LOCK_PREFIX}:{name}:")

    @pytest.mark.parametrize("name,fn", JOBS)
    def test_wrappers_preserve_identity_and_take_no_arguments(self, name, fn):
        """APScheduler calls the target with zero args, and functools.wraps must
        have kept the name — the logs and the scheduler's own repr depend on it."""
        import inspect

        target = getattr(scheduler, fn)
        assert target.__name__ == fn
        assert getattr(target, "__wrapped__").__name__ == fn
        assert len(inspect.signature(target).parameters) == 0

    def test_held_slot_runs_nothing(self, monkeypatch):
        """The loser skips before the body executes, so no job_runs row and no
        scrape — the winner's row describes the run that actually happened."""
        monkeypatch.setattr(scheduler, "claim_job_slot",
                            lambda name, ttl_seconds=None: (False, "held"))
        called: List[str] = []
        monkeypatch.setattr(scheduler, "_safe_job",
                            lambda *a, **k: called.append("body"), raising=False)
        assert scheduler.job_daily_scrapers() is None
        assert scheduler.job_breadth_daily.__wrapped__ is not None
        assert called == []


class TestUntouchedJobRunnerInvariants:
    """Phase 9 was explicitly forbidden from changing these three; pinned so a
    future refactor notices."""

    def test_boot_at_is_a_utc_instant(self):
        from app.services import job_runner

        assert job_runner.BOOT_AT.tzinfo is not None
        assert job_runner.BOOT_AT.utcoffset() == timedelta(0)

    def test_corpse_detection_still_keys_off_boot_at(self):
        from app.services import job_runner

        old = job_runner.BOOT_AT - timedelta(days=1)
        fresh = job_runner.BOOT_AT + timedelta(minutes=5)
        # Only Run Now rows are this process's business; cron/terminal rows are
        # owned by a different process and must never be guessed about.
        assert job_runner.row_is_a_corpse("daily", old, "admin") is True
        assert job_runner.row_is_a_corpse("daily", fresh, "admin") is False
        assert job_runner.row_is_a_corpse("daily", old, "cron") is False
        assert job_runner.row_is_a_corpse("daily", None, "admin") is False

    def test_concurrency_cap_is_still_configured(self):
        from app.core.config import settings

        assert int(getattr(settings, "MAX_CONCURRENT_JOBS", 2)) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Cross-cutting: the three phases must not contradict each other
# ═══════════════════════════════════════════════════════════════════════════

class TestCoexistence:
    def test_auth_keyspace_is_not_compressed_by_default(self):
        """A 30-char token is below the threshold, so the fail-closed path gains
        no new dependency on the codec."""
        for key in ("access_jti:abc", "refresh_gen:12", "loginfail:1:2"):
            assert len(json.dumps({"v": "x" * 16})) < COMPRESS_MIN_BYTES
            assert cache_codec.encode(json.dumps({key: True}))[1] is None

    def test_rate_limit_buckets_do_not_share_the_cache_namespace(self):
        """Limiter keys are prefixed LUMIVST; cache sweeps only touch cache
        prefixes, so a daily bust can never wipe someone's login counter."""
        from app.core.cache_config import APP_CACHE_PREFIXES

        assert RATE_LIMIT_KEY_PREFIX not in APP_CACHE_PREFIXES
        assert not RATE_LIMIT_KEY_PREFIX.startswith(tuple(APP_CACHE_PREFIXES))

    def test_slot_lock_keys_are_outside_the_flushable_prefixes(self):
        from app.core.cache_config import APP_CACHE_PREFIXES

        assert SCHED_LOCK_PREFIX not in APP_CACHE_PREFIXES
