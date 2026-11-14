"""
Script to fix DB column precision for stock_indicators and prices.
Also rewrites the model files to use Numeric(14, 4) instead of 2 decimals.
"""
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import create_engine, text

# 1. Update stock_indicators.py
model_path = os.path.join("app", "models", "stock_indicators.py")
with open(model_path, "r", encoding="utf-8") as f:
    content = f.read()

content = re.sub(r'Numeric\(5,\s*2\)', 'Numeric(14, 4)', content)
content = re.sub(r'Numeric\(10,\s*2\)', 'Numeric(14, 4)', content)

with open(model_path, "w", encoding="utf-8") as f:
    f.write(content)
print(f"✅ Updated {model_path} models precision")

# 2. Update price.py
price_model_path = os.path.join("app", "models", "price.py")
with open(price_model_path, "r", encoding="utf-8") as f:
    p_content = f.read()

p_content = re.sub(r'Numeric\(12,\s*2\)', 'Numeric(14, 4)', p_content)
p_content = re.sub(r'Numeric\(8,\s*2\)', 'Numeric(14, 4)', p_content)

with open(price_model_path, "w", encoding="utf-8") as f:
    f.write(p_content)
print(f"✅ Updated {price_model_path} precision")

# 3. Alter DB columns directly
from app.core.config import settings

engine = create_engine(str(settings.DATABASE_URL))

# List of columns to alter in stock_indicators
stock_cols = [
    'close', 'rsi_14', 'rsi_3', 'sma9_rsi', 'wma45_rsi', 'ema45_rsi', 'sma3_rsi3',
    'ema20_sma3', 'rsi_w', 'rsi_3_w', 'sma3_rsi3_w', 'sma9_rsi_w', 'wma45_rsi_w',
    'ema45_rsi_w', 'ema20_sma3_w', 'sma9_close', 'high_sma13', 'low_sma13',
    'high_sma65', 'low_sma65', 'the_number', 'the_number_hl', 'the_number_ll',
    'sma9_close_w', 'the_number_w', 'the_number_hl_w', 'the_number_ll_w',
    'high_sma13_w', 'low_sma13_w', 'high_sma65_w', 'low_sma65_w', 'rsi_14_9days_ago',
    'stamp_a_value', 'stamp_s9rsi', 'stamp_e45cfg', 'stamp_e45rsi', 'stamp_e20sma3',
    'rsi_14_9days_ago_w', 'stamp_a_value_w', 'stamp_s9rsi_w', 'stamp_e45cfg_w',
    'stamp_e45rsi_w', 'stamp_e20sma3_w', 'cfg_daily', 'cfg_sma4', 'cfg_sma9',
    'cfg_sma20', 'cfg_ema20', 'cfg_ema45', 'cfg_wma45', 'cfg_w', 'cfg_sma4_w',
    'cfg_sma9_w', 'cfg_ema20_w', 'cfg_ema45_w', 'cfg_wma45_w', 'rsi_14_9days_ago_cfg',
    'rsi_14_minus_9', 'rsi_14_minus_9_w', 'rsi_14_w_shifted', 'sma4', 'sma9',
    'sma18', 'wma45_close', 'close_w', 'sma4_w', 'sma9_w', 'sma18_w', 'wma45_close_w',
    'cci', 'cci_ema20', 'cci_w', 'cci_ema20_w', 'aroon_up', 'aroon_down',
    'aroon_up_w', 'aroon_down_w'
]

price_cols = [
    'change', 'price_minus_sma_10', 'price_minus_sma_21', 'price_minus_sma_50',
    'price_minus_sma_150', 'price_minus_sma_200', 'fifty_two_week_high',
    'fifty_two_week_low', 'price_vs_sma_10_percent', 'price_vs_sma_21_percent',
    'price_vs_sma_50_percent', 'price_vs_sma_150_percent', 'price_vs_sma_200_percent',
    'price_vs_ema_10_percent', 'price_vs_ema_21_percent', 'percent_off_52w_high',
    'percent_off_52w_low', 'vol_diff_50_percent', 'sma_10', 'sma_21', 'sma_50',
    'sma_150', 'sma_200', 'ema_10', 'ema_21', 'sma_200_1m_ago', 'sma_200_2m_ago',
    'sma_200_3m_ago', 'sma_200_4m_ago', 'sma_200_5m_ago', 'sma_30w', 'sma_40w'
]

with engine.begin() as conn:
    for col in stock_cols:
        try:
            conn.execute(text(f"ALTER TABLE stock_indicators ALTER COLUMN {col} TYPE NUMERIC(14, 4);"))
        except Exception as e:
            print(f"Skipped {col} in stock_indicators: {e}")
            
    for col in price_cols:
        try:
            conn.execute(text(f"ALTER TABLE prices ALTER COLUMN {col} TYPE NUMERIC(14, 4);"))
        except Exception as e:
            print(f"Skipped {col} in prices: {e}")

print("✅ Database types altered to NUMERIC(14, 4)")
