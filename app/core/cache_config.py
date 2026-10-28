"""
Cache Configuration & TTL Constants
Single source of truth for all cache TTLs
"""

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
