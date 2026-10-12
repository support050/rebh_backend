"""
Unified Indicators Data Service
ربط جميع ملفات المؤشرات الأربعة في خدمة واحدة
"""
import sys
import os
import pandas as pd
import numpy as np
from typing import Dict, Any, List, Optional
from datetime import date
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import from the four specialized modules
from scripts.calculate_rsi_indicators import (
    convert_to_float, get_val,
    calculate_rsi_components, get_rsi_current_values,
    calculate_rsi_pinescript, calculate_sma, calculate_wma, calculate_ema
)

from scripts.calculate_the_number_indicators import (
    calculate_the_number_full, get_the_number_current_values
)

from scripts.calculate_stamp_indicators import (
    calculate_stamp_components, get_stamp_current_values, 
    calculate_rsi_on_shifted_series
)

from scripts.calculate_trend_screener_indicators import (
    calculate_trend_components, calculate_weekly_components,
    calculate_trend_conditions, get_trend_current_values,
    calculate_cci_pinescript_exact, calculate_aroon_pinescript_exact
)


class IndicatorsDataService:
    """خدمة موحدة لجمع وحساب جميع المؤشرات"""
    
    @staticmethod
    def prepare_price_dataframe(rows: List) -> Optional[pd.DataFrame]:
        """تحويل بيانات الأسعار إلى DataFrame"""
        if not rows or len(rows) < 100:
            return None
        
        df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close'])
        df['date'] = pd.to_datetime(df['date'])
        df['open'] = df['open'].apply(convert_to_float)
        df['high'] = df['high'].apply(convert_to_float)
        df['low'] = df['low'].apply(convert_to_float)
        df['close'] = df['close'].apply(convert_to_float)
        df.dropna(subset=['close'], inplace=True)
        df.set_index('date', inplace=True)
        df.sort_index(inplace=True)
        
        # فلترة أيام العطلات (عندما يكرر مزود البيانات نفس الشمعة السابقة تماماً)
        # هذا ضروري جداً لتطابق المؤشرات مع TradingView
        columns_to_check = ['open', 'high', 'low', 'close']
        mask = (df[columns_to_check] != df[columns_to_check].shift(1)).any(axis=1)
        df = df[mask]
        
        return df if len(df) >= 100 else None
    
    @staticmethod
    def prepare_weekly_dataframe(df: pd.DataFrame) -> Optional[pd.DataFrame]:
        """
        تحويل البيانات إلى إطار زمني أسبوعي (إغلاق الجمعة)
        وحساب المؤشرات الأسبوعية بشكل كامل
        """
        # تجميع البيانات أسبوعياً (W-THU = إغلاق يوم الخميس للسوق السعودي/الخليجي)
        df_weekly = df.resample('W-THU').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last'
        }).dropna()
        
        if len(df_weekly) < 20:
            return None
            
        # حفظ تاريخ آخر يوم تداول فعلي في الأسبوع حتى نتمكن من دمج الأسبوع الحالي بشكل صحيح عند الدمج مع اليومي
        # هذا يحل مشكلة يوم الأحد، حيث يكون الشمعة الأسبوعية بـ(تاريخ الأحد) وليس (الخميس القادم) 
        weekly_dates = df.index.to_series().resample('W-THU').max()
        df_weekly.index = weekly_dates.loc[df_weekly.index]
        # expose weekly close with '_w' suffix so merged dataframe contains it
        df_weekly['close_w'] = df_weekly['close']
        
        # استخراج السلاسل الزمنية الأسبوعية
        closes_w = df_weekly['close'].tolist()
        highs_w = df_weekly['high'].tolist()
        lows_w = df_weekly['low'].tolist()
        
        # ===== حساب جميع المؤشرات الأسبوعية =====
        
        # 1. RSI Weekly Components
        rsi_w = calculate_rsi_pinescript(closes_w, 14)
        rsi_3_w = calculate_rsi_pinescript(closes_w, 3)
        sma9_rsi_w = calculate_sma(rsi_w, 9)
        wma45_rsi_w = calculate_wma(rsi_w, 45)
        ema45_rsi_w = calculate_ema(rsi_w, 45)
        sma3_rsi3_w = calculate_sma(rsi_3_w, 3)
        ema20_sma3_w = calculate_ema(sma3_rsi3_w, 20)
        
        # 2. The Number Weekly
        tn_components_w = calculate_the_number_full(highs_w, lows_w, closes_w)
        
        # 3. STAMP Weekly (CFG)
        # حساب CFG الأسبوعي: RSI14 - RSI14[9] + SMA(RSI3, 3)
        a_values_w = []
        for i in range(len(rsi_w)):
            if i < 9 or rsi_w[i] is None or rsi_w[i-9] is None or sma3_rsi3_w[i] is None:
                a_values_w.append(None)
            else:
                a_values_w.append(rsi_w[i] - rsi_w[i-9] + sma3_rsi3_w[i])
        
        cfg_w_series = a_values_w
        cfg_w_sma4 = calculate_sma(cfg_w_series, 4)
        cfg_w_sma9 = calculate_sma(cfg_w_series, 9)
        cfg_w_sma20 = calculate_sma(cfg_w_series, 20)
        cfg_w_ema20 = calculate_ema(cfg_w_series, 20)
        cfg_w_ema45 = calculate_ema(cfg_w_series, 45)
        cfg_w_wma45 = calculate_wma(cfg_w_series, 45)
        
        # 4. Trend Weekly Components
        trend_w_components = calculate_trend_components(highs_w, lows_w, closes_w)
        
        # إضافة جميع السلاسل الأسبوعية إلى DataFrame
        df_weekly['rsi_w'] = pd.Series(rsi_w, index=df_weekly.index)
        df_weekly['rsi_3_w'] = pd.Series(rsi_3_w, index=df_weekly.index)
        df_weekly['sma9_rsi_w'] = pd.Series(sma9_rsi_w, index=df_weekly.index)
        df_weekly['wma45_rsi_w'] = pd.Series(wma45_rsi_w, index=df_weekly.index)
        df_weekly['ema45_rsi_w'] = pd.Series(ema45_rsi_w, index=df_weekly.index)
        df_weekly['sma3_rsi3_w'] = pd.Series(sma3_rsi3_w, index=df_weekly.index)
        df_weekly['ema20_sma3_w'] = pd.Series(ema20_sma3_w, index=df_weekly.index)
        
        # The Number Weekly
        df_weekly['sma9_close_w'] = pd.Series(tn_components_w['sma9_close'], index=df_weekly.index)
        df_weekly['the_number_w'] = pd.Series(tn_components_w['the_number'], index=df_weekly.index)
        df_weekly['the_number_hl_w'] = pd.Series(tn_components_w['the_number_hl'], index=df_weekly.index)
        df_weekly['the_number_ll_w'] = pd.Series(tn_components_w['the_number_ll'], index=df_weekly.index)
        df_weekly['high_sma13_w'] = pd.Series(tn_components_w['high_sma13'], index=df_weekly.index)
        df_weekly['low_sma13_w'] = pd.Series(tn_components_w['low_sma13'], index=df_weekly.index)
        df_weekly['high_sma65_w'] = pd.Series(tn_components_w['high_sma65'], index=df_weekly.index)
        df_weekly['low_sma65_w'] = pd.Series(tn_components_w['low_sma65'], index=df_weekly.index)
        
        # CFG Weekly
        df_weekly['cfg_w'] = pd.Series(cfg_w_series, index=df_weekly.index)
        df_weekly['cfg_sma4_w'] = pd.Series(cfg_w_sma4, index=df_weekly.index)
        df_weekly['cfg_sma9_w'] = pd.Series(cfg_w_sma9, index=df_weekly.index)
        df_weekly['cfg_sma20_w'] = pd.Series(cfg_w_sma20, index=df_weekly.index)
        df_weekly['cfg_ema20_w'] = pd.Series(cfg_w_ema20, index=df_weekly.index)
        df_weekly['cfg_ema45_w'] = pd.Series(cfg_w_ema45, index=df_weekly.index)
        df_weekly['cfg_wma45_w'] = pd.Series(cfg_w_wma45, index=df_weekly.index)
        
        # STAMP Weekly Components - إضافة حقول STAMP الأسبوعية المفقودة
        rsi_14_9days_ago_w = []
        for i in range(len(rsi_w)):
            rsi_14_9days_ago_w.append(rsi_w[i-9] if i >= 9 and rsi_w[i-9] is not None else None)
        
        df_weekly['rsi_14_9days_ago_w'] = pd.Series(rsi_14_9days_ago_w, index=df_weekly.index)

        # CFG Term 1 (Weekly): RSI14_w - RSI14_w[9].  Daily twin is 'rsi_14_minus_9'
        # (current_rsi['rsi_14'] - current_stamp['rsi_14_9days_ago']).  Together with
        # sma3_rsi3_w this rebuilds cfg_w, so cfg_w - sma3_rsi3_w must equal this column.
        rsi_14_minus_9_w = []
        for i in range(len(rsi_w)):
            if rsi_w[i] is None or rsi_14_9days_ago_w[i] is None:
                rsi_14_minus_9_w.append(None)
            else:
                rsi_14_minus_9_w.append(rsi_w[i] - rsi_14_9days_ago_w[i])
        df_weekly['rsi_14_minus_9_w'] = pd.Series(rsi_14_minus_9_w, index=df_weekly.index)

        # ta.rsi(close[9], 14) Weekly. Pine shifts the series, so this is the weekly RSI14
        # read 9 bars back — the per-row form of calculate_rsi_on_shifted_series(closes_w, 14, 9),
        # which is by definition the same number as rsi_14_9days_ago_w (the daily pair
        # rsi_14_9days_ago_cfg / rsi_14_9days_ago behaves exactly the same way).
        df_weekly['rsi_14_w_shifted'] = pd.Series(rsi_14_9days_ago_w, index=df_weekly.index)
        df_weekly['stamp_a_value_w'] = pd.Series(cfg_w_series, index=df_weekly.index)  # A = CFG
        df_weekly['stamp_s9rsi_w'] = pd.Series(sma9_rsi_w, index=df_weekly.index)  # SMA9(RSI14)
        df_weekly['stamp_e45cfg_w'] = pd.Series(cfg_w_ema45, index=df_weekly.index)  # EMA45(CFG)
        df_weekly['stamp_e45rsi_w'] = pd.Series(ema45_rsi_w, index=df_weekly.index)  # EMA45(RSI14)
        df_weekly['stamp_e20sma3_w'] = pd.Series(ema20_sma3_w, index=df_weekly.index)  # EMA20(SMA3(RSI3))
        
        # Trend Weekly
        df_weekly['sma4_w'] = pd.Series(trend_w_components['sma4'], index=df_weekly.index)
        df_weekly['sma9_w'] = pd.Series(trend_w_components['sma9'], index=df_weekly.index)
        df_weekly['sma18_w'] = pd.Series(trend_w_components['sma18'], index=df_weekly.index)
        df_weekly['wma45_close_w'] = pd.Series(trend_w_components['wma45_close'], index=df_weekly.index)
        df_weekly['cci_w'] = pd.Series(trend_w_components['cci'], index=df_weekly.index)
        df_weekly['cci_ema20_w'] = pd.Series(trend_w_components['cci_ema20'], index=df_weekly.index)
        df_weekly['aroon_up_w'] = pd.Series(trend_w_components['aroon_up'], index=df_weekly.index)
        df_weekly['aroon_down_w'] = pd.Series(trend_w_components['aroon_down'], index=df_weekly.index)
        
        # Minervini specific weekly SMAs (30w and 40w)
        df_weekly['sma_30w'] = df_weekly['close_w'].rolling(window=30).mean()
        df_weekly['sma_40w'] = df_weekly['close_w'].rolling(window=40).mean()
        
        return df_weekly
    
    @staticmethod
    def merge_weekly_with_daily(df_daily: pd.DataFrame, df_weekly: pd.DataFrame) -> pd.DataFrame:
        """
        دمج المؤشرات الأسبوعية مع البيانات اليومية
        استخدام forward fill لضمان توفر القيم الأسبوعية الصحيحة لكل يوم
        
        في Pine Script، request.security(timeframe, ...) يحصل على قيمة الأسبوع الحالي
        قيد التشكيل، لذلك نستخدم forward fill لتطبيق قيم الأسبوع على جميع أيام الأسبوع
        """
        # الحصول على أعمدة البيانات الأسبوعية فقط
        # 'rsi_14_w_shifted' يجب أن يُذكر صراحة: اسم العمود ينتهي بـ _shifted وليس _w،
        # فاختبار endswith('_w') وحده كان يسقطه من df_merged → العمود بقي NULL دائماً
        # رغم أن قيمته تُحسب في prepare_weekly_dataframe (نفس سبب موت sma_30w/sma_40w).
        weekly_extra = ('sma_30w', 'sma_40w', 'rsi_14_w_shifted')
        weekly_cols = [col for col in df_weekly.columns if col.endswith('_w') or col in weekly_extra]
        
        # إعادة تعيين مؤشر البيانات الأسبوعية ليطابق التواريخ اليومية
        # forward fill يضمن أن كل يوم يحصل على آخر قيمة أسبوعية معروفة
        weekly_aligned = df_weekly[weekly_cols].reindex(df_daily.index, method='ffill')
        
        # دمج البيانات اليومية مع البيانات الأسبوعية المعاد تعيينها
        result = df_daily.join(weekly_aligned)
        
        return result
    
    @staticmethod
    def verify_weekly_data_alignment(df_daily: pd.DataFrame, df_weekly: pd.DataFrame, idx: int) -> Dict[str, Any]:
        """
        التحقق من أن بيانات الأسبوع محاذاة بشكل صحيح في الموضع المحدد
        
        Args:
            df_daily: DataFrame يومي
            df_weekly: DataFrame أسبوعي
            idx: الموضع الحالي في البيانات اليومية
            
        Returns:
            قاموس يحتوي على معلومات المحاذاة والتحقق
        """
        if idx < 0 or idx >= len(df_daily):
            return {'valid': False, 'error': 'Invalid index'}
        
        current_date = df_daily.index[idx]
        current_week_start = current_date - pd.Timedelta(days=current_date.weekday())
        
        # العثور على أحدث تاريخ أسبوعي <= التاريخ الحالي
        weekly_dates = df_weekly.index.tolist()
        if not weekly_dates:
            return {'valid': False, 'error': 'No weekly data'}
        
        # الحصول على آخر تاريخ أسبوعي معروف
        applicable_weekly_dates = [d for d in weekly_dates if d <= current_date]
        if not applicable_weekly_dates:
            return {'valid': False, 'error': 'No applicable weekly date'}
        
        latest_weekly_date = max(applicable_weekly_dates)
        
        return {
            'valid': True,
            'current_date': current_date,
            'current_week_start': current_week_start,
            'latest_weekly_date': latest_weekly_date,
            'days_since_weekly_update': (current_date - latest_weekly_date).days,
        }
    
    @staticmethod
    def calculate_all_indicators(
        df: pd.DataFrame,
        df_weekly: pd.DataFrame,
        symbol: str,
        target_date: date = None,
        idx: int = None,
        w_idx: int = None
    ) -> Dict[str, Any]:
        """
        حساب جميع المؤشرات من جميع الملفات الأربعة
        """
        
        # دمج البيانات الأسبوعية مع اليومية
        df_merged = IndicatorsDataService.merge_weekly_with_daily(df, df_weekly)
        
        # --- البيانات اليومية ---
        closes = df['close'].tolist()
        highs = df['high'].tolist()
        lows = df['low'].tolist()
        
        # --- 1. RSI Components ---
        rsi_components = calculate_rsi_components(closes)
        
        # --- 2. The Number Components ---
        the_number_components = calculate_the_number_full(highs, lows, closes)
        
        # --- 3. STAMP & CFG Components (محدث) ---
        # حساب Stamp باستخدام الدالة المحدثة
        rsi14 = calculate_rsi_pinescript(closes, 14)
        rsi3 = calculate_rsi_pinescript(closes, 3)
        sma3_rsi3 = calculate_sma(rsi3, 3)
        
        # حساب a = rsi14 - rsi14[9] + sma(rsi3, 3)
        a_values = []
        for i in range(len(rsi14)):
            if i < 9 or rsi14[i] is None or rsi14[i-9] is None or sma3_rsi3[i] is None:
                a_values.append(None)
            else:
                a_values.append(rsi14[i] - rsi14[i-9] + sma3_rsi3[i])
        
        stamp_components = {
            'sma9_rsi': rsi_components['sma9_rsi'],
            'ema45_rsi': rsi_components['ema45_rsi'],
            'sma3_rsi3': sma3_rsi3,
            'ema20_sma3': rsi_components['ema20_sma3'],
            'cfg_series': a_values,
            'cfg_sma4': calculate_sma(a_values, 4),
            'cfg_ema45': calculate_ema(a_values, 45),
            'cfg_ema20': calculate_ema(a_values, 20),
            'cfg_sma9': calculate_sma(a_values, 9),
            'cfg_sma20': calculate_sma(a_values, 20),
            'cfg_wma45': calculate_wma(a_values, 45),
        }
        
        # --- 4. Trend Components (Daily) ---
        trend_components = calculate_trend_components(highs, lows, closes, symbol)
        
        # --- تحديد المؤشر الحالي ---
        if idx is None:
            idx = len(df) - 1
        if w_idx is None:
            w_idx = len(df_weekly) - 1
        
        # --- 5. Current Values from all components ---
        current_rsi = get_rsi_current_values(rsi_components, idx)
        current_the_number = get_the_number_current_values(the_number_components, idx)
        
        # Stamp current values
        current_stamp = {
            'rsi_14_9days_ago': rsi14[idx-9] if idx >= 9 and rsi14[idx] is not None else None,
            'sma3_rsi3': sma3_rsi3[idx] if idx < len(sma3_rsi3) else None,
            'stamp_a_value': a_values[idx] if idx < len(a_values) else None,
            'cfg_daily': a_values[idx] if idx < len(a_values) else None,
            'cfg_sma4': stamp_components['cfg_sma4'][idx] if idx < len(stamp_components['cfg_sma4']) else None,
            'cfg_sma9': stamp_components['cfg_sma9'][idx] if idx < len(stamp_components['cfg_sma9']) else None,
            'cfg_sma20': stamp_components['cfg_sma20'][idx] if idx < len(stamp_components['cfg_sma20']) else None,
            'cfg_ema20': stamp_components['cfg_ema20'][idx] if idx < len(stamp_components['cfg_ema20']) else None,
            'cfg_ema45': stamp_components['cfg_ema45'][idx] if idx < len(stamp_components['cfg_ema45']) else None,
            'cfg_wma45': stamp_components['cfg_wma45'][idx] if idx < len(stamp_components['cfg_wma45']) else None,
            'stamp_s9rsi': rsi_components['sma9_rsi'][idx] if idx < len(rsi_components['sma9_rsi']) else None,
            'stamp_e45cfg': stamp_components['cfg_ema45'][idx] if idx < len(stamp_components['cfg_ema45']) else None,
            'stamp_e45rsi': rsi_components['ema45_rsi'][idx] if idx < len(rsi_components['ema45_rsi']) else None,
            'stamp_e20sma3': rsi_components['ema20_sma3'][idx] if idx < len(rsi_components['ema20_sma3']) else None,
        }
        
        current_trend = get_trend_current_values(trend_components, {}, idx)
        
        # --- 6. RSI Screener Conditions ---
        # STAMP Conditions - Daily
        cond_stamp_1_d = False
        sma9_close_val = get_val(the_number_components['sma9_close'], idx)
        wma45_close_val = get_val(trend_components['wma45_close'], idx)
        if sma9_close_val is not None and wma45_close_val is not None:
            cond_stamp_1_d = sma9_close_val > wma45_close_val
        
        cond_stamp_2_d = False
        sma9_rsi_val = get_val(rsi_components['sma9_rsi'], idx)
        wma45_rsi_val = get_val(rsi_components['wma45_rsi'], idx)
        if sma9_rsi_val is not None and wma45_rsi_val is not None:
            cond_stamp_2_d = sma9_rsi_val > wma45_rsi_val
        
        cond_stamp_3_d = get_val(rsi_components['ema45_rsi'], idx) is not None and get_val(rsi_components['ema45_rsi'], idx) > 50
        cond_stamp_4_d = stamp_components['cfg_ema45'][idx] is not None and stamp_components['cfg_ema45'][idx] > 50 if idx < len(stamp_components['cfg_ema45']) else False
        cond_stamp_5_d = get_val(rsi_components['ema20_sma3'], idx) is not None and get_val(rsi_components['ema20_sma3'], idx) > 50
        
        stamp_daily = cond_stamp_1_d and cond_stamp_2_d and cond_stamp_3_d and cond_stamp_4_d and cond_stamp_5_d
        
        # STAMP Conditions - Weekly
        cond_stamp_1_w = False
        sma9_close_w_val = df_merged.iloc[idx]['sma9_close_w'] if 'sma9_close_w' in df_merged.columns else None
        wma45_close_w_val = df_merged.iloc[idx]['wma45_close_w'] if 'wma45_close_w' in df_merged.columns else None
        if sma9_close_w_val is not None and wma45_close_w_val is not None and not pd.isna(sma9_close_w_val) and not pd.isna(wma45_close_w_val):
            cond_stamp_1_w = sma9_close_w_val > wma45_close_w_val
        
        cond_stamp_2_w = False
        sma9_rsi_w_val = df_merged.iloc[idx]['sma9_rsi_w'] if 'sma9_rsi_w' in df_merged.columns else None
        wma45_rsi_w_val = df_merged.iloc[idx]['wma45_rsi_w'] if 'wma45_rsi_w' in df_merged.columns else None
        if sma9_rsi_w_val is not None and wma45_rsi_w_val is not None and not pd.isna(sma9_rsi_w_val) and not pd.isna(wma45_rsi_w_val):
            cond_stamp_2_w = sma9_rsi_w_val > wma45_rsi_w_val
        
        cond_stamp_3_w = False
        ema45_rsi_w_val = df_merged.iloc[idx]['ema45_rsi_w'] if 'ema45_rsi_w' in df_merged.columns else None
        if ema45_rsi_w_val is not None and not pd.isna(ema45_rsi_w_val):
            cond_stamp_3_w = ema45_rsi_w_val > 50
        
        cond_stamp_4_w = False
        cfg_ema45_w_val = df_merged.iloc[idx]['cfg_ema45_w'] if 'cfg_ema45_w' in df_merged.columns else None
        if cfg_ema45_w_val is not None and not pd.isna(cfg_ema45_w_val):
            cond_stamp_4_w = cfg_ema45_w_val > 50
        
        cond_stamp_5_w = False
        ema20_sma3_w_val = df_merged.iloc[idx]['ema20_sma3_w'] if 'ema20_sma3_w' in df_merged.columns else None
        if ema20_sma3_w_val is not None and not pd.isna(ema20_sma3_w_val):
            cond_stamp_5_w = ema20_sma3_w_val > 50
        
        stamp_weekly = cond_stamp_1_w and cond_stamp_2_w and cond_stamp_3_w and cond_stamp_4_w and cond_stamp_5_w
        stamp = stamp_daily and stamp_weekly
        
        # RSI Screener Conditions
        sma9_gt_tn_daily = False
        sma9_close_val = get_val(the_number_components['sma9_close'], idx)
        the_number_val = get_val(the_number_components['the_number'], idx)
        if sma9_close_val is not None and the_number_val is not None:
            sma9_gt_tn_daily = sma9_close_val > the_number_val
        
        sma9_gt_tn_weekly = False
        sma9_close_w_val = df_merged.iloc[idx]['sma9_close_w'] if 'sma9_close_w' in df_merged.columns else None
        the_number_w_val = df_merged.iloc[idx]['the_number_w'] if 'the_number_w' in df_merged.columns else None
        if sma9_close_w_val is not None and the_number_w_val is not None and not pd.isna(sma9_close_w_val) and not pd.isna(the_number_w_val):
            sma9_gt_tn_weekly = sma9_close_w_val > the_number_w_val
        
        rsi_lt_80_d = get_val(rsi_components['rsi_14'], idx) is not None and get_val(rsi_components['rsi_14'], idx) < 80
        
        rsi_lt_80_w = False
        rsi_w_val = df_merged.iloc[idx]['rsi_w'] if 'rsi_w' in df_merged.columns else None
        if rsi_w_val is not None and not pd.isna(rsi_w_val):
            rsi_lt_80_w = rsi_w_val < 80
        
        sma9_rsi_lte_75_d = get_val(rsi_components['sma9_rsi'], idx) is not None and get_val(rsi_components['sma9_rsi'], idx) <= 75
        
        sma9_rsi_lte_75_w = False
        sma9_rsi_w_val = df_merged.iloc[idx]['sma9_rsi_w'] if 'sma9_rsi_w' in df_merged.columns else None
        if sma9_rsi_w_val is not None and not pd.isna(sma9_rsi_w_val):
            sma9_rsi_lte_75_w = sma9_rsi_w_val <= 75
        
        ema45_rsi_lte_70_d = get_val(rsi_components['ema45_rsi'], idx) is not None and get_val(rsi_components['ema45_rsi'], idx) <= 70
        
        ema45_rsi_lte_70_w = False
        ema45_rsi_w_val = df_merged.iloc[idx]['ema45_rsi_w'] if 'ema45_rsi_w' in df_merged.columns else None
        if ema45_rsi_w_val is not None and not pd.isna(ema45_rsi_w_val):
            ema45_rsi_lte_70_w = ema45_rsi_w_val <= 70
        
        rsi_55_70 = False
        rsi_val = get_val(rsi_components['rsi_14'], idx)
        if rsi_val is not None:
            rsi_55_70 = 55 <= rsi_val <= 70
        
        rsi_gt_wma45_d = False
        rsi_val = get_val(rsi_components['rsi_14'], idx)
        wma45_rsi_val = get_val(rsi_components['wma45_rsi'], idx)
        if rsi_val is not None and wma45_rsi_val is not None:
            rsi_gt_wma45_d = rsi_val > wma45_rsi_val
        
        rsi_gt_wma45_w = False
        rsi_w_val = df_merged.iloc[idx]['rsi_w'] if 'rsi_w' in df_merged.columns else None
        wma45_rsi_w_val = df_merged.iloc[idx]['wma45_rsi_w'] if 'wma45_rsi_w' in df_merged.columns else None
        if rsi_w_val is not None and wma45_rsi_w_val is not None and not pd.isna(rsi_w_val) and not pd.isna(wma45_rsi_w_val):
            rsi_gt_wma45_w = rsi_w_val > wma45_rsi_w_val
        
        sma9rsi_gt_wma45rsi_d = False
        sma9_rsi_val = get_val(rsi_components['sma9_rsi'], idx)
        wma45_rsi_val = get_val(rsi_components['wma45_rsi'], idx)
        if sma9_rsi_val is not None and wma45_rsi_val is not None:
            sma9rsi_gt_wma45rsi_d = sma9_rsi_val > wma45_rsi_val
        
        sma9rsi_gt_wma45rsi_w = False
        sma9_rsi_w_val = df_merged.iloc[idx]['sma9_rsi_w'] if 'sma9_rsi_w' in df_merged.columns else None
        wma45_rsi_w_val = df_merged.iloc[idx]['wma45_rsi_w'] if 'wma45_rsi_w' in df_merged.columns else None
        if sma9_rsi_w_val is not None and wma45_rsi_w_val is not None and not pd.isna(sma9_rsi_w_val) and not pd.isna(wma45_rsi_w_val):
            sma9rsi_gt_wma45rsi_w = sma9_rsi_w_val > wma45_rsi_w_val
        
        # Final Signal
        final_signal = (
            stamp and
            sma9_gt_tn_daily and sma9_gt_tn_weekly and
            rsi_lt_80_d and rsi_lt_80_w and
            sma9_rsi_lte_75_d and sma9_rsi_lte_75_w and
            ema45_rsi_lte_70_d and ema45_rsi_lte_70_w and
            rsi_55_70 and
            rsi_gt_wma45_d and rsi_gt_wma45_w and
            sma9rsi_gt_wma45rsi_d and sma9rsi_gt_wma45rsi_w
        )
        
        # Score
        conditions = [
            stamp_daily, stamp_weekly,
            sma9_gt_tn_daily, sma9_gt_tn_weekly,
            rsi_lt_80_d, rsi_lt_80_w,
            sma9_rsi_lte_75_d, sma9_rsi_lte_75_w,
            ema45_rsi_lte_70_d, ema45_rsi_lte_70_w,
            rsi_55_70,
            rsi_gt_wma45_d, rsi_gt_wma45_w,
            sma9rsi_gt_wma45rsi_d, sma9rsi_gt_wma45rsi_w
        ]
        score = sum(1 for c in conditions if c)
        
        # --- 7. Trend Conditions ---
        trend_conditions = calculate_trend_conditions(
            trend_components,
            {},  # سنستخدم df_merged بدلاً من ذلك
            idx,
            w_idx,
            symbol,
            df
        )
        
        # تحديث شروط التريند باستخدام القيم الأسبوعية من df_merged
        price_gt_sma9_weekly = False
        close_w_val = df_merged.iloc[idx]['close_w'] if 'close_w' in df_merged.columns else None
        sma9_w_val = df_merged.iloc[idx]['sma9_w'] if 'sma9_w' in df_merged.columns else None
        if close_w_val is not None and sma9_w_val is not None and not pd.isna(close_w_val) and not pd.isna(sma9_w_val):
            price_gt_sma9_weekly = close_w_val > sma9_w_val
        
        sma_trend_weekly = False
        sma4_w_val = df_merged.iloc[idx]['sma4_w'] if 'sma4_w' in df_merged.columns else None
        sma9_w_val2 = df_merged.iloc[idx]['sma9_w'] if 'sma9_w' in df_merged.columns else None
        sma18_w_val = df_merged.iloc[idx]['sma18_w'] if 'sma18_w' in df_merged.columns else None
        if all(v is not None and not pd.isna(v) for v in [sma4_w_val, sma9_w_val2, sma18_w_val]):
            sma_trend_weekly = sma4_w_val > sma9_w_val2 and sma9_w_val2 > sma18_w_val
        
        cci_ema20_gt_0_weekly = False
        cci_ema20_w_val = df_merged.iloc[idx]['cci_ema20_w'] if 'cci_ema20_w' in df_merged.columns else None
        if cci_ema20_w_val is not None and not pd.isna(cci_ema20_w_val):
            cci_ema20_gt_0_weekly = cci_ema20_w_val > 0
        
        # تحديث trend_conditions
        trend_conditions['price_gt_sma9_weekly'] = price_gt_sma9_weekly
        trend_conditions['sma_trend_weekly'] = sma_trend_weekly
        trend_conditions['cci_ema20_gt_0_weekly'] = cci_ema20_gt_0_weekly
        
        # إعادة حساب trend_signal
        trend_conditions['trend_signal'] = (
            trend_conditions['price_gt_sma18'] and 
            price_gt_sma9_weekly and
            trend_conditions['sma_trend_daily'] and 
            sma_trend_weekly and
            trend_conditions['cci_gt_100'] and 
            trend_conditions['cci_ema20_gt_0_daily'] and 
            cci_ema20_gt_0_weekly and
            trend_conditions['aroon_up_gt_70'] and 
            trend_conditions['aroon_down_lt_30'] and
            not trend_conditions['is_etf_or_index'] and 
            not trend_conditions['has_gap']
        )
        
        # --- 8. Build Complete Result Dictionary ---
        result = {
            # Price
            'close': get_val(closes, idx),
            
            # ===== 1. RSI Indicator =====
            'rsi_14': current_rsi['rsi_14'],
            'rsi_3': current_rsi['rsi_3'],
            'sma9_rsi': current_rsi['sma9_rsi'],
            'wma45_rsi': current_rsi['wma45_rsi'],
            'ema45_rsi': current_rsi['ema45_rsi'],
            'sma3_rsi3': current_rsi['sma3_rsi3'],
            'ema20_sma3': current_rsi['ema20_sma3'],
            
            # ===== 2. The Number =====
            'sma9_close': current_the_number['sma9_close'],
            'the_number': current_the_number['the_number'],
            'the_number_hl': current_the_number['the_number_hl'],
            'the_number_ll': current_the_number['the_number_ll'],
            'high_sma13': get_val(the_number_components['high_sma13'], idx),
            'low_sma13': get_val(the_number_components['low_sma13'], idx),
            'high_sma65': get_val(the_number_components['high_sma65'], idx),
            'low_sma65': get_val(the_number_components['low_sma65'], idx),
            
            # ===== 3. Stamp Indicator =====
            'rsi_14_9days_ago': current_stamp['rsi_14_9days_ago'],
            'stamp_a_value': current_stamp['stamp_a_value'],
            'stamp_s9rsi': current_stamp['stamp_s9rsi'],
            'stamp_e45cfg': current_stamp['stamp_e45cfg'],
            'stamp_e45rsi': current_stamp['stamp_e45rsi'],
            'stamp_e20sma3': current_stamp['stamp_e20sma3'],
            
            # ===== 4. CFG Analysis =====
            'cfg_daily': current_stamp['cfg_daily'],
            'cfg_sma4': current_stamp.get('cfg_sma4', None),
            'cfg_sma9': current_stamp['cfg_sma9'],
            'cfg_sma20': current_stamp['cfg_sma20'],
            'cfg_ema20': current_stamp['cfg_ema20'],
            'cfg_ema45': current_stamp['cfg_ema45'],
            'cfg_wma45': current_stamp.get('cfg_wma45', None),
            
            # CFG Components
            'rsi_14_9days_ago_cfg': calculate_rsi_on_shifted_series(closes, 14, 9),
            'rsi_14_minus_9': (current_rsi['rsi_14'] - current_stamp['rsi_14_9days_ago']) if all(v is not None for v in [current_rsi['rsi_14'], current_stamp['rsi_14_9days_ago']]) else None,
            
            # CFG Conditions
            'cfg_gt_50_daily': current_stamp['cfg_daily'] is not None and current_stamp['cfg_daily'] > 50 if current_stamp['cfg_daily'] is not None else False,
            'cfg_ema45_gt_50': current_stamp['cfg_ema45'] is not None and current_stamp['cfg_ema45'] > 50 if current_stamp['cfg_ema45'] is not None else False,
            'cfg_ema20_gt_50': current_stamp['cfg_ema20'] is not None and current_stamp['cfg_ema20'] > 50 if current_stamp['cfg_ema20'] is not None else False,
            
            # ===== 5. Trend Screener =====
            # Store PineScript-exact EMA values directly in stock_indicators
            'ema10': current_trend['ema10'],
            'ema21': current_trend['ema21'],
            'sma4': current_trend['sma4'],
            'sma9': current_trend['sma9'],
            'sma18': current_trend['sma18'],
            'wma45_close': current_trend['wma45_close'],
            'cci': current_trend['cci'],
            'cci_ema20': current_trend['cci_ema20'],
            'aroon_up': current_trend['aroon_up'],
            'aroon_down': current_trend['aroon_down'],
            
            # Trend Conditions
            'price_gt_sma18': trend_conditions['price_gt_sma18'],
            'price_gt_sma9_weekly': trend_conditions['price_gt_sma9_weekly'],
            'sma_trend_daily': trend_conditions['sma_trend_daily'],
            'sma_trend_weekly': trend_conditions['sma_trend_weekly'],
            'cci_gt_100': trend_conditions['cci_gt_100'],
            'cci_ema20_gt_0_daily': trend_conditions['cci_ema20_gt_0_daily'],
            'cci_ema20_gt_0_weekly': trend_conditions['cci_ema20_gt_0_weekly'],
            'aroon_up_gt_70': trend_conditions['aroon_up_gt_70'],
            'aroon_down_lt_30': trend_conditions['aroon_down_lt_30'],
            'is_etf_or_index': trend_conditions['is_etf_or_index'],
            'has_gap': trend_conditions['has_gap'],
            'trend_signal': trend_conditions['trend_signal'],
            
            # ✅ MA COMPARISON CONDITIONS
            'ema10_gt_sma50': trend_conditions.get('ema10_gt_sma50', False),
            'ema10_gt_sma200': trend_conditions.get('ema10_gt_sma200', False),
            'ema21_gt_sma50': trend_conditions.get('ema21_gt_sma50', False),
            'ema21_gt_sma200': trend_conditions.get('ema21_gt_sma200', False),
            'sma50_gt_sma150': trend_conditions.get('sma50_gt_sma150', False),
            'sma50_gt_sma200': trend_conditions.get('sma50_gt_sma200', False),
            'sma150_gt_sma200': trend_conditions.get('sma150_gt_sma200', False),
            
            # ✅ 200SMA TREND CONDITIONS
            'sma200_gt_sma200_1m_ago': trend_conditions.get('sma200_gt_sma200_1m_ago', False),
            'sma200_gt_sma200_2m_ago': trend_conditions.get('sma200_gt_sma200_2m_ago', False),
            'sma200_gt_sma200_3m_ago': trend_conditions.get('sma200_gt_sma200_3m_ago', False),
            'sma200_gt_sma200_4m_ago': trend_conditions.get('sma200_gt_sma200_4m_ago', False),
            'sma200_gt_sma200_5m_ago': trend_conditions.get('sma200_gt_sma200_5m_ago', False),
            
            # ===== 6. Weekly Values =====
            # RSI Weekly
            'rsi_w': df_merged.iloc[idx]['rsi_w'] if 'rsi_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['rsi_w']) else None,
            'rsi_3_w': df_merged.iloc[idx]['rsi_3_w'] if 'rsi_3_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['rsi_3_w']) else None,
            'sma3_rsi3_w': df_merged.iloc[idx]['sma3_rsi3_w'] if 'sma3_rsi3_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['sma3_rsi3_w']) else None,
            'sma9_rsi_w': df_merged.iloc[idx]['sma9_rsi_w'] if 'sma9_rsi_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['sma9_rsi_w']) else None,
            'wma45_rsi_w': df_merged.iloc[idx]['wma45_rsi_w'] if 'wma45_rsi_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['wma45_rsi_w']) else None,
            'ema45_rsi_w': df_merged.iloc[idx]['ema45_rsi_w'] if 'ema45_rsi_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['ema45_rsi_w']) else None,
            'ema20_sma3_w': df_merged.iloc[idx]['ema20_sma3_w'] if 'ema20_sma3_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['ema20_sma3_w']) else None,
            
            # The Number Weekly
            'sma9_close_w': df_merged.iloc[idx]['sma9_close_w'] if 'sma9_close_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['sma9_close_w']) else None,
            'the_number_w': df_merged.iloc[idx]['the_number_w'] if 'the_number_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['the_number_w']) else None,
            'the_number_hl_w': df_merged.iloc[idx]['the_number_hl_w'] if 'the_number_hl_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['the_number_hl_w']) else None,
            'the_number_ll_w': df_merged.iloc[idx]['the_number_ll_w'] if 'the_number_ll_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['the_number_ll_w']) else None,
            'high_sma13_w': df_merged.iloc[idx]['high_sma13_w'] if 'high_sma13_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['high_sma13_w']) else None,
            'low_sma13_w': df_merged.iloc[idx]['low_sma13_w'] if 'low_sma13_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['low_sma13_w']) else None,
            'high_sma65_w': df_merged.iloc[idx]['high_sma65_w'] if 'high_sma65_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['high_sma65_w']) else None,
            'low_sma65_w': df_merged.iloc[idx]['low_sma65_w'] if 'low_sma65_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['low_sma65_w']) else None,
            
            # CFG Weekly
            'cfg_w': df_merged.iloc[idx]['cfg_w'] if 'cfg_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['cfg_w']) else None,
            'cfg_sma4_w': df_merged.iloc[idx]['cfg_sma4_w'] if 'cfg_sma4_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['cfg_sma4_w']) else None,
            'cfg_sma9_w': df_merged.iloc[idx]['cfg_sma9_w'] if 'cfg_sma9_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['cfg_sma9_w']) else None,
            'cfg_sma20_w': df_merged.iloc[idx]['cfg_sma20_w'] if 'cfg_sma20_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['cfg_sma20_w']) else None,
            'cfg_ema20_w': df_merged.iloc[idx]['cfg_ema20_w'] if 'cfg_ema20_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['cfg_ema20_w']) else None,
            'cfg_ema45_w': df_merged.iloc[idx]['cfg_ema45_w'] if 'cfg_ema45_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['cfg_ema45_w']) else None,
            'cfg_wma45_w': df_merged.iloc[idx]['cfg_wma45_w'] if 'cfg_wma45_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['cfg_wma45_w']) else None,
            
            # STAMP Weekly
            'rsi_14_9days_ago_w': df_merged.iloc[idx]['rsi_14_9days_ago_w'] if 'rsi_14_9days_ago_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['rsi_14_9days_ago_w']) else None,
            'rsi_14_minus_9_w': df_merged.iloc[idx]['rsi_14_minus_9_w'] if 'rsi_14_minus_9_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['rsi_14_minus_9_w']) else None,
            'rsi_14_w_shifted': df_merged.iloc[idx]['rsi_14_w_shifted'] if 'rsi_14_w_shifted' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['rsi_14_w_shifted']) else None,
            'stamp_a_value_w': df_merged.iloc[idx]['stamp_a_value_w'] if 'stamp_a_value_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['stamp_a_value_w']) else None,
            'stamp_s9rsi_w': df_merged.iloc[idx]['stamp_s9rsi_w'] if 'stamp_s9rsi_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['stamp_s9rsi_w']) else None,
            'stamp_e45cfg_w': df_merged.iloc[idx]['stamp_e45cfg_w'] if 'stamp_e45cfg_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['stamp_e45cfg_w']) else None,
            'stamp_e45rsi_w': df_merged.iloc[idx]['stamp_e45rsi_w'] if 'stamp_e45rsi_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['stamp_e45rsi_w']) else None,
            'stamp_e20sma3_w': df_merged.iloc[idx]['stamp_e20sma3_w'] if 'stamp_e20sma3_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['stamp_e20sma3_w']) else None,
            
            # Weekly Components
            'close_w': df_merged.iloc[idx]['close_w'] if 'close_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['close_w']) else None,
            'sma4_w': df_merged.iloc[idx]['sma4_w'] if 'sma4_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['sma4_w']) else None,
            'sma9_w': df_merged.iloc[idx]['sma9_w'] if 'sma9_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['sma9_w']) else None,
            'sma18_w': df_merged.iloc[idx]['sma18_w'] if 'sma18_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['sma18_w']) else None,
            'wma45_close_w': df_merged.iloc[idx]['wma45_close_w'] if 'wma45_close_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['wma45_close_w']) else None,
            'cci_w': df_merged.iloc[idx]['cci_w'] if 'cci_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['cci_w']) else None,
            'cci_ema20_w': df_merged.iloc[idx]['cci_ema20_w'] if 'cci_ema20_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['cci_ema20_w']) else None,
            'aroon_up_w': df_merged.iloc[idx]['aroon_up_w'] if 'aroon_up_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['aroon_up_w']) else None,
            'aroon_down_w': df_merged.iloc[idx]['aroon_down_w'] if 'aroon_down_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['aroon_down_w']) else None,
            
            # RSI Screener Conditions
            'sma9_gt_tn_daily': sma9_gt_tn_daily,
            'sma9_gt_tn_weekly': sma9_gt_tn_weekly,
            'rsi_lt_80_d': rsi_lt_80_d,
            'rsi_lt_80_w': rsi_lt_80_w,
            'sma9_rsi_lte_75_d': sma9_rsi_lte_75_d,
            'sma9_rsi_lte_75_w': sma9_rsi_lte_75_w,
            'ema45_rsi_lte_70_d': ema45_rsi_lte_70_d,
            'ema45_rsi_lte_70_w': ema45_rsi_lte_70_w,
            'rsi_55_70': rsi_55_70,
            'rsi_gt_wma45_d': rsi_gt_wma45_d,
            'rsi_gt_wma45_w': rsi_gt_wma45_w,
            'sma9rsi_gt_wma45rsi_d': sma9rsi_gt_wma45rsi_d,
            'sma9rsi_gt_wma45rsi_w': sma9rsi_gt_wma45rsi_w,
            
            # STAMP Conditions
            'stamp_daily': stamp_daily,
            'stamp_weekly': stamp_weekly,
            'stamp': stamp,
            
            # Weekly CFG Conditions
            'cfg_gt_50_w': df_merged.iloc[idx]['cfg_w'] > 50 if 'cfg_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['cfg_w']) else False,
            'cfg_ema45_gt_50_w': df_merged.iloc[idx]['cfg_ema45_w'] > 50 if 'cfg_ema45_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['cfg_ema45_w']) else False,
            'cfg_ema20_gt_50_w': df_merged.iloc[idx]['cfg_ema20_w'] > 50 if 'cfg_ema20_w' in df_merged.columns and not pd.isna(df_merged.iloc[idx]['cfg_ema20_w']) else False,
            
            # Final Results
            'final_signal': final_signal,
            'score': score,
        }
        
        return result