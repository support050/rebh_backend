"""
Breadth Engine Models — dedicated, self-owned files for the REBH Market Breadth system.

14 result tables that mirror the engine's storage/features/*.parquet ONE-TO-ONE
(column names are frozen by the engine output — parity tool depends on them),
plus breadth_runs (daily job log).

Independent from the old `market_breadth` table by design: nothing here reads or
rewrites it. Tables here are written ONLY by app/services/breadth_loader.py and
read ONLY by API routes.
"""

from sqlalchemy import (
    JSON, BigInteger, Boolean, Column, Date, DateTime, Index, Integer,
    Numeric, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.core.database import Base


def _n(name: str) -> Column:
    """Generic numeric column (engine floats: pct, scores, oscillator values)."""
    return Column(name, Numeric(18, 6), nullable=True)


def _big(name: str) -> Column:
    """Wide numeric: counts/volumes/lines — engine stores some as FLOAT or BOOL flags,
    so we must NOT use integer types here (verified against parquet dtypes)."""
    return Column(name, Numeric(20, 6), nullable=True)


def _s(name: str, size: int) -> Column:
    return Column(name, String(size), nullable=True)


class BreadthRun(Base):
    """One row per daily engine job. status = success/failed so a half-run is visible."""

    __tablename__ = "breadth_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asof = Column(Date, nullable=False, index=True)               # last trading date processed
    status = Column(String(16), nullable=False, default="running")  # running/success/failed
    source_mode = Column(String(16), nullable=False, default="db-direct")
    started_at = Column(DateTime, server_default=func.now())
    finished_at = Column(DateTime, nullable=True)
    seconds = Column(Numeric(8, 1), nullable=True)
    rows_prices = Column(Integer, nullable=True)
    rows_index = Column(Integer, nullable=True)
    score = Column(Numeric(6, 2), nullable=True)
    regime = Column(String(16), nullable=True)
    corporate_actions_review = Column(JSON, nullable=True)         # pending CAs the engine flagged
    tables_loaded = Column(JSON, nullable=True)                    # {table: row_count}
    error = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_breadth_runs_asof", "asof"),
    )

    def __repr__(self):
        return f"<BreadthRun(asof={self.asof}, status={self.status})>"


class BreadthParticipation(Base):
    """Family A — % above moving averages + new highs/lows (features/participation.parquet)."""

    __tablename__ = "breadth_participation"

    date = Column(Date, primary_key=True)
    pct_above_ema10 = _n("pct_above_ema10"); n_ema10 = _big("n_ema10")
    pct_above_sma20 = _n("pct_above_sma20"); n_sma20 = _big("n_sma20")
    pct_above_sma50 = _n("pct_above_sma50"); n_sma50 = _big("n_sma50")
    pct_above_sma100 = _n("pct_above_sma100"); n_sma100 = _big("n_sma100")
    pct_above_sma150 = _n("pct_above_sma150"); n_sma150 = _big("n_sma150")
    pct_above_sma200 = _n("pct_above_sma200"); n_sma200 = _big("n_sma200")
    pct_stage2 = _n("pct_stage2"); n_weinstein = _big("n_weinstein"); pct_stage4 = _n("pct_stage4")
    pct_minervini = _n("pct_minervini"); n_minervini = _big("n_minervini")
    new_highs = _big("new_highs"); new_lows = _big("new_lows"); new_highs_vol = _big("new_highs_vol")
    n_nhnl = _big("n_nhnl"); net_nh_nl = Column("net_nh_nl", Numeric(18, 6), nullable=True)
    hl_index = _n("hl_index"); pct_new_highs = _n("pct_new_highs"); pct_new_lows = _n("pct_new_lows")


class BreadthAdvanceDecline(Base):
    """Family B — A/D, McClellan, TRIN (features/advance_decline.parquet)."""

    __tablename__ = "breadth_advance_decline"

    date = Column(Date, primary_key=True)
    advances = _big("advances"); declines = _big("declines"); unchanged = _big("unchanged")
    n_ad = _big("n_ad")
    up_volume = _big("up_volume"); down_volume = _big("down_volume")
    up_points = Column("up_points", Numeric(20, 4), nullable=True)
    down_points = Column("down_points", Numeric(20, 4), nullable=True)
    ad_net = _n("ad_net")
    ad_line = Column("ad_line", Numeric(20, 4), nullable=True)
    ad_ratio = _n("ad_ratio"); ud_volume_ratio = _n("ud_volume_ratio")
    net_up_volume = _big("net_up_volume"); up_volume_pct = _n("up_volume_pct")
    up_points_pct = _n("up_points_pct"); rana = _n("rana")
    mcclellan_osc = _n("mcclellan_osc"); mcclellan_sum = Column("mcclellan_sum", Numeric(20, 4), nullable=True)
    trin = _n("trin"); trin_10 = _n("trin_10"); trin_open10 = _n("trin_open10")
    day90_up = _big("day90_up"); day90_down = _big("day90_down")
    day90_up_vol = _big("day90_up_vol"); day90_down_vol = _big("day90_down_vol")
    day80_up_vol = _big("day80_up_vol"); day80_down_vol = _big("day80_down_vol")


class BreadthMomentum(Base):
    """New-x-day momentum counts (features/momentum.parquet)."""

    __tablename__ = "breadth_momentum"

    date = Column(Date, primary_key=True)
    n_mom = _big("n_mom")
    up_x = _n("up_x"); down_x = _n("down_x")
    ratio_5 = _n("ratio_5"); up_5 = _big("up_5"); down_5 = _big("down_5")
    ratio_10 = _n("ratio_10"); up_10 = _big("up_10"); down_10 = _big("down_10")
    up_25_q = _big("up_25_q"); down_25_q = _big("down_25_q")
    up_25_m = _big("up_25_m"); down_25_m = _big("down_25_m")
    up_50_m = _big("up_50_m"); down_50_m = _big("down_50_m")
    up_13_34 = _big("up_13_34"); down_13_34 = _big("down_13_34")
    pct_up_x = _n("pct_up_x"); pct_down_x = _n("pct_down_x"); threshold = _n("threshold")


class BreadthThrusts(Base):
    # fmt: off
    """Named thrusts (features/thrusts.parquet)."""

    __tablename__ = "breadth_thrusts"

    date = Column(Date, primary_key=True)
    zweig_value = _n("zweig_value"); zweig_state = _s("zweig_state", 32); zweig_signal = _n("zweig_signal")
    whaley_value = _n("whaley_value"); whaley_state = _s("whaley_state", 32); whaley_signal = _n("whaley_signal")
    deemer_value = _n("deemer_value"); deemer_state = _s("deemer_state", 32); deemer_signal = _n("deemer_signal")
    pct50_thrust_value = _n("pct50_thrust_value"); pct50_thrust_state = _s("pct50_thrust_state", 32)
    pct50_thrust_signal = _n("pct50_thrust_signal")
    lowry_up_signal = _n("lowry_up_signal"); lowry_down_signal = _n("lowry_down_signal")
    lowry_down_points = _n("lowry_down_points"); lowry_down_volume = _big("lowry_down_volume")
    lowry_seq_signal = _n("lowry_seq_signal")
    zweig_rare_state = _s("zweig_rare_state", 32); zweig_rare_signal = _n("zweig_rare_signal")
    deemer_rare_state = _s("deemer_rare_state", 32); deemer_rare_signal = _n("deemer_rare_signal")
    # fmt: on


class BreadthClassic(Base):
    """Classic breadth signals (features/classic.parquet)."""

    __tablename__ = "breadth_classic"

    date = Column(Date, primary_key=True)
    hlli = _n("hlli"); abi = _n("abi")
    hindenburg = _n("hindenburg"); hindenburg_confirmed = _n("hindenburg_confirmed")
    hindenburg_signal = _n("hindenburg_signal")
    titanic = _n("titanic"); titanic_signal = _n("titanic_signal")
    sto_b = _n("sto_b")
    itbm = _n("itbm"); itbm_signal = _n("itbm_signal")
    mvo = _n("mvo"); mvo_sum = _n("mvo_sum")
    cvi = Column("cvi", Numeric(20, 4), nullable=True)          # SAR millions (display layer)
    cvi_ma50 = Column("cvi_ma50", Numeric(20, 4), nullable=True)
    zweig_91 = _n("zweig_91"); zweig_double_91 = _n("zweig_double_91")
    zweig_double_91_signal = _n("zweig_double_91_signal")
    nh_pct_10 = _n("nh_pct_10"); haurlan_st = _n("haurlan_st"); ad_pct_21 = _n("ad_pct_21")


class BreadthDivergences(Base):
    """Index vs breadth bear/bull divergences (features/divergences.parquet)."""

    __tablename__ = "breadth_divergences"

    date = Column(Date, primary_key=True)
    index_high = _n("index_high"); index_low = _n("index_low")
    bear_ad_line = _n("bear_ad_line"); bear_ad_line_signal = _n("bear_ad_line_signal")
    bull_ad_line = _n("bull_ad_line"); bull_ad_line_signal = _n("bull_ad_line_signal")
    bear_net_nh_nl_10 = _n("bear_net_nh_nl_10"); bear_net_nh_nl_10_signal = _n("bear_net_nh_nl_10_signal")
    bull_net_nh_nl_10 = _n("bull_net_nh_nl_10"); bull_net_nh_nl_10_signal = _n("bull_net_nh_nl_10_signal")
    bear_pct_above_sma50 = _n("bear_pct_above_sma50"); bear_pct_above_sma50_signal = _n("bear_pct_above_sma50_signal")
    bull_pct_above_sma50 = _n("bull_pct_above_sma50"); bull_pct_above_sma50_signal = _n("bull_pct_above_sma50_signal")
    bear_mcclellan_sum = _n("bear_mcclellan_sum"); bear_mcclellan_sum_signal = _n("bear_mcclellan_sum_signal")
    bull_mcclellan_sum = _n("bull_mcclellan_sum"); bull_mcclellan_sum_signal = _n("bull_mcclellan_sum_signal")
    n_bearish = _big("n_bearish"); n_bullish = _big("n_bullish")
    bearish_signal = _n("bearish_signal"); bullish_signal = _n("bullish_signal")


class BreadthConcentration(Base):
    """Top-5 concentration vs equal weight (features/concentration.parquet)."""

    __tablename__ = "breadth_concentration"

    date = Column(Date, primary_key=True)
    tasi = Column("tasi", Numeric(14, 4), nullable=True)
    ew = _n("ew"); tasi_ret = _n("tasi_ret"); ew_ret = _n("ew_ret")
    spread_20 = _n("spread_20"); spread_60 = _n("spread_60")
    ratio = _n("ratio"); ratio_ma = _n("ratio_ma"); ratio_trend = _n("ratio_trend")
    ew_lead_60 = _n("ew_lead_60")
    top5_contrib = _n("top5_contrib"); top5_share = _n("top5_share")
    cw_proxy_ret = _n("cw_proxy_ret")
    top5_names = Column("top5_names", String(128), nullable=True)  # "2222,1211,..."


class BreadthWidget(Base):
    """Overview-widget day summary (features/widget.parquet)."""

    __tablename__ = "breadth_widget"

    date = Column(Date, primary_key=True)
    up_from_open = _big("up_from_open"); down_from_open = _big("down_from_open")
    n_open = _big("n_open")
    up_on_volume = _big("up_on_volume"); down_on_volume = _big("down_on_volume")
    stage1 = _big("stage1"); stage2 = _big("stage2"); stage3 = _big("stage3"); stage4 = _big("stage4")
    n_stage = _big("n_stage")


class BreadthStagesWeekly(Base):
    """Weekly Minervini/Weinstein stage distribution (features/stages_weekly.parquet)."""

    __tablename__ = "breadth_stages_weekly"

    date = Column(Date, primary_key=True)
    stage1_w = _big("stage1_w"); stage2_w = _big("stage2_w")
    stage3_w = _big("stage3_w"); stage4_w = _big("stage4_w")
    n_stage_w = _big("n_stage_w")
    pct_stage1_w = _n("pct_stage1_w"); pct_stage2_w = _n("pct_stage2_w")
    pct_stage3_w = _n("pct_stage3_w"); pct_stage4_w = _n("pct_stage4_w")


class BreadthBpi(Base):
    """Breadth Thrust Power Index (features/bpi.parquet)."""

    __tablename__ = "breadth_bpi"

    date = Column(Date, primary_key=True)
    n_pnf = _big("n_pnf"); pnf_buy = _big("pnf_buy"); pnf_sell = _big("pnf_sell")
    bpi = _n("bpi")
    bpi_status = _s("bpi_status", 32)


class BreadthSectors(Base):
    """LONG table: one row per (date, sector) — features/sectors.parquet (61k rows)."""

    __tablename__ = "breadth_sectors"

    date = Column(Date, primary_key=True)
    sector = Column("sector", String(128), primary_key=True)     # composite PK per owner note
    n = _big("n")
    pct50 = _n("pct50"); pct200 = _n("pct200")
    nhnl = Column("nhnl", Numeric(18, 6), nullable=True)
    ad10 = _n("ad10"); mom10 = _n("mom10")
    score = _n("score")
    healthy = Column("healthy", Numeric(6, 4), nullable=True)   # engine flags: bool/0/1
    weak = Column("weak", Numeric(6, 4), nullable=True)

    __table_args__ = (
        Index("idx_breadth_sectors_date", "date"),
        Index("idx_breadth_sectors_sector", "sector"),
    )


class BreadthSectorsSummary(Base):
    """Daily sector-health rollup (features/sectors_summary.parquet)."""

    __tablename__ = "breadth_sectors_summary"

    date = Column(Date, primary_key=True)
    n_sectors = _big("n_sectors"); n_healthy = _big("n_healthy"); n_weak = _big("n_weak")
    median_score = _n("median_score"); pct_healthy = _n("pct_healthy"); pct_weak = _n("pct_weak")


class BreadthComposite(Base):
    """Final score / regime / drivers (features/composite.parquet)."""

    __tablename__ = "breadth_composite"

    date = Column(Date, primary_key=True)
    A_participation = _n("A_participation"); B_advance_decline = _n("B_advance_decline")
    E_sectors = _n("E_sectors")
    overlay = _n("overlay"); score = _n("score")
    contrib_A_participation = _n("contrib_A_participation"); delta_A_participation = _n("delta_A_participation")
    contrib_B_advance_decline = _n("contrib_B_advance_decline"); delta_B_advance_decline = _n("delta_B_advance_decline")
    contrib_E_sectors = _n("contrib_E_sectors"); delta_E_sectors = _n("delta_E_sectors")
    driver = _s("driver", 32)
    score_change = _n("score_change")
    overlay_s = _n("overlay_s"); score_smooth = _n("score_smooth")
    regime = _s("regime", 16)
    raw_pct_above_sma200 = _n("raw_pct_above_sma200"); raw_pct_above_sma50 = _n("raw_pct_above_sma50")
    raw_hl_index = _n("raw_hl_index"); raw_mcclellan_osc = _n("raw_mcclellan_osc")
    raw_ad_line_chg20 = _n("raw_ad_line_chg20"); raw_pct_healthy = _n("raw_pct_healthy")
    raw_median_score = _n("raw_median_score"); raw_spread_60 = _n("raw_spread_60")
    raw_confirmation = _n("raw_confirmation"); raw_trin_10 = _n("raw_trin_10")
    raw_ratio_10_inv = _n("raw_ratio_10_inv"); raw_pct_down_x_10d = _n("raw_pct_down_x_10d")
    raw_lowry95_down_recent = _n("raw_lowry95_down_recent"); raw_thrust_recent = _n("raw_thrust_recent")


class BreadthCompositePercentiles(Base):
    """Rolling percentiles of composite inputs (features/composite_percentiles.parquet)."""

    __tablename__ = "breadth_composite_percentiles"

    date = Column(Date, primary_key=True)
    pct_above_sma200 = _n("pct_above_sma200"); pct_above_sma50 = _n("pct_above_sma50")
    hl_index = _n("hl_index"); mcclellan_osc = _n("mcclellan_osc")
    ad_line_chg20 = _n("ad_line_chg20"); pct_healthy = _n("pct_healthy")
    median_score = _n("median_score"); trin_10 = _n("trin_10")
    ratio_10_inv = _n("ratio_10_inv"); pct_down_x_10d = _n("pct_down_x_10d")
    lowry95_down_recent = _n("lowry95_down_recent"); thrust_recent = _n("thrust_recent")


class BreadthData(Base):
    """Rolling snapshot of the engine's DATA object (pro_dashboard.build output).

    One JSONB row per trading date, kept for the last 14 days ONLY — the daily
    job upserts then prunes in the same transaction path (no extra jobs).
    Serves the 10 /api/rebh/breadth/* endpoints with byte-for-byte parity to
    the client's api_samples (same object the reference dashboard embeds).
    """

    __tablename__ = "breadth_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trading_date = Column(Date, nullable=False, unique=True, index=True)
    engine_version = Column(String(32), nullable=False, default="breadth-v3")
    data = Column(JSONB, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    def __repr__(self):
        return f"<BreadthData(trading_date={self.trading_date})>"
