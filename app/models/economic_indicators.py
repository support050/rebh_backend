from sqlalchemy import Column, Integer, String, Text, Date, Numeric, DateTime, UniqueConstraint, BigInteger, Boolean, JSON
from sqlalchemy.sql import func
from app.core.database import Base

class EconomicIndicator(Base):
    __tablename__ = "economic_indicators"
    id = Column(Integer, primary_key=True, index=True)
    report_date = Column(Date, nullable=False, index=True)
    indicator_code = Column(String(50), nullable=False, index=True) # e.g. UNRATE, PAYEMS, IC4WSA
    value = Column(Numeric(precision=15, scale=6), nullable=True)
    yoy_pct = Column(Numeric(precision=10, scale=4), nullable=True)  # Year-over-year % change (scraped directly from source)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    __table_args__ = (UniqueConstraint('report_date', 'indicator_code', name='uq_economic_indicator_date_code'),)


class FredSeriesMetadata(Base):
    """Per-series FRED metadata, persisted (not a transient cache).

    One row per indicator_code (UNRATE / PAYEMS / IC4WSA / ...). Written by
    app/scrapers/fred_scraper.py alongside the historical observations, and
    read by the economic-indicator API so the frontend renders live units,
    frequency, latest observation, last-updated stamp and next release date
    instead of the old hardcoded indicatorMetadata.ts.

    `last_updated_time` keeps FRED's exact display string (e.g. "Oct 2, 2026
    8:24 AM CDT"); `updated_at` is when WE last refreshed the row.
    """
    __tablename__ = "fred_series_metadata"

    id = Column(Integer, primary_key=True, index=True)
    indicator_code = Column(String(50), nullable=False, unique=True, index=True)  # UNRATE, PAYEMS, IC4WSA
    series_id = Column(String(50), nullable=True)          # FRED series id (usually == indicator_code)
    title = Column(String(200), nullable=True)

    units = Column(String(100), nullable=True)             # e.g. "Percent", "Thousands of Persons"
    frequency = Column(String(50), nullable=True)          # e.g. "Monthly", "Weekly, Ending Saturday"
    seasonal_adjustment = Column(String(50), nullable=True)  # e.g. "Seasonally Adjusted"
    source_agency = Column(String(200), nullable=True)     # e.g. "U.S. Bureau of Labor Statistics"
    fred_url = Column(String(300), nullable=True)          # https://fred.stlouisfed.org/series/<ID>

    latest_observation_date = Column(Date, nullable=True)
    latest_observation_value = Column(Numeric(precision=15, scale=6), nullable=True)
    last_updated_time = Column(String(60), nullable=True)  # exact FRED "Updated:" text
    next_release_date = Column(Date, nullable=True)
    # FRED groups series under a "release" that has its own publication calendar
    # (rid 209 = ICE BofA Indices, 50 = Employment Situation, 180 = UI Weekly
    # Claims). The next date comes from /fred/release/dates?release_id=<id>, and
    # FRED's own "Next Release Date" link is the calendar page for that rid+year.
    # Kept because /fred/series does NOT expose release_id (verified 2026-10-31).
    release_id = Column(Integer, nullable=True)
    release_calendar_url = Column(String(300), nullable=True)

    notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TreasuryYieldCurve(Base):
    __tablename__ = "treasury_yield_curves"
    id = Column(Integer, primary_key=True, index=True)
    report_date = Column(Date, nullable=False, unique=True, index=True)
    
    month_1 = Column(Numeric(precision=8, scale=4), nullable=True)
    month_1_5 = Column(Numeric(precision=8, scale=4), nullable=True)
    month_2 = Column(Numeric(precision=8, scale=4), nullable=True)
    month_3 = Column(Numeric(precision=8, scale=4), nullable=True)
    month_4 = Column(Numeric(precision=8, scale=4), nullable=True)
    month_6 = Column(Numeric(precision=8, scale=4), nullable=True)
    year_1 = Column(Numeric(precision=8, scale=4), nullable=True)
    year_2 = Column(Numeric(precision=8, scale=4), nullable=True)
    year_3 = Column(Numeric(precision=8, scale=4), nullable=True)
    year_5 = Column(Numeric(precision=8, scale=4), nullable=True)
    year_7 = Column(Numeric(precision=8, scale=4), nullable=True)
    year_10 = Column(Numeric(precision=8, scale=4), nullable=True)
    year_20 = Column(Numeric(precision=8, scale=4), nullable=True)
    year_30 = Column(Numeric(precision=8, scale=4), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # stamped by the treasury_gov upsert on every ON CONFLICT DO UPDATE — the
    # honest "last time this row's data was (re)fetched" (created_at never
    # moves, so it cannot serve as the freshness signal).
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class SP500History(Base):
    __tablename__ = "sp500_history"
    id         = Column(Integer, primary_key=True, index=True)
    trade_date = Column(Date, unique=True, nullable=False, index=True)
    open       = Column(Numeric(precision=12, scale=4), nullable=True)
    high       = Column(Numeric(precision=12, scale=4), nullable=True)
    low        = Column(Numeric(precision=12, scale=4), nullable=True)
    close      = Column(Numeric(precision=12, scale=4), nullable=True)
    volume     = Column(BigInteger, nullable=True)
    pe_ratio   = Column(Numeric(precision=10, scale=4), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EurodollarFutures(Base):
    """Daily snapshot of Eurodollar futures contracts from Investing.com."""
    __tablename__ = "eurodollar_futures"
    id           = Column(Integer, primary_key=True, index=True)
    scrape_date  = Column(Date, nullable=False, index=True)        # The day we scraped
    symbol       = Column(String(50), nullable=True, index=True)   # e.g. GEM26
    contract     = Column(String(50), nullable=False, index=True)  # e.g. Jun 26
    last_price   = Column(Numeric(precision=10, scale=4), nullable=True)
    change       = Column(Numeric(precision=10, scale=4), nullable=True)
    open_price   = Column(Numeric(precision=10, scale=4), nullable=True)
    high         = Column(Numeric(precision=10, scale=4), nullable=True)
    low          = Column(Numeric(precision=10, scale=4), nullable=True)
    previous     = Column(Numeric(precision=10, scale=4), nullable=True)
    volume       = Column(BigInteger, nullable=True)
    open_interest = Column(Integer, nullable=True)
    updated_time = Column(String(30), nullable=True)               # e.g. "02:46 CT"

    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint('scrape_date', 'contract', name='uq_sofr_futures_date_contract'),)


class CmeFedwatch(Base):
    """Daily snapshot of CME FedWatch probabilities for upcoming FOMC meetings."""
    __tablename__ = "cme_fedwatch"
    id            = Column(Integer, primary_key=True, index=True)
    scrape_date   = Column(Date, nullable=False, index=True)        # The day we scraped
    meeting_date  = Column(String(50), nullable=False, index=True)  # e.g., "1 May 2024"
    rate_range    = Column(String(50), nullable=False)              # e.g., "5.25 - 5.50"
    probability   = Column(Numeric(precision=5, scale=2), nullable=False)                   # e.g., 95.5 (percentage)
    
    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint('scrape_date', 'meeting_date', 'rate_range', name='uq_fedwatch_date_meeting_rate'),)


class CmeFedwatchSnapshot(Base):
    """The QuikStrike "Current" view header: meeting info + aggregate odds.

    One row per scrape. Everything here is read off the default (nearest) meeting
    tab, which is the only tab the tool renders without a browser-side click.
    """
    __tablename__ = "cme_fedwatch_snapshot"
    id                   = Column(Integer, primary_key=True, index=True)
    scrape_date          = Column(Date, nullable=False, unique=True, index=True)
    current_target_range = Column(String(20), nullable=True)    # "375-400"
    chart_title          = Column(String(120), nullable=True)   # "Target Rate Probabilities for 9 Dec 2026 Fed Meeting"
    chart_labels         = Column(JSON, nullable=True)          # ["375-400", "400-425"]
    meeting_label        = Column(String(40), nullable=True)    # "9 Dec 2026"
    meeting_date         = Column(Date, nullable=True, index=True)
    contract             = Column(String(10), nullable=True)    # "ZQZ6"
    expires              = Column(String(30), nullable=True)    # "31 Dec 2026"
    mid_price            = Column(Numeric(precision=10, scale=4), nullable=True)
    prior_volume         = Column(BigInteger, nullable=True)
    prior_oi             = Column(BigInteger, nullable=True)
    prob_ease            = Column(Numeric(precision=6, scale=2), nullable=True)
    prob_no_change       = Column(Numeric(precision=6, scale=2), nullable=True)
    prob_hike            = Column(Numeric(precision=6, scale=2), nullable=True)
    data_as_of           = Column(String(60), nullable=True)    # "31 Oct 2026 04:49:50 CT"

    created_at           = Column(DateTime(timezone=True), server_default=func.now())


class CmeFedwatchHorizon(Base):
    """"Target Rate (bps) / Probability(%)" table: each bucket across Now/1D/1W/1M."""
    __tablename__ = "cme_fedwatch_horizon"
    id            = Column(Integer, primary_key=True, index=True)
    scrape_date   = Column(Date, nullable=False, index=True)
    meeting_date  = Column(Date, nullable=False, index=True)
    rate_range    = Column(String(20), nullable=False)          # "375-400"
    is_current    = Column(Boolean, default=False, nullable=False)
    horizon       = Column(String(10), nullable=False)          # now | 1d | 1w | 1m
    horizon_label = Column(String(30), nullable=True)           # "1 Day 30 Oct 2026"
    probability   = Column(Numeric(precision=6, scale=2), nullable=True)

    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint('scrape_date', 'meeting_date', 'rate_range', 'horizon',
                                       name='uq_fedwatch_horizon_date_meeting_range_h'),)


class CmeFedwatchFutures(Base):
    """The "Fed Fund Futures" ladder shown above the conditional matrix."""
    __tablename__ = "cme_fedwatch_futures"
    id           = Column(Integer, primary_key=True, index=True)
    scrape_date  = Column(Date, nullable=False, index=True)
    contract     = Column(String(10), nullable=False, index=True)   # "ZQZ6"
    price        = Column(Numeric(precision=10, scale=4), nullable=True)
    # CME month codes: F Jan, G Feb, H Mar, J Apr, K May, M Jun, N Jul, Q Aug,
    # U Sep, V Oct, X Nov, Z Dec — resolved to the contract's last trading month.
    contract_month = Column(Date, nullable=True, index=True)

    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint('scrape_date', 'contract', name='uq_fedwatch_futures_date_contract'),)


class CmeFedwatchMeeting(Base):
    """Per-meeting "Current" header, scraped by replaying each meeting tab.

    The tool only renders the nearest meeting on the initial GET; the other
    eight need an ASP.NET meeting-tab postback (uccv$lvMeetings$ctrlN$lbMeeting).
    Since cmefedwatch_scraper now replays all nine, every row here holds the
    *authoritative* Meeting Information (contract/expiry/mid price/prior
    volume/OI) and the tool's own Ease/No Change/Hike aggregates as printed for
    that meeting — not the derived partition the dashboard used to fall back to
    when only the nearest tab was available.
    """
    __tablename__ = "cme_fedwatch_meeting"
    id             = Column(Integer, primary_key=True, index=True)
    scrape_date    = Column(Date, nullable=False, index=True)
    meeting_date   = Column(Date, nullable=False, index=True)
    meeting_label  = Column(String(40), nullable=True)    # "27 Jan 2027"
    contract       = Column(String(10), nullable=True)    # "ZQF7"
    expires        = Column(String(30), nullable=True)    # "29 Jan 2027"
    mid_price      = Column(Numeric(precision=10, scale=4), nullable=True)
    prior_volume   = Column(BigInteger, nullable=True)
    prior_oi       = Column(BigInteger, nullable=True)
    prob_ease      = Column(Numeric(precision=6, scale=2), nullable=True)
    prob_no_change = Column(Numeric(precision=6, scale=2), nullable=True)
    prob_hike      = Column(Numeric(precision=6, scale=2), nullable=True)
    data_as_of     = Column(String(60), nullable=True)

    created_at     = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint('scrape_date', 'meeting_date',
                                       name='uq_fedwatch_meeting_date_meeting'),)


class CmeFedwatchDotPlot(Base):
    """The QuikStrike "Dot Plot - Table" view: FOMC participants' rate projections.

    Scraped by replaying the lbDotPlotTable tab. One row per (target rate, year)
    cell: `count` is how many participants plotted that year-end median and
    `bg_color` keeps the tool's own heat-map fill so the replica can shade the
    cell identically. `is_median` marks the row the tool highlights (highlight2).
    """
    __tablename__ = "cme_fedwatch_dotplot"
    id          = Column(Integer, primary_key=True, index=True)
    scrape_date = Column(Date, nullable=False, index=True)
    target_rate = Column(Numeric(precision=8, scale=3), nullable=False)  # 4.375 (%)
    year_label  = Column(String(20), nullable=False)                     # "2027" | "Longer run"
    year_order  = Column(Integer, nullable=False, default=0)             # column position
    count       = Column(Integer, nullable=True)                         # dots; None/blank = no dot
    bg_color    = Column(String(20), nullable=True)                      # inline heat colour
    is_median   = Column(Boolean, default=False, nullable=False)

    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint('scrape_date', 'target_rate', 'year_label',
                                       name='uq_fedwatch_dotplot_date_rate_year'),)

