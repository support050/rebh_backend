"""
REBH Khurafshi Engine Service (Façade & Unified Entrypoint)

This module acts as the backward-compatible interface and façade for:
1. app.services.khurafshi_core_service (Valuation models, Porter, R, 9-Box, Molodovsky, P/S ladder)
2. app.services.market_universe_service (Market-wide universe scan, unit normalization, TTM & Watchlist data)

All existing imports from `app.services.khurafshi_engine_service` continue to work seamlessly.
"""

# Re-export Khurafshi Core Valuation Engine
from app.services.khurafshi_core_service import (
    get_company_sukuk_yield,
    calculate_porter_compensation,
    calculate_safety_compensation,
    calculate_build_up_r,
    calculate_nine_box_matrix,
    calculate_cyclical_bands,
    calculate_loss_ps_ladder,
)

# Re-export Market Universe & Watchlist Service
from app.services.market_universe_service import (
    _parse_period_end_date,
    _detect_period_months,
    _compute_growth_rates,
    _compute_piotroski_f_score,
    _grade_metric,
    _get_latest_prices_map,
    get_khurafshi_live_market_stats,
    get_khurafshi_universe_data,
    build_universe_snapshot,
    get_universe_snapshot,
    get_rebh_peers,
    _UNIVERSE_CACHE,
    _PRICES_MAP_CACHE,
    _STATS_CACHE,
)

__all__ = [
    # Core Valuation
    "get_company_sukuk_yield",
    "calculate_porter_compensation",
    "calculate_safety_compensation",
    "calculate_build_up_r",
    "calculate_nine_box_matrix",
    "calculate_cyclical_bands",
    "calculate_loss_ps_ladder",
    # Market Universe & Watchlist
    "_parse_period_end_date",
    "_detect_period_months",
    "_compute_growth_rates",
    "_compute_piotroski_f_score",
    "_grade_metric",
    "_get_latest_prices_map",
    "get_khurafshi_live_market_stats",
    "get_khurafshi_universe_data",
    "build_universe_snapshot",
    "get_universe_snapshot",
    "get_rebh_peers",
]
