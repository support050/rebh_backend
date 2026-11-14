import sys
import json
import decimal
from datetime import date
from typing import Optional, Dict, Any

sys.path.insert(0, '.')
from app.core.database import SessionLocal
from app.models.stock_indicators import StockIndicator

def dec(v):
    """Convert Decimal to float, handle None and other types"""
    if v is None:
        return None
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, date):
        return str(v)
    if isinstance(v, (int, float)):
        return v
    return v

def fetch(symbol: str, target_date: Optional[str] = None) -> Dict[str, Any]:
    """Fetch ALL indicators for a given symbol"""
    db = SessionLocal()
    try:
        query = db.query(StockIndicator).filter(StockIndicator.symbol == symbol)
        
        if target_date:
            query = query.filter(StockIndicator.date == target_date)
        else:
            query = query.order_by(StockIndicator.date.desc())
        
        ind = query.first()
        
        if not ind:
            print(f'❌ No indicator found for {symbol}' + (f' on {target_date}' if target_date else ''))
            return {}
        
        # Get all columns from the model
        columns = StockIndicator.__table__.columns.keys()
        
        # Build output dictionary with ALL fields
        out = {}
        for col in columns:
            value = getattr(ind, col, None)
            out[col] = dec(value)
        
        print(f'✅ Fetched {len(out)} indicators for {symbol} - Date: {out.get("date")}')
        print(json.dumps(out, ensure_ascii=False, indent=2))
        
        return out
        
    except Exception as e:
        print(f'❌ Error fetching data: {e}')
        import traceback
        traceback.print_exc()
        return {}
    finally:
        db.close()

def fetch_all_daily(symbol: str) -> Dict[str, Any]:
    """Fetch only daily indicators"""
    db = SessionLocal()
    try:
        ind = db.query(StockIndicator).filter(
            StockIndicator.symbol == symbol
        ).order_by(StockIndicator.date.desc()).first()
        
        if not ind:
            print(f'❌ No indicator found for {symbol}')
            return {}
        
        out = {
            # Basic Info
            'symbol': ind.symbol,
            'company_name': ind.company_name,
            'date': str(ind.date) if ind.date else None,
            'close': dec(ind.close),
            
            # ===== 1. RSI INDICATOR - DAILY =====
            'rsi_14': dec(ind.rsi_14),
            'rsi_3': dec(ind.rsi_3),
            'sma9_rsi': dec(ind.sma9_rsi),
            'wma45_rsi': dec(ind.wma45_rsi),
            'ema45_rsi': dec(ind.ema45_rsi),
            'sma3_rsi3': dec(ind.sma3_rsi3),
            'ema20_sma3': dec(ind.ema20_sma3),
            
            # ===== 2. THE NUMBER - DAILY =====
            'sma9_close': dec(ind.sma9_close),
            'the_number': dec(ind.the_number),
            'the_number_hl': dec(ind.the_number_hl),
            'the_number_ll': dec(ind.the_number_ll),
            'high_sma13': dec(ind.high_sma13),
            'low_sma13': dec(ind.low_sma13),
            'high_sma65': dec(ind.high_sma65),
            'low_sma65': dec(ind.low_sma65),
            
            # ===== 3. STAMP INDICATOR - DAILY =====
            'rsi_14_9days_ago': dec(ind.rsi_14_9days_ago),
            'stamp_a_value': dec(ind.stamp_a_value),
            'stamp_s9rsi': dec(ind.stamp_s9rsi),
            'stamp_e45cfg': dec(ind.stamp_e45cfg),
            'stamp_e45rsi': dec(ind.stamp_e45rsi),
            'stamp_e20sma3': dec(ind.stamp_e20sma3),
            
            # ===== 4. CFG ANALYSIS - DAILY =====
            'cfg_daily': dec(ind.cfg_daily),
            'cfg_sma9': dec(ind.cfg_sma9),
            'cfg_sma20': dec(ind.cfg_sma20),
            'cfg_ema20': dec(ind.cfg_ema20),
            'cfg_ema45': dec(ind.cfg_ema45),
            'cfg_wma45': dec(ind.cfg_wma45),
            'rsi_14_9days_ago_cfg': dec(ind.rsi_14_9days_ago_cfg),
            'rsi_14_minus_9': dec(ind.rsi_14_minus_9),
            
            # ===== 5. TREND SCREENER - DAILY =====
            'sma4': dec(ind.sma4),
            'sma9': dec(ind.sma9),
            'sma18': dec(ind.sma18),
            'wma45_close': dec(ind.wma45_close),
            'cci': dec(ind.cci),
            'cci_ema20': dec(ind.cci_ema20),
            'aroon_up': dec(ind.aroon_up),
            'aroon_down': dec(ind.aroon_down),
        }
        
        print(f'✅ Fetched daily indicators for {symbol}')
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return out
        
    except Exception as e:
        print(f'❌ Error: {e}')
        import traceback
        traceback.print_exc()
        return {}
    finally:
        db.close()

def fetch_all_weekly(symbol: str) -> Dict[str, Any]:
    """Fetch only weekly indicators - INCLUDING STAMP WEEKLY AND FULL THE NUMBER"""
    db = SessionLocal()
    try:
        ind = db.query(StockIndicator).filter(
            StockIndicator.symbol == symbol
        ).order_by(StockIndicator.date.desc()).first()
        
        if not ind:
            print(f'❌ No indicator found for {symbol}')
            return {}
        
        out = {
            # Basic Info
            'symbol': ind.symbol,
            'company_name': ind.company_name,
            'date': str(ind.date) if ind.date else None,
            'close_w': dec(ind.close_w),
            
            # ===== 1. RSI INDICATOR - WEEKLY =====
            'rsi_w': dec(ind.rsi_w),
            'rsi_3_w': dec(ind.rsi_3_w),
            'sma9_rsi_w': dec(ind.sma9_rsi_w),
            'wma45_rsi_w': dec(ind.wma45_rsi_w),
            'ema45_rsi_w': dec(ind.ema45_rsi_w),
            'sma3_rsi3_w': dec(ind.sma3_rsi3_w),
            'ema20_sma3_w': dec(ind.ema20_sma3_w),
            
            # ===== 2. THE NUMBER - WEEKLY (FULL) =====
            'sma9_close_w': dec(ind.sma9_close_w),
            'the_number_w': dec(ind.the_number_w),
            'the_number_hl_w': dec(ind.the_number_hl) if hasattr(ind, 'the_number_hl') else None,
            'the_number_ll_w': dec(ind.the_number_ll) if hasattr(ind, 'the_number_ll') else None,
            'high_sma13_w': dec(ind.high_sma13) if hasattr(ind, 'high_sma13') else None,
            'low_sma13_w': dec(ind.low_sma13) if hasattr(ind, 'low_sma13') else None,
            'high_sma65_w': dec(ind.high_sma65) if hasattr(ind, 'high_sma65') else None,
            'low_sma65_w': dec(ind.low_sma65) if hasattr(ind, 'low_sma65') else None,
            
            # ===== 3. STAMP INDICATOR - WEEKLY =====
            'rsi_14_9weeks_ago': dec(ind.rsi_14_9days_ago) if hasattr(ind, 'rsi_14_9days_ago') else None,
            'stamp_a_value_w': dec(ind.stamp_a_value) if hasattr(ind, 'stamp_a_value') else None,
            'stamp_s9rsi_w': dec(ind.sma9_rsi_w) if hasattr(ind, 'sma9_rsi_w') else None,
            'stamp_e45cfg_w': dec(ind.cfg_ema45_w) if hasattr(ind, 'cfg_ema45_w') else None,
            'stamp_e45rsi_w': dec(ind.ema45_rsi_w) if hasattr(ind, 'ema45_rsi_w') else None,
            'stamp_e20sma3_w': dec(ind.ema20_sma3_w) if hasattr(ind, 'ema20_sma3_w') else None,
            
            # ===== 4. CFG ANALYSIS - WEEKLY =====
            'cfg_w': dec(ind.cfg_w),
            'cfg_sma9_w': dec(ind.cfg_sma9_w),
            'cfg_ema20_w': dec(ind.cfg_ema20_w),
            'cfg_ema45_w': dec(ind.cfg_ema45_w),
            'cfg_wma45_w': dec(ind.cfg_wma45_w),
            'rsi_14_w_shifted': dec(ind.rsi_14_w_shifted),
            'rsi_14_minus_9_w': dec(ind.rsi_14_minus_9_w),
            
            # ===== 5. TREND SCREENER - WEEKLY =====
            'sma4_w': dec(ind.sma4_w),
            'sma9_w': dec(ind.sma9_w),
            'sma18_w': dec(ind.sma18_w),
            'wma45_close_w': dec(ind.wma45_close_w),
            'cci_w': dec(ind.cci_w),
            'cci_ema20_w': dec(ind.cci_ema20_w),
            'aroon_up_w': dec(ind.aroon_up_w),
            'aroon_down_w': dec(ind.aroon_down_w),
        }
        
        print(f'✅ Fetched weekly indicators for {symbol} (including STAMP WEEKLY & FULL THE NUMBER)')
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return out
        
    except Exception as e:
        print(f'❌ Error: {e}')
        import traceback
        traceback.print_exc()
        return {}
    finally:
        db.close()

def fetch_conditions(symbol: str) -> Dict[str, Any]:
    """Fetch only conditions and signals"""
    db = SessionLocal()
    try:
        ind = db.query(StockIndicator).filter(
            StockIndicator.symbol == symbol
        ).order_by(StockIndicator.date.desc()).first()
        
        if not ind:
            print(f'❌ No indicator found for {symbol}')
            return {}
        
        out = {
            # Basic Info
            'symbol': ind.symbol,
            'date': str(ind.date) if ind.date else None,
            
            # ===== STAMP CONDITIONS =====
            'stamp_daily': bool(ind.stamp_daily),
            'stamp_weekly': bool(ind.stamp_weekly),
            'stamp': bool(ind.stamp),
            
            # ===== RSI SCREENER CONDITIONS =====
            'sma9_gt_tn_daily': bool(ind.sma9_gt_tn_daily),
            'sma9_gt_tn_weekly': bool(ind.sma9_gt_tn_weekly),
            'rsi_lt_80_d': bool(ind.rsi_lt_80_d),
            'rsi_lt_80_w': bool(ind.rsi_lt_80_w),
            'sma9_rsi_lte_75_d': bool(ind.sma9_rsi_lte_75_d),
            'sma9_rsi_lte_75_w': bool(ind.sma9_rsi_lte_75_w),
            'ema45_rsi_lte_70_d': bool(ind.ema45_rsi_lte_70_d),
            'ema45_rsi_lte_70_w': bool(ind.ema45_rsi_lte_70_w),
            'rsi_55_70': bool(ind.rsi_55_70),
            'rsi_gt_wma45_d': bool(ind.rsi_gt_wma45_d),
            'rsi_gt_wma45_w': bool(ind.rsi_gt_wma45_w),
            'sma9rsi_gt_wma45rsi_d': bool(ind.sma9rsi_gt_wma45rsi_d),
            'sma9rsi_gt_wma45rsi_w': bool(ind.sma9rsi_gt_wma45rsi_w),
            
            # ===== CFG CONDITIONS =====
            'cfg_gt_50_daily': bool(ind.cfg_gt_50_daily),
            'cfg_ema45_gt_50': bool(ind.cfg_ema45_gt_50),
            'cfg_ema20_gt_50': bool(ind.cfg_ema20_gt_50),
            'cfg_gt_50_w': bool(ind.cfg_gt_50_w),
            'cfg_ema45_gt_50_w': bool(ind.cfg_ema45_gt_50_w),
            'cfg_ema20_gt_50_w': bool(ind.cfg_ema20_gt_50_w),
            
            # ===== TREND CONDITIONS =====
            'price_gt_sma18': bool(ind.price_gt_sma18),
            'price_gt_sma9_weekly': bool(ind.price_gt_sma9_weekly),
            'sma_trend_daily': bool(ind.sma_trend_daily),
            'sma_trend_weekly': bool(ind.sma_trend_weekly),
            'cci_gt_100': bool(ind.cci_gt_100),
            'cci_ema20_gt_0_daily': bool(ind.cci_ema20_gt_0_daily),
            'cci_ema20_gt_0_weekly': bool(ind.cci_ema20_gt_0_weekly),
            'aroon_up_gt_70': bool(ind.aroon_up_gt_70),
            'aroon_down_lt_30': bool(ind.aroon_down_lt_30),
            'is_etf_or_index': bool(ind.is_etf_or_index),
            'has_gap': bool(ind.has_gap),
            'trend_signal': bool(ind.trend_signal),
            
            # ===== FINAL =====
            'final_signal': bool(ind.final_signal),
            'score': int(ind.score) if ind.score else 0,
        }
        
        print(f'✅ Fetched conditions for {symbol}')
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return out
        
    except Exception as e:
        print(f'❌ Error: {e}')
        import traceback
        traceback.print_exc()
        return {}
    finally:
        db.close()

def fetch_complete(symbol: str) -> Dict[str, Any]:
    """Fetch ALL indicators, conditions, and signals - Complete dataset with ALL weekly fields"""
    db = SessionLocal()
    try:
        ind = db.query(StockIndicator).filter(
            StockIndicator.symbol == symbol
        ).order_by(StockIndicator.date.desc()).first()
        
        if not ind:
            print(f'❌ No indicator found for {symbol}')
            return {}
        
        # Get all columns from the model
        columns = StockIndicator.__table__.columns.keys()
        
        # Build complete output dictionary
        out = {}
        for col in columns:
            value = getattr(ind, col, None)
            out[col] = dec(value)
        
        # Add computed fields
        out['id'] = str(out.get('id')) if out.get('id') else None
        out['created_at'] = str(out.get('created_at')) if out.get('created_at') else None
        out['updated_at'] = str(out.get('updated_at')) if out.get('updated_at') else None
        
        # Add weekly STAMP and THE NUMBER aliases
        out['rsi_14_9weeks_ago'] = out.get('rsi_14_9days_ago')
        out['stamp_a_value_w'] = out.get('stamp_a_value')
        out['stamp_s9rsi_w'] = out.get('sma9_rsi_w')
        out['stamp_e45cfg_w'] = out.get('cfg_ema45_w')
        out['stamp_e45rsi_w'] = out.get('ema45_rsi_w')
        out['stamp_e20sma3_w'] = out.get('ema20_sma3_w')
        out['the_number_hl_w'] = out.get('the_number_hl')
        out['the_number_ll_w'] = out.get('the_number_ll')
        out['high_sma13_w'] = out.get('high_sma13')
        out['low_sma13_w'] = out.get('low_sma13')
        out['high_sma65_w'] = out.get('high_sma65')
        out['low_sma65_w'] = out.get('low_sma65')
        
        print('=' * 60)
        print(f'📊 COMPLETE INDICATORS REPORT - {symbol}')
        print('=' * 60)
        print(f'📅 Date: {out.get("date")}')
        print(f'🏢 Company: {out.get("company_name")}')
        print(f'💰 Price: {out.get("close")}')
        print(f'🎯 Score: {out.get("score")}/15')
        print(f'🚦 Final Signal: {"✅ PASSING" if out.get("final_signal") else "❌ FAILING"}')
        print('=' * 60)
        print(f'📈 Total indicators: {len(out)} fields')
        print('=' * 60)
        
        # Print organized by category - COMPLETE WITH ALL WEEKLY FIELDS
        categories = {
            'BASIC INFO': ['symbol', 'company_name', 'date', 'close', 'close_w', 'score', 'final_signal'],
            
            # RSI
            'RSI DAILY': ['rsi_14', 'rsi_3', 'sma9_rsi', 'wma45_rsi', 'ema45_rsi', 'sma3_rsi3', 'ema20_sma3'],
            'RSI WEEKLY': ['rsi_w', 'rsi_3_w', 'sma9_rsi_w', 'wma45_rsi_w', 'ema45_rsi_w', 'sma3_rsi3_w', 'ema20_sma3_w'],
            
            # THE NUMBER - DAILY & WEEKLY
            'THE NUMBER DAILY': ['sma9_close', 'the_number', 'the_number_hl', 'the_number_ll', 'high_sma13', 'low_sma13', 'high_sma65', 'low_sma65'],
            'THE NUMBER WEEKLY': ['sma9_close_w', 'the_number_w', 'the_number_hl_w', 'the_number_ll_w', 'high_sma13_w', 'low_sma13_w', 'high_sma65_w', 'low_sma65_w'],
            
            # STAMP - DAILY & WEEKLY
            'STAMP DAILY': ['rsi_14_9days_ago', 'stamp_a_value', 'stamp_s9rsi', 'stamp_e45cfg', 'stamp_e45rsi', 'stamp_e20sma3', 'stamp_daily', 'stamp'],
            'STAMP WEEKLY': ['rsi_14_9weeks_ago', 'stamp_a_value_w', 'stamp_s9rsi_w', 'stamp_e45cfg_w', 'stamp_e45rsi_w', 'stamp_e20sma3_w', 'stamp_weekly'],
            
            # CFG - DAILY & WEEKLY
            'CFG DAILY': ['cfg_daily', 'cfg_sma9', 'cfg_sma20', 'cfg_ema20', 'cfg_ema45', 'cfg_wma45', 'rsi_14_9days_ago_cfg', 'rsi_14_minus_9', 'cfg_gt_50_daily', 'cfg_ema45_gt_50', 'cfg_ema20_gt_50'],
            'CFG WEEKLY': ['cfg_w', 'cfg_sma9_w', 'cfg_ema20_w', 'cfg_ema45_w', 'cfg_wma45_w', 'rsi_14_w_shifted', 'rsi_14_minus_9_w', 'cfg_gt_50_w', 'cfg_ema45_gt_50_w', 'cfg_ema20_gt_50_w'],
            
            # TREND - DAILY & WEEKLY
            'TREND DAILY': ['sma4', 'sma9', 'sma18', 'wma45_close', 'cci', 'cci_ema20', 'aroon_up', 'aroon_down'],
            'TREND WEEKLY': ['sma4_w', 'sma9_w', 'sma18_w', 'wma45_close_w', 'close_w', 'cci_w', 'cci_ema20_w', 'aroon_up_w', 'aroon_down_w'],
            
            # CONDITIONS
            'TREND CONDITIONS': ['price_gt_sma18', 'price_gt_sma9_weekly', 'sma_trend_daily', 'sma_trend_weekly', 'cci_gt_100', 'cci_ema20_gt_0_daily', 'cci_ema20_gt_0_weekly', 'aroon_up_gt_70', 'aroon_down_lt_30', 'is_etf_or_index', 'has_gap', 'trend_signal'],
            'RSI SCREENER CONDITIONS': ['sma9_gt_tn_daily', 'sma9_gt_tn_weekly', 'rsi_lt_80_d', 'rsi_lt_80_w', 'sma9_rsi_lte_75_d', 'sma9_rsi_lte_75_w', 'ema45_rsi_lte_70_d', 'ema45_rsi_lte_70_w', 'rsi_55_70', 'rsi_gt_wma45_d', 'rsi_gt_wma45_w', 'sma9rsi_gt_wma45rsi_d', 'sma9rsi_gt_wma45rsi_w'],
        }
        
        for category, fields in categories.items():
            print(f'\n📌 {category}:')
            print('  ' + '-' * 40)
            for field in fields:
                if field in out:
                    value = out[field]
                    if isinstance(value, bool):
                        status = '✅' if value else '❌'
                        print(f'  {status} {field:30s}: {value}')
                    else:
                        print(f'     {field:30s}: {value}')
                else:
                    print(f'     {field:30s}: NOT FOUND')
        
        print('\n' + '=' * 60)
        print('✅ STAMP WEEKLY and FULL THE NUMBER WEEKLY included!')
        print('=' * 60)
        
        return out
        
    except Exception as e:
        print(f'❌ Error fetching data: {e}')
        import traceback
        traceback.print_exc()
        return {}
    finally:
        db.close()

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Fetch stock indicators - INCLUDING STAMP WEEKLY & FULL THE NUMBER')
    parser.add_argument('symbol', nargs='?', default='1321', help='Stock symbol')
    parser.add_argument('--date', '-d', help='Specific date (YYYY-MM-DD)')
    parser.add_argument('--type', '-t', choices=['all', 'daily', 'weekly', 'conditions', 'complete'], 
                       default='complete', help='Type of data to fetch')
    
    args = parser.parse_args()
    
    if args.type == 'all':
        fetch(args.symbol, args.date)
    elif args.type == 'daily':
        fetch_all_daily(args.symbol)
    elif args.type == 'weekly':
        fetch_all_weekly(args.symbol)
    elif args.type == 'conditions':
        fetch_conditions(args.symbol)
    else:  # complete
        fetch_complete(args.symbol)