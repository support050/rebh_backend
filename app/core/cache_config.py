"""
Cache Configuration & TTL Constants
Single source of truth for all cache TTLs, key prefixes, and the eviction
groups the jobs/scripts use to bust the cache (see app/core/cache_bust.py).

Pure constants only — no settings/pydantic import — so the root-venv workers
(breadth_job.py) can read this file without pulling in the web app's deps.
"""

from typing import Tuple


# ============================================================================
# TTL VALUES (in seconds) - Production Configuration
# ============================================================================

# ── 1. Daily Market Caches (3 Days TTL / Explicitly invalidated after each ──
# daily run — the TTL is a safety net bridging weekends, holidays and late jobs)
# RS Latest / RS V2 Latest / Screeners / Technical Screener
CACHE_TTL_SCREENERS = 259200  # 3 days

# Prices Latest
CACHE_TTL_PRICES_LATEST = 259200  # 3 days

# Prices History by Symbol
CACHE_TTL_PRICES_HISTORY = 259200  # 3 days

# RS History and RS V2 History
CACHE_TTL_RS_HISTORY = 259200  # 3 days

# Industry Groups Latest
CACHE_TTL_INDUSTRY_GROUPS_LATEST = 259200  # 3 days

# Industry Groups Stocks
CACHE_TTL_INDUSTRY_GROUPS_STOCKS = 259200  # 3 days

# RS V2 Stats / RS V2 Industries / RS V2 Top-Movers
CACHE_TTL_RS_V2_STATS = 259200  # 3 days

# Terminal Daily Market Aggregates (Market Machine, Quant Lab, All Ratios)
CACHE_TTL_TERMINAL_DAILY = 259200  # 3 days

# REBH Engine per-company contract (heavy XBRL + engine computation)
# No intraday feeds exist; the daily job busts the keyspace on completion.
CACHE_TTL_REBH_ENGINE = 259200  # 3 days — heavy computation, daily invalidation

# ── 2. Long-Term Financial Statement Caches (Quarterly / 90 Days) ─────────
# Invalidate automatically when a new XBRL file is ingested/parsed
CACHE_TTL_TERMINAL_QUARTERLY = 7776000  # 90 days (90  * 24 * 3600 seconds)

# ── 3. Module-level Distributed (Redis) Cache TTLs ─────────
# REBH Market Breadth snapshot API (rebh:breadth:*)
# Safety net only — the nightly breadth job clears the keyspace explicitly.
CACHE_TTL_BREADTH = 259200  # 3 days — EOD-computed, job invalidates on write

# Legacy market breadth endpoints (A/D rating, breadth dashboards)
CACHE_TTL_LEGACY_BREADTH = 259200  # 3 days

# Minervini historical trend series (key screener:historical:trend_v5)
CACHE_TTL_HISTORICAL_TREND = 259200  # 3 days — data updates once daily

# Historical screener fallback payload
CACHE_TTL_HISTORICAL_SCREENER = 3600  # 1 hour

# Market reports aggregations
CACHE_TTL_MARKET_REPORTS = 259200  # 3 days — weekly reports, published daily

# RS Hub rs_data.json downloaded from R2
CACHE_TTL_RS_HUB_R2 = 86400  # 24 hours — file published once daily

# Economic indicators (SP500 / yield curve scrapings)
CACHE_TTL_ECONOMIC_INDICATORS = 3600  # 1 hour

# Scraped per-company financials
CACHE_TTL_FINANCIALS = 604800  # 7 days (7 * 86400)

# ============================================================================
# In-Memory / Process-Level Cache TTLs (seconds)
# ============================================================================

# Admin system map snapshot rebuild (app/api/routes/system_map.py)
IN_MEMORY_TTL_SYSTEM_MAP = 60  # 1 minute — frontend polls with the same interval

# Market universe stats aggregation (services/market_universe_service.py)
IN_MEMORY_TTL_UNIVERSE_STATS = 300  # 5 minutes

# Market universe company list (services/market_universe_service.py)
IN_MEMORY_TTL_UNIVERSE_LIST = 300  # 5 minutes

# Market universe snapshot rows (services/market_universe_service.py)
IN_MEMORY_TTL_UNIVERSE_SNAPSHOT = 120  # 2 minutes — rows are light, cheap to reload


# ============================================================================
# Cache Key Prefixes (Namespace Organization)
# ============================================================================

PREFIX_RS = "rs"
PREFIX_RS_V2 = "rsv2"
PREFIX_SCREENER = "screener"
PREFIX_TECHNICAL_SCREENER = "technical"
PREFIX_PRICES = "prices"
PREFIX_INDUSTRY_GROUPS = "industry:groups"
PREFIX_TERMINAL = "terminal"
PREFIX_REBH = "rebh"
# Keyspaces that sit OUTSIDE the 8-namespace daily flush and need their own
# explicit invalidation hook (see scheduler / export_rs_hub_data / job_runner):
PREFIX_MARKET_REPORTS = "market_reports"
PREFIX_RS_HUB = "rs_hub"
PREFIX_ECONOMIC = "economic"
PREFIX_SCRAPER = "scraper"


# ============================================================================
# Eviction Groups (what a finished job busts — see cache_bust.py)
# ============================================================================
# The 8 namespaces the daily market pipeline rewrites. This list used to live
# copy-pasted inside scripts/daily_market_update.py step 10 and
# scripts/run_analytics_pipeline.py:flush_redis_cache — two hand-maintained
# copies that already drifted once (one of them added market_reports/rs_hub).
DAILY_MARKET_PREFIXES: Tuple[str, ...] = (
    PREFIX_RS,
    PREFIX_RS_V2,
    PREFIX_SCREENER,
    PREFIX_TECHNICAL_SCREENER,
    PREFIX_PRICES,
    PREFIX_INDUSTRY_GROUPS,
    PREFIX_TERMINAL,
    PREFIX_REBH,
)

# Every prefix the APPLICATION caches. Deliberately not "*": a wildcard sweep
# is what forced a hand-maintained auth exclusion list, and it also deletes
# rate-limit buckets that belong to no cache namespace at all.
APP_CACHE_PREFIXES: Tuple[str, ...] = DAILY_MARKET_PREFIXES + (
    PREFIX_MARKET_REPORTS,
    PREFIX_RS_HUB,
    PREFIX_ECONOMIC,
    PREFIX_SCRAPER,
)

# Narrow patterns for a job that touches only one slice of a namespace.
PATTERN_BREADTH_SNAPSHOT = f"{PREFIX_REBH}:breadth:*"


# ============================================================================
# Keys That Are NOT A Cache (sessions, tokens, OAuth state)
# ============================================================================
# One list for the whole codebase: redis.RedisCache refuses to evict them,
# cache_bust skips them even when a caller passes a wildcard, and the admin
# /api/cache endpoints re-check them before UNLINK. Adding a new auth key
# prefix without updating THIS list is how a cache clear logs everyone out.
AUTH_KEY_PREFIXES: Tuple[str, ...] = (
    "access_token:",
    "access_jti:",
    "refresh_token:",
    "refresh_jti:",
    "refresh_gen:",
    "refresh_used:",
    "session_index:",
    "oauth_state:",
    "oauth_link:",
    "verify_token:",
    "reset_token:",
    "loginfail:",
)


# ============================================================================
# Redis Availability Guardrails (NOT TTLs — no cache TTL lives here)
# ============================================================================
# Safety net for keys written without an explicit TTL. RedisCache.set() has
# always defaulted to 86400s; naming it here keeps the fallback auditable
# instead of buried in a signature. Writing a cache key with NO expiry is a
# permanent memory leak (it survives every bust), so the default is applied
# and a warning is logged rather than silently storing an immortal key.
DEFAULT_CACHE_TTL = 86400  # 24 hours — fallback only, never a configured TTL

# Circuit breaker for the DATA path only. A dead Redis used to cost every
# single request its full socket_connect_timeout (5s) before falling open —
# 5s of dead time per page, forever. After this many consecutive failures the
# breaker opens and data ops return "cache miss" instantly for RESET_SECONDS;
# one probe then decides (half-open). The AUTH path never consults the breaker:
# security-critical reads must keep trying and keep failing closed.
REDIS_BREAKER_FAILURE_THRESHOLD = 3
REDIS_BREAKER_RESET_SECONDS = 30

# Periodic cache-stats log throttle (Phase 2 observability). In-process
# counters only — no StatsD/Prometheus dependency added.
CACHE_STATS_LOG_INTERVAL_SECONDS = 300

# Redis command compatibility floor (Phase 1). These are the server versions
# where the native commands appeared; the code deliberately does NOT use them
# because the deployed Redis version is not pinned anywhere — the Lua
# equivalents in app/core/redis.py work on Redis 2.6+.
REDIS_MIN_VERSION_GETDEL = "6.2"        # GETDEL
REDIS_MIN_VERSION_EXPIRE_NX = "7.0"     # EXPIRE ... NX
REDIS_MIN_VERSION_UNLINK = "4.0"        # UNLINK (still used: fail-soft + DELETE-free)


# ============================================================================
# Stampede Protection (Part 2 / Phase 3) — single-flight + TTL jitter
# ============================================================================
# One expired hot key + 50 concurrent readers used to mean 50 identical heavy
# queries at once (exactly what a daily job leaves behind: the whole namespace
# is gone and the first page load pays for every key). Only the caller that
# wins the lock computes; the others poll for the winner's value and serve it.
LOCK_KEY_PREFIX = "lock:"               # lock:<cache key> — never a cache key itself
LOCK_TTL_SECONDS = 60                   # a crashed worker must not block the key for long
LOCK_POLL_INTERVAL_SECONDS = 0.05       # 50 ms
LOCK_POLL_MAX_SECONDS = 5.0             # then cook anyway: waiting must not fail the request

# Thundering herd on expiry: every key of a namespace written in the same
# second dies in the same second. Large TTLs only (>= 1 h) get a random 0-10 %
# extension. The configured TTL is a FLOOR and is never reduced — this is the
# only place the expiry may move, and only upward.
TTL_JITTER_THRESHOLD_SECONDS = 3600
TTL_JITTER_RATIO = 0.10


# ============================================================================
# Zero-Gap Invalidation & Warm-up (Part 2 / Phase 4)
# ============================================================================
# One UNLINK round trip per this many keys, with a short await between batches
# so a 100k-key sweep cannot occupy the server (and the event loop) for seconds.
INVALIDATE_BATCH_SIZE = 300
INVALIDATE_BATCH_PAUSE_SECONDS = 0.05

# Warm-up runs after a job, before the rest of the namespace is evicted, so the
# hot pages are never simultaneously absent. Bounded two ways: how many
# per-symbol payloads to precompute, and a wall-clock budget for the whole pass
# (a warm-up that runs long is skipped, never allowed to delay the eviction).
WARM_UP_TOP_SYMBOLS = 5
WARM_UP_BUDGET_SECONDS = 120.0
WARM_UP_PER_KEY_TIMEOUT_SECONDS = 60.0


# ============================================================================
# Cache Penetration Protection (Part 2 / Phase 5)
# ============================================================================
# The penetration hole in its exact shape: RedisCache.get() cannot tell a stored
# None from an absent key, so an endpoint that legitimately answers None re-ran
# its query on EVERY request while believing it had a cache. Opt-in negative
# caching stores a sentinel under the SAME key, so "nothing here" becomes an
# answer. For [] and {} the flag additionally shortens the expiry from the route's
# full TTL to this one, which is also why 60 s and not something longer: an empty
# answer must become a real one as soon as the data lands.
NEGATIVE_CACHE_TTL = 60
EMPTY_SENTINEL_KEY = "__cache_empty__"   # dunder-prefixed: no route payload uses it

# Key hygiene. A cache key built from user parameters is an injection surface:
# an attacker who can write an arbitrary stem can mint unbounded keyspace, and
# a key containing "*"/"["/"?" poisons every glob built on top of it.
CACHE_KEY_MAX_LENGTH = 200
# Letters/digits of any script plus the punctuation the existing key generators
# actually emit (rs:latest:min_rs:none:limit:100, economic:sp500:500:None:None,
# rebh:breadth:series:a,b:2026-01-01:-, screener:trend-1-month:...). Any glob
# or control character is refused.
#
# SPACE and APOSTROPHE are allowed on purpose and on evidence: the breadth
# /stocks route interpolates a Tadawul industry-group name straight into its key,
# and 15 of the 21 published groups contain a space ("Software & Services",
# "Pharma &  Biotech & Life Science") while one contains an apostrophe ("Real
# Estate Mgmt & Dev't"). A charset without them would have silently refused to
# cache every legitimate group page — a guard that blocks real traffic is worse
# than the hole it closes. Redis keys are binary-safe; what actually poisons a
# namespace is a glob or control character, and both stay refused.
CACHE_KEY_ALLOWED_PATTERN = r"^[A-Za-z0-9_:.,\-/&=+~@()'\u0600-\u06FF ]*$"
CACHE_KEY_GLOB_CHARS = "*?[]{}"

# Parameter ceilings for endpoints that build a key from a query string.
MAX_QUERY_LIMIT = 50000     # matches the pre-existing /prices/history le=50000
MAX_QUERY_OFFSET = 1000000
MAX_DATE_RANGE_DAYS = 36600  # 100 years: anything wider is a typo or a probe

# In-process TTL of the known-symbol catalog (a handful of index-only scans).
IN_MEMORY_TTL_KNOWN_SYMBOLS = 3600


# ============================================================================
# Payload Compression (Part 3 / Phase 6)
# ============================================================================
# Compression is a CPU trade, not a free win: one gzip pass over a 10 MB payload
# costs real milliseconds on a 512 MB Render instance. Below this size the trade
# is bad and the value is stored as plain JSON, exactly as before — so the
# threshold is the ONLY switch, and small payloads are untouched by the feature.
COMPRESS_MIN_BYTES = 256 * 1024     # 256 KB serialised JSON
# gzip level 6: the knee of the size/CPU curve. Levels 7-9 add seconds for a few
# percent on JSON, which is already highly compressible.
COMPRESS_GZIP_LEVEL = 6
# zstd is used only if the optional `zstandard` package is importable; absent it,
# gzip carries the load (see app/core/cache_codec.py). Level 3 is zstd's default
# and its own knee.
COMPRESS_ZSTD_LEVEL = 3


# ============================================================================
# Distributed Rate Limiting (Part 3 / Phase 8)
# ============================================================================
# Storage selection happens once at import (app/core/limiter.py). The probe is a
# TCP connect with this timeout — deliberately short: it runs during application
# startup, so a Redis that answers slowly must not add seconds to boot. A failed
# probe only means the limits start per-process; slowapi's own in-memory
# fallback still handles a Redis that dies later.
RATE_LIMIT_PROBE_TIMEOUT_SECONDS = 0.5
# Redis storage timeouts. Without them a hung Redis costs the requester the
# kernel's full connect timeout; with them the request pays once, slowly, and
# slowapi then switches this process to the in-memory buckets.
RATE_LIMIT_SOCKET_TIMEOUT_SECONDS = 1.0
RATE_LIMIT_SOCKET_CONNECT_TIMEOUT_SECONDS = 1.0
# Prefix for the buckets slowapi writes. NOT a cache namespace and NOT an auth
# prefix: rate-limit keys must survive a cache bust, which is why the bust never
# sweeps "*" (see APP_CACHE_PREFIXES above) instead of excluding them per key.
RATE_LIMIT_KEY_PREFIX = "LUMIVST"


# ============================================================================
# Background Job Mutual Exclusion (Part 3 / Phase 9)
# ============================================================================
# APScheduler starts inside every process that has ENABLE_SCHEDULER=true, so N
# web replicas / N workers fire the same cron entry N times: N identical scrapes
# at the same source, and N writers against the same tables. One SET NX per slot
# per job makes the first claimant the only runner.
SCHED_LOCK_PREFIX = "sched_lock"          # sched_lock:<job>:<slot>
SCHED_LOCK_DEFAULT_TTL_SECONDS = 4 * 3600  # must outlive the job, not just its start
# A slot is a DAY (Africa/Cairo), not the cron minute: two workers whose clocks
# or fire times straddle a minute boundary would otherwise compute different keys
# and both run. Every job below is scheduled at most once per day, so the day is
# the smallest slot that still means "this scheduled occurrence".
SCHED_LOCK_SLOT_FORMAT = "%Y%m%d"
# Per-job ceilings: a lock that expires while its job is still running lets a
# second worker start a duplicate, so each TTL is sized above that job's own
# worst-case runtime, not above its average one.
SCHED_LOCK_TTL_SECONDS: dict = {
    # breadth_daily shells out to breadth_job.py, whose subprocess ceiling is
    # BREADTH_TIMEOUT_SECONDS (3600 s) in app/core/scheduler.py. +15 min margin.
    "breadth_daily": 4500,
    "daily": 4 * 3600,
    "weekly": 4 * 3600,
    "monthly": 4 * 3600,
    "market_reports": 2 * 3600,
    "filings_importer": 2 * 3600,
    "engine_vintages": 3 * 3600,
}
# The lock probe is a single SET against the same Redis the cache uses; a Redis
# that is not answering must not stop the scrapers (a missing day of data is
# worse than a duplicate scrape), so this is a probe timeout, not a job gate.
SCHED_LOCK_PROBE_TIMEOUT_SECONDS = 2.0


