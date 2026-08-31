"""
Cache Configuration & TTL Constants
Single source of truth for all cache TTLs
"""

# ============================================================================
# TTL VALUES (in seconds) - Production Configuration
# ============================================================================

# ── 1. Daily Market Caches (24 Hours TTL / Invalidate after market close) ──
# RS Latest / RS V2 Latest / Screeners / Technical Screener
CACHE_TTL_SCREENERS = 86400  # 24 hours

# Prices Latest
CACHE_TTL_PRICES_LATEST = 86400  # 24 hours

# Prices History by Symbol
CACHE_TTL_PRICES_HISTORY = 86400  # 24 hours

# RS History and RS V2 History
CACHE_TTL_RS_HISTORY = 86400  # 24 hours

# Industry Groups Latest
CACHE_TTL_INDUSTRY_GROUPS_LATEST = 86400  # 24 hours

# Industry Groups Stocks
CACHE_TTL_INDUSTRY_GROUPS_STOCKS = 86400  # 24 hours

# RS V2 Stats / RS V2 Industries / RS V2 Top-Movers
CACHE_TTL_RS_V2_STATS = 86400  # 24 hours

# Terminal Daily Market Aggregates (Market Machine, Quant Lab, All Ratios)
CACHE_TTL_TERMINAL_DAILY = 86400  # 24 hours (86,400 seconds)

# ── 2. Long-Term Financial Statement Caches (Quarterly / 90 Days) ─────────
# Invalidate automatically when a new XBRL file is ingested/parsed
CACHE_TTL_TERMINAL_QUARTERLY = 7776000  # 90 days (90 * 24 * 3600 seconds)


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
