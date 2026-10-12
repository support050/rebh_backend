"""
Stock Indicators Model
Stores pre-computed technical indicators for each stock per day
"""

from sqlalchemy import Column, Integer, String, Date, Numeric, Boolean, DateTime, UniqueConstraint, Index
from sqlalchemy.sql import func
from app.core.database import Base


class StockIndicator(Base):
    __tablename__ = "stock_indicators"
    
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    
    # Company Info
    company_name = Column(String(255), nullable=True)
    # Price
    close = Column(Numeric(12, 4), nullable=True)
    
    # ============ 1. RSI COMPONENTS ============
    # RSI Values
    rsi_14 = Column(Numeric(12, 4), nullable=True)              # RSI(14) - رمادي
    rsi_3 = Column(Numeric(12, 4), nullable=True)              # RSI(3)
    
    # Moving Averages of RSI
    sma9_rsi = Column(Numeric(12, 4), nullable=True)            # SMA9 RSI - أزرق
    wma45_rsi = Column(Numeric(12, 4), nullable=True)           # WMA45 RSI - أحمر
    ema45_rsi = Column(Numeric(12, 4), nullable=True)          # EMA45 RSI - للـ Screener
    
    # SMA3 of RSI3
    sma3_rsi3 = Column(Numeric(12, 4), nullable=True)          # SMA(RSI3, 3)
    ema20_sma3 = Column(Numeric(12, 4), nullable=True)         # EMA20(SMA3)
    
    # Weekly RSI
    rsi_w = Column(Numeric(12, 4), nullable=True)
    rsi_3_w = Column(Numeric(12, 4), nullable=True)
    sma3_rsi3_w = Column(Numeric(12, 4), nullable=True)
    sma9_rsi_w = Column(Numeric(12, 4), nullable=True)
    wma45_rsi_w = Column(Numeric(12, 4), nullable=True)
    ema45_rsi_w = Column(Numeric(12, 4), nullable=True)
    ema20_sma3_w = Column(Numeric(12, 4), nullable=True)
    
    # ============ 2. THE NUMBER COMPONENTS ============
    sma9_close = Column(Numeric(12, 4), nullable=True)         # SMA9 - أخضر
    high_sma13 = Column(Numeric(12, 4), nullable=True)         # SMA13 High
    low_sma13 = Column(Numeric(12, 4), nullable=True)          # SMA13 Low
    high_sma65 = Column(Numeric(12, 4), nullable=True)         # SMA65 High
    low_sma65 = Column(Numeric(12, 4), nullable=True)          # SMA65 Low
    the_number = Column(Numeric(12, 4), nullable=True)         # THE.NUMBER - أحمر
    the_number_hl = Column(Numeric(12, 4), nullable=True)      # Upper Band - أزرق
    the_number_ll = Column(Numeric(12, 4), nullable=True)      # Lower Band - أزرق
    
    # Weekly The Number
    sma9_close_w = Column(Numeric(12, 4), nullable=True)
    the_number_w = Column(Numeric(12, 4), nullable=True)
    the_number_hl_w = Column(Numeric(12, 4), nullable=True)    # Upper Band Weekly
    the_number_ll_w = Column(Numeric(12, 4), nullable=True)    # Lower Band Weekly
    high_sma13_w = Column(Numeric(12, 4), nullable=True)
    low_sma13_w = Column(Numeric(12, 4), nullable=True)
    high_sma65_w = Column(Numeric(12, 4), nullable=True)
    low_sma65_w = Column(Numeric(12, 4), nullable=True)
    
    # ============ 3. STAMP INDICATOR COMPONENTS ============
    # Formula: A = RSI(14) - RSI(14)[9] + SMA(RSI(3), 3)
    rsi_14_9days_ago = Column(Numeric(12, 4), nullable=True)    # RSI14[9] - قيمة RSI من 9 أيام مضت
    stamp_a_value = Column(Numeric(12, 4), nullable=True)      # قيمة A = RSI14 - RSI14[9] + SMA3(RSI3)
    
    # Stamp Plots - Daily
    stamp_s9rsi = Column(Numeric(12, 4), nullable=True)        # S9rsi - أحمر
    stamp_e45cfg = Column(Numeric(12, 4), nullable=True)       # E45cfg - أخضر
    stamp_e45rsi = Column(Numeric(12, 4), nullable=True)       # E45rsi - أصفر
    stamp_e20sma3 = Column(Numeric(12, 4), nullable=True)      # E20(sma3(rsi3)) - أسود
    
    # Stamp Weekly - مكونات STAMP الأسبوعية
    rsi_14_9days_ago_w = Column(Numeric(12, 4), nullable=True)  # RSI14[9] Weekly
    stamp_a_value_w = Column(Numeric(12, 4), nullable=True)     # A value Weekly
    stamp_s9rsi_w = Column(Numeric(12, 4), nullable=True)       # SMA9(RSI14) Weekly
    stamp_e45cfg_w = Column(Numeric(12, 4), nullable=True)      # EMA45(CFG) Weekly
    stamp_e45rsi_w = Column(Numeric(12, 4), nullable=True)      # EMA45(RSI14) Weekly
    stamp_e20sma3_w = Column(Numeric(12, 4), nullable=True)     # EMA20(SMA3(RSI3)) Weekly
    
    # ============ 4. CFG ANALYSIS ============
    # CFG = RSI14 - RSI14[9] + SMA(RSI3, 3)
    cfg_daily = Column(Numeric(12, 4), nullable=True)
    cfg_sma4 = Column(Numeric(12, 4), nullable=True)           # ✅ S4CFG Daily
    cfg_sma9 = Column(Numeric(12, 4), nullable=True)
    cfg_sma20 = Column(Numeric(12, 4), nullable=True)
    cfg_ema20 = Column(Numeric(12, 4), nullable=True)
    cfg_ema45 = Column(Numeric(12, 4), nullable=True)
    cfg_wma45 = Column(Numeric(12, 4), nullable=True)
    
    # CFG Weekly
    cfg_w = Column(Numeric(12, 4), nullable=True)
    cfg_sma4_w = Column(Numeric(12, 4), nullable=True)         # ✅ S4CFG Weekly
    cfg_sma9_w = Column(Numeric(12, 4), nullable=True)
    cfg_sma20_w = Column(Numeric(12, 4), nullable=True)        # SMA20(CFG) Weekly
    cfg_ema20_w = Column(Numeric(12, 4), nullable=True)
    cfg_ema45_w = Column(Numeric(12, 4), nullable=True)
    cfg_wma45_w = Column(Numeric(12, 4), nullable=True)
    
    # CFG Components
    rsi_14_9days_ago_cfg = Column(Numeric(12, 4), nullable=True)  # RSI14[9] للـ CFG
    rsi_14_minus_9 = Column(Numeric(12, 4), nullable=True)        # RSI14 - RSI14[9]
    rsi_14_minus_9_w = Column(Numeric(12, 4), nullable=True)      # RSI14_w - RSI14_w[9]
    rsi_14_w_shifted = Column(Numeric(12, 4), nullable=True)      # ta.rsi(close[9], 14) Weekly
    
    # CFG Conditions
    cfg_gt_50_daily = Column(Boolean, default=False)
    cfg_ema45_gt_50 = Column(Boolean, default=False)
    cfg_ema20_gt_50 = Column(Boolean, default=False)
    cfg_gt_50_w = Column(Boolean, default=False)
    cfg_ema45_gt_50_w = Column(Boolean, default=False)
    cfg_ema20_gt_50_w = Column(Boolean, default=False)
    
    # ============ 5. TREND SCREENER COMPONENTS ============
    # Price Moving Averages - Daily (PineScript-exact EMA values)
    ema10 = Column(Numeric(12, 4), nullable=True)               # ✅ EMA10 PineScript exact
    ema21 = Column(Numeric(12, 4), nullable=True)               # ✅ EMA21 PineScript exact
    sma4 = Column(Numeric(12, 4), nullable=True)
    sma9 = Column(Numeric(12, 4), nullable=True)
    sma18 = Column(Numeric(12, 4), nullable=True)
    wma45_close = Column(Numeric(12, 4), nullable=True)        # WMA45 Close ✅ تأكد من وجوده
    
    # Price Moving Averages - Weekly
    close_w = Column(Numeric(12, 4), nullable=True)            # ✅ Weekly Close
    sma4_w = Column(Numeric(12, 4), nullable=True)             # ✅ Weekly SMA4
    sma9_w = Column(Numeric(12, 4), nullable=True)             # ✅ Weekly SMA9
    sma18_w = Column(Numeric(12, 4), nullable=True)            # ✅ Weekly SMA18
    wma45_close_w = Column(Numeric(12, 4), nullable=True)      # ✅ Weekly WMA45
    
    # CCI
    cci = Column(Numeric(12, 4), nullable=True)                # CCI Daily
    cci_ema20 = Column(Numeric(12, 4), nullable=True)          # CCI EMA20 Daily
    cci_w = Column(Numeric(12, 4), nullable=True)              # ✅ CCI Weekly
    cci_ema20_w = Column(Numeric(12, 4), nullable=True)        # ✅ CCI EMA20 Weekly
    
    # Aroon (باستخدام أول occurrence)
    aroon_up = Column(Numeric(12, 4), nullable=True)            # Aroon Up Daily
    aroon_down = Column(Numeric(12, 4), nullable=True)          # Aroon Down Daily
    aroon_up_w = Column(Numeric(12, 4), nullable=True)          # ✅ Aroon Up Weekly
    aroon_down_w = Column(Numeric(12, 4), nullable=True)        # ✅ Aroon Down Weekly
    
    # Trend Conditions
    price_gt_sma18 = Column(Boolean, default=False)
    price_gt_sma9_weekly = Column(Boolean, default=False)      # ✅ شرط السعر > SMA9 أسبوعي
    sma_trend_daily = Column(Boolean, default=False)
    sma_trend_weekly = Column(Boolean, default=False)          # ✅ شرط ترتيب المتوسطات أسبوعي
    cci_gt_100 = Column(Boolean, default=False)
    cci_ema20_gt_0_daily = Column(Boolean, default=False)
    
    # ============ 6. MARKET STATISTICS (formerly in prices table) ============
    # Standard Daily SMAs (SMA 10/20/21/50/100/150/200)
    sma_10  = Column(Numeric(14, 4), nullable=True)
    sma_20  = Column(Numeric(14, 4), nullable=True)
    sma_21  = Column(Numeric(14, 4), nullable=True)
    sma_50  = Column(Numeric(14, 4), nullable=True)
    sma_100 = Column(Numeric(14, 4), nullable=True)
    sma_150 = Column(Numeric(14, 4), nullable=True)
    sma_200 = Column(Numeric(14, 4), nullable=True)

    # Historical 200MA (for trend conditions)
    sma_200_1m_ago = Column(Numeric(14, 4), nullable=True)
    sma_200_2m_ago = Column(Numeric(14, 4), nullable=True)
    sma_200_3m_ago = Column(Numeric(14, 4), nullable=True)
    sma_200_4m_ago = Column(Numeric(14, 4), nullable=True)
    sma_200_5m_ago = Column(Numeric(14, 4), nullable=True)

    # Weekly SMAs (30W and 40W)
    sma_30w = Column(Numeric(14, 4), nullable=True)
    sma_40w = Column(Numeric(14, 4), nullable=True)

    # 52-Week High / Low & Volume Stats
    fifty_two_week_high = Column(Numeric(14, 4), nullable=True)
    fifty_two_week_low  = Column(Numeric(14, 4), nullable=True)
    average_volume_50   = Column(Numeric(20, 2), nullable=True)

    # Price vs SMA (Absolute difference)
    price_minus_sma_10  = Column(Numeric(14, 4), nullable=True)
    price_minus_sma_20  = Column(Numeric(14, 4), nullable=True)
    price_minus_sma_21  = Column(Numeric(14, 4), nullable=True)
    price_minus_sma_50  = Column(Numeric(14, 4), nullable=True)
    price_minus_sma_100 = Column(Numeric(14, 4), nullable=True)
    price_minus_sma_150 = Column(Numeric(14, 4), nullable=True)
    price_minus_sma_200 = Column(Numeric(14, 4), nullable=True)

    # Price vs SMA (Percentage)
    price_vs_sma_10_percent  = Column(Numeric(14, 4), nullable=True)
    price_vs_sma_20_percent  = Column(Numeric(14, 4), nullable=True)
    price_vs_sma_21_percent  = Column(Numeric(14, 4), nullable=True)
    price_vs_sma_50_percent  = Column(Numeric(14, 4), nullable=True)
    price_vs_sma_100_percent = Column(Numeric(14, 4), nullable=True)
    price_vs_sma_150_percent = Column(Numeric(14, 4), nullable=True)
    price_vs_sma_200_percent = Column(Numeric(14, 4), nullable=True)

    # Off High / Low & Volume vs Average
    percent_off_52w_high = Column(Numeric(14, 4), nullable=True)
    percent_off_52w_low  = Column(Numeric(14, 4), nullable=True)
    vol_diff_50_percent  = Column(Numeric(14, 4), nullable=True)

    # Power Play: % Change over N days
    percent_change_15d  = Column(Numeric(14, 4), nullable=True)   # % Change last 15 days
    percent_change_20d  = Column(Numeric(14, 4), nullable=True)   # % Change last 20 days (1 month)
    percent_change_63d  = Column(Numeric(14, 4), nullable=True)   # % Change last 63 days (3 months)
    percent_change_126d = Column(Numeric(14, 4), nullable=True)   # % Change last 126 days (6 months)

    # ============ MA COMPARISON CONDITIONS ============
    ema10_gt_sma50 = Column(Boolean, default=False)             # ✅ EMA10 > SMA50
    ema10_gt_sma200 = Column(Boolean, default=False)            # ✅ EMA10 > SMA200
    ema21_gt_sma50 = Column(Boolean, default=False)             # ✅ EMA21 > SMA50
    ema21_gt_sma200 = Column(Boolean, default=False)            # ✅ EMA21 > SMA200
    sma50_gt_sma150 = Column(Boolean, default=False)            # ✅ SMA50 > SMA150
    sma50_gt_sma200 = Column(Boolean, default=False)            # ✅ SMA50 > SMA200
    sma150_gt_sma200 = Column(Boolean, default=False)           # ✅ SMA150 > SMA200
    
    # ============ SMA200 TREND CONDITIONS ============
    sma200_gt_sma200_1m_ago = Column(Boolean, default=False)    # ✅ SMA200 > SMA200 من شهر 1 مضى
    sma200_gt_sma200_2m_ago = Column(Boolean, default=False)    # ✅ SMA200 > SMA200 من شهر 2 مضى
    sma200_gt_sma200_3m_ago = Column(Boolean, default=False)    # ✅ SMA200 > SMA200 من شهر 3 مضى
    sma200_gt_sma200_4m_ago = Column(Boolean, default=False)    # ✅ SMA200 > SMA200 من شهر 4 مضى
    sma200_gt_sma200_5m_ago = Column(Boolean, default=False)    # ✅ SMA200 > SMA200 من شهر 5 مضى
    cci_ema20_gt_0_weekly = Column(Boolean, default=False)     # ✅ شرط CCI EMA20 > 0 أسبوعي
    aroon_up_gt_70 = Column(Boolean, default=False)
    aroon_down_lt_30 = Column(Boolean, default=False)
    
    # Filters
    is_etf_or_index = Column(Boolean, default=False)
    has_gap = Column(Boolean, default=False)
    trend_signal = Column(Boolean, default=False)              # ✅ إشارة التريند النهائية
    beta = Column(Numeric(12, 4), nullable=True)               # ✅ Beta (Volatility vs Benchmark)
    
    # ============ 6. RSI SCREENER ============
    # RSI Screener Conditions
    sma9_gt_tn_daily = Column(Boolean, default=False)
    sma9_gt_tn_weekly = Column(Boolean, default=False)
    rsi_lt_80_d = Column(Boolean, default=False)
    rsi_lt_80_w = Column(Boolean, default=False)
    sma9_rsi_lte_75_d = Column(Boolean, default=False)
    sma9_rsi_lte_75_w = Column(Boolean, default=False)
    ema45_rsi_lte_70_d = Column(Boolean, default=False)
    ema45_rsi_lte_70_w = Column(Boolean, default=False)
    rsi_55_70 = Column(Boolean, default=False)
    rsi_gt_wma45_d = Column(Boolean, default=False)
    rsi_gt_wma45_w = Column(Boolean, default=False)
    sma9rsi_gt_wma45rsi_d = Column(Boolean, default=False)
    sma9rsi_gt_wma45rsi_w = Column(Boolean, default=False)
    
    # STAMP Conditions
    stamp_daily = Column(Boolean, default=False)
    stamp_weekly = Column(Boolean, default=False)
    stamp = Column(Boolean, default=False)
    
    # Final Results
    final_signal = Column(Boolean, default=False)               # ✅ الإشارة النهائية (validSignal في Pine Script)
    score = Column(Integer, default=0)
    
    # Timestamps
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    # Constraints
    __table_args__ = (
        UniqueConstraint('symbol', 'date', name='uix_stock_indicators_symbol_date'),
        Index('idx_stock_indicators_symbol', 'symbol'),
        Index('idx_stock_indicators_date', 'date'),
        Index('idx_stock_indicators_score', 'score'),
        Index('idx_stock_indicators_final_signal', 'final_signal'),
        Index('idx_stock_indicators_trend_signal', 'trend_signal'),
        Index('idx_stock_indicators_cfg_ema45_gt_50', 'cfg_ema45_gt_50'),
    )
    
    def __repr__(self):
        return f"<StockIndicator(symbol={self.symbol}, date={self.date}, score={self.score})>"