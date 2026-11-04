"""Part 2 of the cache overhaul — stampede protection, zero-gap invalidation,
cache-penetration guards.

Covers:
  Phase 3  single-flight inside cache_read_through (SET NX EX + Lua release),
           TTL jitter as a floor-only extension, stale-while-revalidate
  Phase 4  refresh_namespace (warm then batched eviction), the auth-keyspace
           refusals, cache_bust's skip_prefixes, the warm→bust job wiring
  Phase 5  negative caching, key hygiene, the cache_guard parameter gates

Everything runs against the fake client below: no test in this file may contact
a real Redis, so an autouse fixture makes ``init_redis`` permanently fail. The
deployed cache is reachable from this laptop (backend/.env points at it), and a
test suite that could delete keys there is worse than no test suite.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.core import cache_bust, cache_guard, cache_helpers
from app.core.cache_config import (
    APP_CACHE_PREFIXES,
    AUTH_KEY_PREFIXES,
    CACHE_KEY_MAX_LENGTH,
    CACHE_TTL_SCREENERS,
    DEFAULT_CACHE_TTL,
    EMPTY_SENTINEL_KEY,
    INVALIDATE_BATCH_SIZE,
    LOCK_KEY_PREFIX,
    LOCK_TTL_SECONDS,
    MAX_DATE_RANGE_DAYS,
    MAX_QUERY_LIMIT,
    NEGATIVE_CACHE_TTL,
    TTL_JITTER_RATIO,
    TTL_JITTER_THRESHOLD_SECONDS,
)
from app.core.redis import RedisCache

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES = REPO_ROOT / "Rebh_Market_Breadth_Package" / "api_samples"


# ═══════════════════════════════════════════════════════════════════════════
# Test doubles
# ═══════════════════════════════════════════════════════════════════════════

class FakeClient:
    """Async redis-client stand-in for the DATA path.

    Speaks exactly the commands ``RedisCache`` uses on the cache side: SET
    (with NX/EX), GET, EXISTS, TTL, UNLINK, SCAN (cursorless), EVAL. Recording
    every call is the point — most assertions here are about *which* commands
    were sent, in what order, and which keys were never touched.
    """

    def __init__(self, *, store: Optional[Dict[str, Any]] = None,
                 scan_keys: Optional[List[str]] = None, reachable: bool = True,
                 ttl_value: int = -2):
        self.store: Dict[str, Any] = dict(store or {})
        self.ttls: Dict[str, int] = {}
        # ``scan_keys`` is a convenience seed for "these keys exist". Scanning
        # then walks the LIVE store, like Redis does: a key that has just been
        # unlinked must stop being reported, which is the whole basis of
        # refresh_namespace's verification pass. A static key list here would
        # fake that detail away and make the residual assertion meaningless.
        for key in (scan_keys or []):
            self.store.setdefault(str(key), "v")
        self.scan_sequence: List[List[str]] = []   # successive scans pop this
        self.reachable = reachable
        self.ttl_value = ttl_value
        self.closed = False
        self.set_calls: List[dict] = []
        self.get_calls: List[str] = []
        self.exists_calls: List[str] = []
        self.ttl_calls: List[str] = []
        self.eval_calls: List[dict] = []
        self.unlink_batches: List[List[str]] = []
        self.unlinked: List[str] = []

    @property
    def unlink_calls(self) -> int:
        return len(self.unlink_batches)

    async def ping(self):
        if not self.reachable:
            raise ConnectionError("fake: redis is down")
        return True

    async def aclose(self):
        self.closed = True

    async def set(self, key, value, ex=None, nx=False, **kwargs):
        key = str(key)
        self.set_calls.append({"key": key, "ex": ex, "nx": nx})
        if nx and key in self.store:
            return None                      # someone else already holds it
        self.store[key] = value
        if ex:
            self.ttls[key] = ex
        return True

    async def get(self, key):
        key = str(key)
        self.get_calls.append(key)
        return self.store.get(key)

    async def exists(self, key):
        key = str(key)
        self.exists_calls.append(key)
        return 1 if key in self.store else 0

    async def ttl(self, key):
        self.ttl_calls.append(str(key))
        return self.ttl_value

    async def unlink(self, *keys):
        batch = [str(k) for k in keys]
        self.unlink_batches.append(batch)
        self.unlinked.extend(batch)
        for k in batch:
            self.store.pop(k, None)
            self.ttls.pop(k, None)
        return len(batch)

    def scan_iter(self, match=None, **kwargs):
        pattern = match if match is not None else "*"
        if self.scan_sequence:
            pool = self.scan_sequence.pop(0)
        else:
            pool = list(self.store.keys())
        wanted = [k for k in pool if fnmatch.fnmatchcase(str(k), pattern)]

        async def _gen():
            for k in wanted:
                yield k
        return _gen()

    async def eval(self, body, numkeys, *args):
        keys = [str(a) for a in args[:numkeys]]
        argv = [str(a) for a in args[numkeys:]]
        self.eval_calls.append({"body": body, "keys": keys, "argv": argv})
        if "GET" in body and "DEL" in body:      # LUA_RELEASE_LOCK
            key = keys[0]
            if self.store.get(key) == argv[0]:
                self.store.pop(key)
                return 1
            return 0
        raise AssertionError(f"unexpected Lua body in tests: {body[:60]!r}")


def wired(fake: FakeClient) -> RedisCache:
    """A RedisCache bound to a fake — never initialised, so never connected."""
    cache = RedisCache()
    cache.redis_client = fake
    cache.is_connected = True
    cache.auth_client = None
    return cache


def run(coro):
    return asyncio.run(coro)


# cache_guard only consults the DB catalog when it is handed a session, so the
# tests pass a sentinel: _query_catalog is patched and never touches it.
DB = object()


@pytest.fixture(autouse=True)
def no_real_redis(monkeypatch):
    """Hard guarantee: nothing in this file reaches a socket.

    ``ensure_connection`` falls through to ``init_redis`` whenever a ping fails,
    and backend/.env carries a reachable production URL. Returning False makes
    an unavailable Redis behave exactly like the documented fail-open data path
    instead of silently testing against the deployed instance.
    """
    async def _never(self, *args, **kwargs):    # noqa: ANN001 - instance method
        return False

    monkeypatch.setattr(RedisCache, "init_redis", _never)
    cache_guard.reset_symbol_catalog()
    yield


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3 — single-flight
# ═══════════════════════════════════════════════════════════════════════════

class TestSingleFlight:
    KEY = "prices:history:symbol:1010:limit:500"

    def test_two_concurrent_readers_run_one_query(self):
        fake = FakeClient()
        cache = wired(fake)
        calls: List[int] = []

        async def heavy():
            calls.append(1)
            await asyncio.sleep(0.15)          # the DB time the herd used to duplicate
            return {"rows": 42}

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                return await asyncio.gather(
                    cache_helpers.cache_read_through(self.KEY, 60, heavy),
                    cache_helpers.cache_read_through(self.KEY, 60, heavy),
                )

        a, b = run(main())
        assert len(calls) == 1                 # THE stampede assertion
        assert a == b == {"rows": 42}

    def test_fifty_readers_still_run_one_query(self):
        fake = FakeClient()
        cache = wired(fake)
        calls: List[int] = []

        async def heavy():
            calls.append(1)
            await asyncio.sleep(0.1)
            return {"rows": 1}

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                return await asyncio.gather(*[
                    cache_helpers.cache_read_through(self.KEY, 60, heavy)
                    for _ in range(50)
                ])

        results = run(main())
        assert len(calls) == 1
        assert all(r == {"rows": 1} for r in results)

    def test_lock_is_set_nx_ex_and_released_with_lua(self):
        fake = FakeClient()
        cache = wired(fake)

        async def heavy():
            return {"rows": 1}

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                return await cache_helpers.cache_read_through(self.KEY, 60, heavy)

        run(main())
        lock = f"{LOCK_KEY_PREFIX}{self.KEY}"
        nx_calls = [c for c in fake.set_calls if c["nx"]]
        assert nx_calls == [{"key": lock, "ex": LOCK_TTL_SECONDS, "nx": True}]
        assert LOCK_TTL_SECONDS == 60
        # Released by the compare-and-delete script, naming exactly this lock.
        assert fake.eval_calls and fake.eval_calls[-1]["keys"] == [lock]
        assert lock not in fake.store
        # And the value was written BEFORE the lock was released, so a waiter can
        # never observe "lock free, value absent" and stampede alongside it.
        order = [c["key"] for c in fake.set_calls]
        assert order.index(self.KEY) < len(order) - 1 or nx_calls[0]["key"] == order[0]
        assert fake.eval_calls[-1]["keys"][0] == lock

    def test_a_waiter_that_times_out_cooks_rather_than_fails(self, monkeypatch):
        """No failed request, ever: the fallback is doing the old work again."""
        monkeypatch.setattr(cache_helpers, "LOCK_POLL_MAX_SECONDS", 0.1)
        monkeypatch.setattr(cache_helpers, "LOCK_POLL_INTERVAL_SECONDS", 0.02)
        lock = f"{LOCK_KEY_PREFIX}{self.KEY}"
        fake = FakeClient(store={lock: "somebody-elses-token"})
        cache = wired(fake)
        calls: List[int] = []

        async def heavy():
            calls.append(1)
            return {"rows": 7}

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                return await cache_helpers.cache_read_through(self.KEY, 60, heavy)

        started = time.monotonic()
        assert run(main()) == {"rows": 7}
        assert calls == [1]
        assert time.monotonic() - started < 2.0
        # A lock we do not own must survive us: deleting it would let a third
        # reader assume the winner had finished.
        assert lock in fake.store

    def test_release_lock_only_deletes_its_own_token(self):
        fake = FakeClient(store={f"{LOCK_KEY_PREFIX}k": "mine"})
        cache = wired(fake)

        async def main():
            stolen = await cache.release_lock(f"{LOCK_KEY_PREFIX}k", "not-mine")
            held = await cache.release_lock(f"{LOCK_KEY_PREFIX}k", "mine")
            return stolen, held

        stolen, held = run(main())
        assert stolen is False
        assert held is True
        assert f"{LOCK_KEY_PREFIX}k" not in fake.store

    def test_dead_redis_skips_the_lock_and_never_stalls(self):
        """Fail-open means fail-OPEN: no waiting, no 500, no lock traffic."""
        fake = FakeClient(reachable=False)
        cache = wired(fake)
        calls: List[int] = []

        async def heavy():
            calls.append(1)
            return {"rows": 3}

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                started = time.monotonic()
                value = await cache_helpers.cache_read_through(self.KEY, 60, heavy)
                return value, time.monotonic() - started

        value, elapsed = run(main())
        assert value == {"rows": 3}
        assert calls == [1]
        assert elapsed < 1.0                   # not LOCK_POLL_MAX_SECONDS
        assert fake.set_calls == []            # no SET, no SET NX on a dead client

    def test_fetch_errors_still_propagate(self):
        """The lock must not swallow a real failure into a cache miss retry."""
        fake = FakeClient()
        cache = wired(fake)

        async def boom():
            raise RuntimeError("query exploded")

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                with pytest.raises(RuntimeError, match="query exploded"):
                    await cache_helpers.cache_read_through(self.KEY, 60, boom)
                # ...and the lock is still released, so the next request is not
                # parked for LOCK_TTL_SECONDS behind a dead worker.
                return f"{LOCK_KEY_PREFIX}{self.KEY}" in fake.store

        assert run(main()) is False


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3 — TTL jitter (floor-only) and stale-while-revalidate
# ═══════════════════════════════════════════════════════════════════════════

class TestTTLJitter:
    def test_short_ttls_are_untouched(self):
        for ttl in (1, 60, 3599, TTL_JITTER_THRESHOLD_SECONDS - 1):
            assert cache_helpers.apply_ttl_jitter(ttl) == ttl

    def test_long_ttls_move_upward_only(self):
        base = TTL_JITTER_THRESHOLD_SECONDS
        samples = [cache_helpers.apply_ttl_jitter(base) for _ in range(400)]
        assert min(samples) >= base                      # never shortened
        assert max(samples) <= base + int(base * TTL_JITTER_RATIO)
        assert len(set(samples)) > 1                     # actually spread

    def test_a_three_day_ttl_stays_a_three_day_floor(self):
        got = cache_helpers.apply_ttl_jitter(CACHE_TTL_SCREENERS)
        assert CACHE_TTL_SCREENERS <= got <= CACHE_TTL_SCREENERS * 1.1

    def test_store_ttl_adds_the_stale_window_then_jitters(self):
        got = cache_helpers._store_ttl(3600, 120)
        assert got >= 3720        # fresh boundary (hard - stale) is still 3600+

    def test_read_through_writes_a_jittered_expiry(self):
        fake = FakeClient()
        cache = wired(fake)

        async def heavy():
            return {"rows": 1}

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                await cache_helpers.cache_read_through("rs:latest:x", 60, heavy)
                await cache_helpers.cache_read_through("rs:latest:y", 60, heavy)
                await cache_helpers.cache_read_through("rs:latest:z", 60, heavy)

        run(main())
        # 60 s is under the threshold: exact, unchanged (the strict no-TTL rule).
        expiries = [c["ex"] for c in fake.set_calls if not c["nx"]]
        assert expiries == [60, 60, 60]


class TestStaleWhileRevalidate:
    KEY = "rs:latest:min_rs:none:limit:100"

    def _hit(self, fake, cache, ttl_value, stale_ttl, calls):
        async def heavy():
            calls.append(1)
            return {"rows": len(calls)}

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                first = await cache_helpers.cache_read_through(self.KEY, 60, heavy)
                fake.ttl_value = ttl_value
                second = await cache_helpers.cache_read_through(
                    self.KEY, 60, heavy, stale_ttl=stale_ttl)
                await asyncio.sleep(0.2)      # let the background task land
                return first, second

        return run(main()), calls

    def test_default_off_is_byte_compatible(self):
        """stale_ttl=None must not even look at the TTL — the old behaviour."""
        fake = FakeClient()
        cache = wired(fake)
        calls: List[int] = []
        (first, second), calls = self._hit(fake, cache, 5, None, calls)
        assert first == second == {"rows": 1}
        assert fake.ttl_calls == []

    def test_stale_window_serves_old_data_and_refills_in_flight(self):
        fake = FakeClient()
        cache = wired(fake)
        calls: List[int] = []
        (first, second), calls = self._hit(fake, cache, 5, 60, calls)
        assert first == {"rows": 1}
        assert second == {"rows": 1}           # the reader got the stale payload
        assert len(calls) == 2                 # ...and a refresh happened
        assert json.loads(fake.store[self.KEY]) == {"rows": 2}

    def test_a_fresh_hit_does_not_requery(self):
        fake = FakeClient()
        cache = wired(fake)
        calls: List[int] = []
        (_first, second), calls = self._hit(fake, cache, 3600, 60, calls)
        assert second == {"rows": 1}
        assert len(calls) == 1

    def test_missing_ttl_answer_is_treated_as_fresh(self):
        """-2 (key gone) / None (Redis down) must not trigger a thundering refill."""
        for remaining in (-2, -1, None):
            fake = FakeClient()
            cache = wired(fake)
            calls: List[int] = []
            (_f, _s), calls = self._hit(fake, cache, remaining, 60, calls)
            assert len(calls) == 1, remaining


# ═══════════════════════════════════════════════════════════════════════════
# Phase 5 — negative caching
# ═══════════════════════════════════════════════════════════════════════════

class TestNegativeCaching:
    KEY = "prices:history:symbol:9999:limit:500"

    def test_empty_list_is_stored_and_served_without_the_db(self):
        fake = FakeClient()
        cache = wired(fake)
        calls: List[int] = []

        async def heavy():
            calls.append(1)
            return []

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                a = await cache_helpers.cache_read_through(self.KEY, 60, heavy,
                                                           cache_empty=True)
                b = await cache_helpers.cache_read_through(self.KEY, 60, heavy,
                                                           cache_empty=True)
                return a, b

        a, b = run(main())
        assert a == b == []                    # shape preserved, no surprise dict
        assert len(calls) == 1                 # the penetration assertion
        stored = json.loads(fake.store[self.KEY])
        assert stored == {EMPTY_SENTINEL_KEY: []}
        assert fake.ttls[self.KEY] == NEGATIVE_CACHE_TTL

    @pytest.mark.parametrize("empty", [None, {}, []])
    def test_every_empty_shape_round_trips_to_itself(self, empty):
        fake = FakeClient()
        cache = wired(fake)

        async def heavy():
            return empty

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                await cache_helpers.cache_read_through("rs:x", 60, heavy,
                                                       cache_empty=True)
                return await cache_helpers.cache_read_through("rs:x", 60, heavy,
                                                              cache_empty=True)

        got = run(main())
        assert got == empty and type(got) is type(empty)

    def test_opt_out_leaves_the_pre_part_2_behaviour_intact(self):
        """cache_empty defaults False: [] is still cached and served as it always
        was (the strict no-behaviour-change rule for existing routes)."""
        fake = FakeClient()
        cache = wired(fake)
        calls: List[int] = []

        async def heavy():
            calls.append(1)
            return []

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                a = await cache_helpers.cache_read_through("rs:y", 60, heavy)
                b = await cache_helpers.cache_read_through("rs:y", 60, heavy)
                return a, b

        a, b = run(main())
        assert a == b == []
        assert len(calls) == 1
        assert json.loads(fake.store["rs:y"]) == []      # bare, no sentinel
        assert fake.ttls["rs:y"] == 60

    def test_none_is_the_hole_negative_caching_closes(self):
        """A stored None reads back as "absent", so it was re-fetched forever.

        This is the actual penetration path: an endpoint that answers None for an
        unknown key hit the database on every single request while believing it
        had a cache. cache_empty=True turns it into one query per 60 seconds.
        """
        without = FakeClient()
        calls: List[int] = []

        async def heavy():
            calls.append(1)
            return None

        async def main_opt_out():
            with patch.object(cache_helpers, "redis_cache", wired(without)):
                for _ in range(3):
                    assert await cache_helpers.cache_read_through("rs:n", 60, heavy) is None

        run(main_opt_out())
        assert len(calls) == 3                   # the hole, as it shipped
        assert "rs:n" in without.store           # stored, but unreadable as a hit

        calls.clear()
        negative = FakeClient()

        async def main_opt_in():
            with patch.object(cache_helpers, "redis_cache", wired(negative)):
                first = await cache_helpers.cache_read_through("rs:n", 60, heavy,
                                                               cache_empty=True)
                second = await cache_helpers.cache_read_through("rs:n", 60, heavy,
                                                                cache_empty=True)
                return first, second

        assert run(main_opt_in()) == (None, None)
        assert len(calls) == 1                   # ...now it is one query
        assert negative.ttls["rs:n"] == NEGATIVE_CACHE_TTL

    def test_a_non_empty_result_is_not_sentinel_wrapped(self):
        fake = FakeClient()
        cache = wired(fake)

        async def heavy():
            return {"rows": 1}

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                return await cache_helpers.cache_read_through("rs:z", 60, heavy,
                                                              cache_empty=True)

        run(main())
        assert json.loads(fake.store["rs:z"]) == {"rows": 1}
        assert EMPTY_SENTINEL_KEY not in json.loads(fake.store["rs:z"])


# ═══════════════════════════════════════════════════════════════════════════
# Phase 5 — key hygiene (fail-open at write time)
# ═══════════════════════════════════════════════════════════════════════════

class TestKeyHygiene:
    def test_each_rejection_reason(self):
        cases = {
            "": "empty",
            "x" * (CACHE_KEY_MAX_LENGTH + 1): "too_long",
            "prices:history:*": "glob_character",
            "prices:history:?": "glob_character",
            "prices:history:[1]": "glob_character",
            "prices:history:10\n10": "control_character",
            "access_token:abc": "auth_keyspace",
            "prices:history:10\x10": "control_character",
            "prices:histo\u00e9ry": "unsupported_character",
        }
        for key, reason in cases.items():
            assert cache_helpers.cache_key_problem(key) == reason, repr(key)

    def test_clean_keys_are_accepted(self):
        for key in (
            "rs:latest:min_rs:none:limit:100",
            "economic:sp500:500:None:None",
            "screener:trend-1-month:date:latest:limit:100:offset:0",
            "rebh:breadth:series:a,b:2026-01-01:-",
            "rebh:breadth:stocks:Software & Services",
            "rebh:breadth:stocks:Real Estate Mgmt & Dev't",
            "rebh:breadth:stocks:البنوك",
            "terminal:company:1010",
        ):
            assert cache_helpers.cache_key_problem(key) is None, key

    def test_a_dirty_key_is_never_written_but_the_request_still_works(self):
        fake = FakeClient()
        cache = wired(fake)
        calls: List[int] = []
        dirty = "prices:history:symbol:" + "*" + ":limit:500"

        async def heavy():
            calls.append(1)
            return {"rows": 1}

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                first = await cache_helpers.cache_read_through(dirty, 60, heavy)
                second = await cache_helpers.cache_read_through(dirty, 60, heavy)
                return first, second

        first, second = run(main())
        assert (first, second) == ({"rows": 1}, {"rows": 1})
        assert len(calls) == 2                 # served from the DB, not cached
        assert fake.set_calls == []            # and nothing was written
        assert dirty not in fake.store

    def test_the_industry_groups_that_really_exist_all_pass(self):
        """Evidence, not taste: the breadth /stocks route interpolates a Tadawul
        group name straight into its cache key. If the charset refused a space or
        an apostrophe, every legitimate group page would silently stop caching."""
        path = SAMPLES / "stocks.json"
        if not path.exists():
            pytest.skip("breadth api_samples are not present in this checkout")
        groups = list(json.loads(path.read_text(encoding="utf-8"))["stocks"])
        assert len(groups) == 21
        offenders = [g for g in groups
                     if cache_helpers.cache_key_problem(f"rebh:breadth:stocks:{g}")
                     is not None]
        assert offenders == []


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4 — batched eviction
# ═══════════════════════════════════════════════════════════════════════════

class TestBatchedUnlink:
    def test_batch_size_and_pause_are_bounded(self):
        # The brief asks for 200-500 keys per round trip.
        assert 200 <= INVALIDATE_BATCH_SIZE <= 500

    def test_large_sweep_is_split_into_batches(self):
        keys = [f"prices:history:symbol:{i}" for i in range(700)]
        fake = FakeClient()
        cache = wired(fake)

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                return await cache_helpers._batched_unlink(
                    keys, batch_size=300, pause=0.0, client=cache)

        assert run(main()) == 700
        assert [len(b) for b in fake.unlink_batches] == [300, 300, 100]

    def test_an_empty_sweep_sends_no_command(self):
        fake = FakeClient()
        cache = wired(fake)

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                return await cache_helpers._batched_unlink([], pause=0.0, client=cache)

        assert run(main()) == 0
        assert fake.unlink_batches == []

    def test_invalidate_patterns_batches_and_still_refuses_auth_keys(self):
        keys = [f"prices:latest:{i}" for i in range(50)] + ["refresh_token:jti-1"]
        fake = FakeClient(scan_keys=keys)
        cache = wired(fake)

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                await cache_helpers.invalidate_patterns(["prices:*"])

        run(main())
        assert "refresh_token:jti-1" not in fake.unlinked
        assert len(fake.unlinked) == 50
        assert all(len(b) <= INVALIDATE_BATCH_SIZE for b in fake.unlink_batches)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4 — refresh_namespace
# ═══════════════════════════════════════════════════════════════════════════

class TestRefreshNamespace:
    def test_warms_the_hot_keys_then_evicts_only_the_rest(self):
        hot = ["terminal:market_machine", "terminal:company:1010"]
        cold = [f"terminal:quant:x{i}" for i in range(5)]
        auth = ["session_index:7:a", "access_token:abc"]
        fake = FakeClient(scan_keys=hot + cold + auth)
        cache = wired(fake)

        def warm_fn():
            return [(k, {"built": k}, CACHE_TTL_SCREENERS) for k in hot]

        report = run(cache_helpers.refresh_namespace(
            "terminal", warm_fn, cache=cache, patterns=["terminal:*"]))
        assert report["ok"] is True
        assert report["warmed"] == 2
        assert report["evicted"] == 5
        assert report["kept_warm"] == 2
        # Zero, and that is the point: a scoped sweep physically cannot see an
        # auth key. The per-key filter underneath is the second layer, for the
        # callers (invalidate_patterns) that do accept a broad pattern.
        assert report["skipped_auth_keys"] == 0
        assert report["residual"] == 0
        assert report["error"] is None
        # The hot keys were overwritten, not deleted: never a gap for a reader.
        for k in hot:
            assert k in fake.store
            assert fake.ttls[k] >= CACHE_TTL_SCREENERS     # jitter only lengthens
        assert cold[0] not in fake.store
        assert set(fake.unlinked) == set(cold)

    def test_auth_keys_are_never_deleted_by_a_warm_up(self):
        """Phase 4's hard requirement, through the door that can actually be reached.

        Two layers protect the keyspace and both are asserted: refresh_namespace
        refuses any pattern broad enough to reach an auth key, and a namespace
        aimed straight at an auth stem is refused as well.
        """
        auth = [f"{p}do-not-delete" for p in AUTH_KEY_PREFIXES]
        fake = FakeClient(store={k: "v" for k in auth})
        fake.store["rebh:breadth:today"] = "v"
        cache = wired(fake)

        def warm_fn():
            return [("rebh:breadth:today", {"today": 1})]

        refused = run(cache_helpers.refresh_namespace("rebh", warm_fn, cache=cache,
                                                      patterns=["*"]))
        assert refused["ok"] is False and "unscoped" in refused["error"]
        assert fake.unlink_batches == []

        # Layer two: a namespace aimed at the auth keys themselves.
        for prefix in AUTH_KEY_PREFIXES:
            aimed = run(cache_helpers.refresh_namespace(
                prefix.rstrip(":"), warm_fn, cache=cache,
                patterns=[f"{prefix}*"]))
            assert aimed["ok"] is False and "auth keyspace" in aimed["error"]
            assert fake.unlink_batches == []
        for key in auth:
            assert key in fake.store

    def test_an_unscoped_pattern_is_refused_before_anything_happens(self):
        fake = FakeClient(scan_keys=["terminal:quant:x", "refresh_token:j"])
        cache = wired(fake)
        touched: List[int] = []

        def warm_fn():
            touched.append(1)
            return []

        report = run(cache_helpers.refresh_namespace("terminal", warm_fn,
                                                    cache=cache, patterns=["*"]))
        assert report["ok"] is False
        assert "unscoped" in (report["error"] or "")
        assert touched == []                   # not even the warm step ran
        assert fake.set_calls == []
        assert fake.unlink_batches == []

    @pytest.mark.parametrize("prefix", AUTH_KEY_PREFIXES)
    def test_an_auth_namespace_cannot_be_refreshed(self, prefix):
        fake = FakeClient(scan_keys=[f"{prefix}session"])
        cache = wired(fake)

        def warm_fn():
            return [(f"{prefix}session", {"x": 1})]

        report = run(cache_helpers.refresh_namespace(prefix, warm_fn, cache=cache))
        assert report["ok"] is False
        assert "auth keyspace" in (report["error"] or "")
        assert fake.unlink_batches == []

    def test_a_failing_warm_fn_still_evicts_the_stale_keys(self):
        """Stale data must not survive because the optimisation broke."""
        fake = FakeClient(scan_keys=["terminal:quant:old"])
        cache = wired(fake)

        def warm_fn():
            raise RuntimeError("service is down")

        report = run(cache_helpers.refresh_namespace("terminal", warm_fn,
                                                     cache=cache,
                                                     patterns=["terminal:*"]))
        assert report["ok"] is True
        assert "warm_fn failed" in (report["note"] or "")
        assert report["evicted"] == 1
        assert "terminal:quant:old" in fake.unlinked

    def test_one_slow_symbol_does_not_abandon_the_others(self):
        fake = FakeClient(scan_keys=[])
        cache = wired(fake)

        def slow():
            time.sleep(1.0)
            return {"slow": True}

        def warm_fn():
            return [
                ("terminal:company:4001", slow),
                ("terminal:market_machine", lambda: {"fast": True}),
            ]

        report = run(cache_helpers.refresh_namespace(
            "terminal", warm_fn, cache=cache, patterns=["terminal:*"],
            per_key_timeout=0.05))
        assert report["warm_failed"] == 1
        assert report["warmed"] == 1
        assert "terminal:market_machine" in fake.store

    def test_a_hot_key_that_produces_nothing_is_not_counted_as_warm(self):
        fake = FakeClient(scan_keys=[])
        cache = wired(fake)

        def warm_fn():
            return [
                ("terminal:market_machine", lambda: None),      # supplier failed
                ("terminal:quant_lab", {"ok": True}),
                ("not-a-pair",),                                 # wrong shape
            ]

        report = run(cache_helpers.refresh_namespace("terminal", warm_fn,
                                                     cache=cache,
                                                     patterns=["terminal:*"]))
        assert report["warmed"] == 1
        assert report["warm_failed"] == 2
        assert "terminal:quant_lab" in fake.store

    def test_the_wall_clock_budget_stops_a_runaway_pass(self, monkeypatch):
        monkeypatch.setattr(cache_helpers, "time", cache_helpers.time)
        fake = FakeClient(scan_keys=[])
        cache = wired(fake)

        def warm_fn():
            return [(f"terminal:k{i}", {"i": i}) for i in range(20)]

        clock = iter([0.0, 0.0, 999.0] + [999.0] * 50)
        monkeypatch.setattr(cache_helpers.time, "monotonic", lambda: next(clock))
        report = run(cache_helpers.refresh_namespace(
            "terminal", warm_fn, cache=cache, patterns=["terminal:*"],
            budget_seconds=0.001))
        assert "budget exceeded" in (report["note"] or "")
        assert report["warmed"] < 20

    def test_a_dead_cache_is_not_reported_as_a_clean_sweep(self):
        """The lie this guard exists for: scan returns [] when Redis is down, which
      looks identical to "nothing left" and would license the bust to skip."""
        fake = FakeClient(reachable=False)
        cache = wired(fake)
        cache.is_connected = False

        def warm_fn():
            return [("terminal:market_machine", {"x": 1})]

        report = run(cache_helpers.refresh_namespace("terminal", warm_fn,
                                                    cache=cache,
                                                    patterns=["terminal:*"]))
        assert report["ok"] is False
        assert "sweep probe failed" in (report["error"] or "")
        assert report["residual"] == 0

    def test_residual_keys_left_by_a_reader_are_counted(self):
        fake = FakeClient(scan_keys=["terminal:quant_lab"])
        cache = wired(fake)
        # First scan: everything. Second (verification) scan: a reader repopulated.
        fake.scan_sequence = [["terminal:quant_lab"],
                              ["terminal:quant_lab", "terminal:company:1010"]]

        def warm_fn():
            return [("terminal:market_machine", {"x": 1})]

        report = run(cache_helpers.refresh_namespace("terminal", warm_fn,
                                                    cache=cache,
                                                    patterns=["terminal:*"]))
        assert report["ok"] is True
        # Both keys came back on the verification scan, so neither is provably
        # gone: the honest answer is "the regular bust must still run".
        assert report["residual"] == 2         # not provably swept

    def test_uses_the_injected_client_never_the_app_singleton(self):
        """A job thread has its own event loop; the module pool belongs to another."""
        mine = FakeClient(scan_keys=["terminal:quant:old"])
        theirs = FakeClient(scan_keys=["terminal:quant:old"])
        singleton = wired(theirs)

        def warm_fn():
            return [("terminal:market_machine", {"x": 1})]

        with patch.object(cache_helpers, "redis_cache", singleton):
            run(cache_helpers.refresh_namespace("terminal", warm_fn,
                                                cache=wired(mine),
                                                patterns=["terminal:*"]))
        assert theirs.set_calls == []
        assert theirs.unlink_batches == []
        assert mine.unlink_batches


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4 — the bust must not undo the warm-up
# ═══════════════════════════════════════════════════════════════════════════

class TestBustSkipPrefixes:
    def test_stem_boundary_is_respected(self):
        assert cache_bust.is_skipped_key("terminal:quant_lab", ["terminal"])
        assert cache_bust.is_skipped_key("terminal:quant_lab", ["terminal:*"])
        assert cache_bust.is_skipped_key("terminal:quant_lab", ["terminal:"])
        # The dangerous one: a prefix-string match would drop a whole namespace.
        assert not cache_bust.is_skipped_key("terminalia:x", ["terminal"])
        assert not cache_bust.is_skipped_key("rs:latest:1", ["terminal"])
        assert not cache_bust.is_skipped_key("terminalx:1", ["terminal"])

    def test_evict_keeps_the_warm_keys_and_reports_it(self):
        fake = cache_bust_Fake(keys=[
            "terminal:market_machine", "terminal:company:1010",
            "terminal:quant:stale", "rs:latest:1",
        ])
        with patch.object(cache_bust, "_client", return_value=fake):
            report = cache_bust.evict(["terminal:*", "rs:*"],
                                      skip_prefixes=["terminal:market_machine",
                                                     "terminal:company:1010"])
        assert report["skipped_warm_keys"] == 2
        assert report["deleted"] == 2
        assert "terminal:quant:stale" in fake.unlinked
        assert "terminal:market_machine" not in fake.unlinked

    def test_skip_prefixes_never_override_the_auth_refusal(self):
        fake = cache_bust_Fake(keys=["session_index:9:a"])
        with patch.object(cache_bust, "_client", return_value=fake):
            report = cache_bust.evict(["*"], skip_prefixes=[])
        assert report["deleted"] == 0
        assert report["skipped_auth_keys"] == 1

    def test_warm_before_bust_only_protects_a_verified_namespace(self, monkeypatch):
        calls: List[dict] = []

        async def fake_refresh(prefix, warm_fn, **kwargs):
            calls.append({"prefix": prefix})
            return {"ok": True, "warmed": 3, "warm_failed": 0, "kept_warm": 3,
                    "skipped_auth_keys": 0, "scan_failed": 0, "residual": 0,
                    "evicted": 10, "error": None, "note": None,
                    "elapsed_ms": 5}

        monkeypatch.setattr(cache_helpers, "refresh_namespace", fake_refresh)
        monkeypatch.setattr(cache_guard, "_read_csv_symbols", lambda *a: set())
        from app.services import cache_warmers

        plan = [("terminal", lambda: [], ["terminal:*"])]
        monkeypatch.setattr(cache_warmers, "warm_plan_for_job", lambda job: plan)
        assert cache_warmers.warm_before_bust("daily_market_update") == ["terminal"]

    def test_warm_before_bust_refuses_to_protect_a_partial_sweep(self, monkeypatch):
        async def partial(prefix, warm_fn, **kwargs):
            return {"ok": True, "warmed": 1, "warm_failed": 0, "kept_warm": 1,
                    "skipped_auth_keys": 0, "scan_failed": 0, "residual": 7,
                    "evicted": 1, "error": None, "note": None, "elapsed_ms": 1}

        monkeypatch.setattr(cache_helpers, "refresh_namespace", partial)
        from app.services import cache_warmers
        monkeypatch.setattr(
            cache_warmers, "warm_plan_for_job",
            lambda job: [("terminal", lambda: [], ["terminal:*"])])
        # residual > 0 → nothing may be skipped by the regular bust
        assert cache_warmers.warm_before_bust("daily_market_update") == []

    def test_bust_job_caches_passes_the_protected_stems_to_the_bust(self, monkeypatch):
        from app.services import cache_warmers
        seen: List[dict] = []

        def fake_bust(job=None, skip_prefixes=None, **kwargs):
            seen.append({"job": job, "skip": list(skip_prefixes or ())})
            return {"deleted": 0}

        monkeypatch.setattr(cache_warmers, "warm_before_bust", lambda job: ["terminal"])
        monkeypatch.setattr(cache_bust, "bust", fake_bust)
        cache_warmers.bust_job_caches("daily_market_update", local=True, remote=False)
        assert seen == [{"job": "daily_market_update", "skip": ["terminal"]}]

    def test_a_job_plan_follows_the_bust_table(self):
        """Warm-up and eviction must never drift apart (the Phase 4 bug class)."""
        from app.services import cache_warmers
        plan = dict((stem, patterns)
                    for stem, _fn, patterns in cache_warmers.warm_plan_for_job(
                        "daily_market_update"))
        assert "terminal:*" in plan["terminal"]
        assert cache_warmers.namespaces_for_job("daily_market_update")
        assert cache_warmers.warmers_for_prefix("rebh") == [("rebh:engine",
                                                             cache_warmers.warm_rebh_engine)]
        # breadth is deliberately NOT warmed (see the module docstring)
        assert "rebh:breadth" not in cache_warmers.WARMERS


def cache_bust_Fake(keys):
    """A sync client for cache_bust, which does its own SCAN cursor walk."""

    class _F:
        def __init__(self, keys):
            self._all = list(keys)
            self.unlinked: List[str] = []
            self.scans = 0

        def ping(self):
            return True

        def scan(self, cursor=0, match=None, count=None):
            self.scans += 1
            return 0, [k for k in self._all
                       if fnmatch.fnmatchcase(k, match or "*")]

        def unlink(self, *batch):
            self.unlinked.extend(batch)
            return len(batch)

        def close(self):
            pass

    return _F(keys)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 5 — the parameter gate (app/core/cache_guard.py)
# ═══════════════════════════════════════════════════════════════════════════

class TestCacheGuard:
    @pytest.fixture
    def catalog(self, monkeypatch):
        """A controlled catalog: DB source patched, CSV suppressed, no sockets."""
        monkeypatch.setattr(cache_guard, "_query_catalog",
                            lambda db: {"1010", "4001", "ALBABTAIN", "1256"})
        monkeypatch.setattr(cache_guard, "_read_csv_symbols", lambda *a: set())
        cache_guard.reset_symbol_catalog()
        yield {"1010", "4001", "ALBABTAIN", "1256"}
        cache_guard.reset_symbol_catalog()

    def test_known_symbol_passes_through_unchanged(self, catalog):
        # Unchanged matters: normalising would alter a case-sensitive WHERE clause.
        assert cache_guard.guard_symbol(DB, "1010") == "1010"
        assert cache_guard.guard_symbol(DB, "albabtain") == "albabtain"

    def test_zero_padded_and_case_variants_are_accepted(self, catalog):
        assert cache_guard.is_known_symbol(DB, "01010") is True
        assert cache_guard.is_known_symbol(DB, "1010") is True
        assert cache_guard.is_known_symbol(DB, "4001") is True
        assert cache_guard.is_known_symbol(DB, "9999") is False

    def test_unknown_symbol_is_404_not_500(self, catalog):
        with pytest.raises(HTTPException) as exc:
            cache_guard.guard_symbol(DB, "99999")
        assert exc.value.status_code == 404

    def test_blank_symbol_is_422(self, catalog):
        for blank in ("", "   ", None):
            with pytest.raises(HTTPException) as exc:
                cache_guard.guard_symbol(DB, blank)
            assert exc.value.status_code == 422, repr(blank)

    def test_an_unavailable_catalog_fails_open(self, monkeypatch):
        """A broken lookup must never block a real ticker."""
        monkeypatch.setattr(cache_guard, "_query_catalog", lambda db: set())
        monkeypatch.setattr(cache_guard, "_read_csv_symbols", lambda *a: set())
        cache_guard.reset_symbol_catalog()
        assert cache_guard.is_known_symbol(DB, "anything-at-all") is True
        assert cache_guard.guard_symbol(DB, "9999") == "9999"
        assert cache_guard.catalog_size() == 0

    def test_the_shipped_csv_alone_is_a_usable_catalog(self, monkeypatch):
        monkeypatch.setattr(cache_guard, "_query_catalog", lambda db: set())
        cache_guard.reset_symbol_catalog()
        assert cache_guard.known_symbols(None)          # CSV present → non-empty
        assert cache_guard.is_known_symbol(None, "4327") is True

    def test_pagination_ceiling(self):
        cache_guard.guard_pagination(limit=100, offset=0)      # fine
        with pytest.raises(HTTPException) as exc:
            cache_guard.guard_pagination(limit=MAX_QUERY_LIMIT + 1)
        assert exc.value.status_code == 422
        with pytest.raises(HTTPException):
            cache_guard.guard_pagination(limit=-1)
        with pytest.raises(HTTPException):
            cache_guard.guard_pagination(offset=-5)

    def test_the_real_page_sizes_stay_inside_the_ceiling(self):
        """Backward compatibility: the live frontend asks for 25 000 rows."""
        for limit in (500, 10000, 25000, 50000):
            cache_guard.guard_pagination(limit=limit)

    def test_date_range_guards(self):
        cache_guard.guard_date_range("2026-01-01", "2026-02-01")
        cache_guard.guard_date_range(None, None)
        cache_guard.guard_date_range("2026-01-01", None)
        with pytest.raises(HTTPException) as exc:
            cache_guard.guard_date_range("2026-02-01", "2026-01-01")
        assert exc.value.status_code == 422
        with pytest.raises(HTTPException):
            cache_guard.guard_date_range("1900-01-01", "2100-01-01")
        assert MAX_DATE_RANGE_DAYS == 36600

    def test_cache_key_guard_rejects_before_the_db(self):
        with pytest.raises(HTTPException) as exc:
            cache_guard.guard_cache_key("prices:history:symbol:*:limit:50")
        assert exc.value.status_code == 422
        assert "glob_character" in str(exc.value.detail)
        with pytest.raises(HTTPException) as long_exc:
            cache_guard.guard_cache_key("prices:" + "y" * 400)
        assert long_exc.value.status_code == 422
        assert cache_guard.guard_cache_key("prices:history:symbol:1010:limit:500")

    def test_guard_query_orders_the_cheap_checks_first(self, catalog):
        assert cache_guard.guard_query(limit=10, symbol="1010", db=DB) == "1010"
        with pytest.raises(HTTPException):        # rejected without a catalog lookup
            cache_guard.guard_query(limit=MAX_QUERY_LIMIT + 1, symbol="99999", db=DB)


# ═══════════════════════════════════════════════════════════════════════════
# The auth keyspace, as a property rather than an example
# ═══════════════════════════════════════════════════════════════════════════

class TestAuthKeyspaceIsNeverDeletable:
    """No cache helper may delete a key under any AUTH_KEY_PREFIXES stem.

    Parametrised over every prefix and every deleting entry point, then repeated
    against randomly assembled keyspace — the one test the brief asks for, and
    the invariant whose failure mode ("a cache clear logs everyone out") is the
    most expensive in this subsystem.
    """

    DELETING_ENTRY_POINTS = ("invalidate_patterns", "refresh_namespace",
                             "cache_bust_evict", "redis_flush_all")

    @pytest.mark.parametrize("prefix", AUTH_KEY_PREFIXES)
    def test_invalidate_patterns_spares_every_auth_prefix(self, prefix):
        auth_key = f"{prefix}jti-abc"
        fake = FakeClient(scan_keys=[auth_key, "rs:latest:1"])
        cache = wired(fake)

        async def main():
            with patch.object(cache_helpers, "redis_cache", cache):
                await cache_helpers.invalidate_patterns(["*"])

        run(main())
        assert fake.unlinked == ["rs:latest:1"]
        assert auth_key in fake.store

    @pytest.mark.parametrize("prefix", AUTH_KEY_PREFIXES)
    def test_refresh_namespace_spares_every_auth_prefix(self, prefix):
        auth_key = f"{prefix}jti-abc"
        fake = FakeClient(scan_keys=[auth_key, "terminal:quant:stale"])
        cache = wired(fake)

        def warm_fn():
            return [("terminal:market_machine", {"x": 1})]

        report = run(cache_helpers.refresh_namespace(
            "terminal", warm_fn, cache=cache,
            patterns=["terminal:*", "*"]))          # the wildcard is refused
        assert report["ok"] is False and "unscoped" in report["error"]
        assert fake.unlink_batches == []
        assert auth_key in fake.store

    @pytest.mark.parametrize("prefix", AUTH_KEY_PREFIXES)
    def test_cache_bust_evict_spares_every_auth_prefix(self, prefix):
        fake = cache_bust_Fake(keys=[f"{prefix}jti-abc", "prices:latest:1"])
        with patch.object(cache_bust, "_client", return_value=fake):
            report = cache_bust.evict(["*"])
        assert report["deleted"] == 1
        assert report["skipped_auth_keys"] == 1
        assert fake.unlinked == ["prices:latest:1"]

    @pytest.mark.parametrize("prefix", AUTH_KEY_PREFIXES)
    def test_redis_flush_all_spares_every_auth_prefix(self, prefix):
        fake = FakeClient(store={f"{prefix}jti-abc": "v", "rs:latest:1": "v"},
                          scan_keys=[f"{prefix}jti-abc", "rs:latest:1"])
        cache = wired(fake)
        run(cache.flush_all())
        assert f"{prefix}jti-abc" in fake.store

    def test_randomised_keyspace_keeps_every_auth_key(self):
        """A property test with a fixed seed: 40 random mixes, four sweeps."""
        rng = random.Random(20261104)
        app_prefixes = list(APP_CACHE_PREFIXES)
        for _attempt in range(40):
            keyspace = []
            for _ in range(rng.randint(3, 12)):
                if rng.getrandbits(1):
                    stem = rng.choice(AUTH_KEY_PREFIXES)
                    suffix = rng.choice(["", ":", "-"]) and rng.choice(["jti", "s-1", "u:9"])
                    keyspace.append(f"{stem}{suffix}{rng.randint(1, 999)}")
                else:
                    keyspace.append(f"{rng.choice(app_prefixes)}:{rng.randint(1, 999)}")
            auth_before = [k for k in keyspace if any(k.startswith(p) for p in AUTH_KEY_PREFIXES)]
            fake = FakeClient(store={k: "v" for k in keyspace}, scan_keys=list(keyspace))
            cache = wired(fake)

            async def main():
                with patch.object(cache_helpers, "redis_cache", cache):
                    await cache_helpers.invalidate_patterns(["*", "access_token:*"])
                    await cache_helpers.refresh_namespace(
                        "rs", lambda: [("rs:hot", {"x": 1})],
                        cache=cache, patterns=["rs:*"])
                    await cache.flush_all()

            run(main())
            survivors = set(fake.store)
            for key in auth_before:
                assert key in survivors, (key, fake.unlinked)
