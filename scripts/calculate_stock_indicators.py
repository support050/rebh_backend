"""
Stock Indicators Calculator - الإصدار المحدث
يحسب جميع المؤشرات ويخزنها في قاعدة البيانات
"""

import sys
import os
import argparse
import numpy as np
import pandas as pd
from decimal import Decimal
from datetime import datetime, date
from typing import List, Dict, Optional, Any
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal
from app.models.stock_indicators import StockIndicator

# Import the unified service
from scripts.indicators_data_service import IndicatorsDataService
from scripts.calculate_rsi_indicators import convert_to_float, get_val


def resolve_target_date(db: Session, target_date: date = None):
    """Resolve the target date for calculations."""
    if target_date is None:
        result = db.execute(text("SELECT MAX(date) FROM prices"))
        target_date = result.scalar()
    
    return 0, target_date


def calculate_all_indicators_for_stock(db: Session, symbol: str, target_date: date = None) -> Dict[str, Any]:
    """
    حساب جميع المؤشرات باستخدام الخدمة الموحدة
    
    Args:
        db: جلسة قاعدة البيانات
        symbol: رمز السهم
        target_date: التاريخ المستهدف (اختياري)
    
    Returns:
        قاموس بجميع المؤشرات المحسوبة
    """
    
    # جلب البيانات من قاعدة البيانات
    # نأخذ آخر 600 يوم فقط — يكفي لحساب كل المؤشرات (SMA200 + مساحة أمان)
    if target_date:
        query_limit = text("""
            SELECT date, open, high, low, close
            FROM prices
            WHERE symbol = :symbol AND date <= :target_date
            ORDER BY date DESC
            LIMIT 600
        """)
        result = db.execute(query_limit, {"symbol": symbol, "target_date": target_date})
    else:
        query_limit = text("""
            SELECT date, open, high, low, close
            FROM prices
            WHERE symbol = :symbol
            ORDER BY date DESC
            LIMIT 600
        """)
        result = db.execute(query_limit, {"symbol": symbol})

    rows = result.fetchall()
    # تحويل البيانات إلى قواميس لتجنب أخطاء Pandas مع كائنات Row في SQLAlchemy 2.0
    rows_dicts = [dict(r._mapping) for r in rows]
    # البيانات جاية DESC من الـ LIMIT — نعكسها ASC للحسابات
    rows_dicts = list(reversed(rows_dicts))
    
    if not rows_dicts or len(rows_dicts) < 100:
        print(f"⚠️  {symbol}: Not enough data ({len(rows_dicts)} rows)")
        return {}
    
    # تحويل البيانات إلى DataFrame
    df = IndicatorsDataService.prepare_price_dataframe(rows_dicts)
    if df is None:
        return {}
    
    # تحويل البيانات إلى إطار أسبوعي
    df_weekly = IndicatorsDataService.prepare_weekly_dataframe(df)
    if df_weekly is None:
        print(f"⚠️  {symbol}: Not enough weekly data")
        return {}
    
    # تحديد المؤشر الحالي (آخر شمعة)
    idx = len(df) - 1
    w_idx = len(df_weekly) - 1
    
    # التحقق من تطابق التاريخ إذا كان target_date محدداً
    if target_date:
        # البحث عن المؤشر المطابق للتاريخ المطلوب
        target_idx = None
        for i in range(len(df) - 1, -1, -1):
            if df.index[i].date() == target_date:
                target_idx = i
                break
        
        if target_idx is None:
            # إذا لم نجد التاريخ المطلوب، نستخدم آخر تاريخ متاح
            print(f"⚠️  {symbol}: Target date {target_date} not found, using latest date {df.index[-1].date()}")
        else:
            idx = target_idx
    
    # حساب جميع المؤشرات باستخدام الخدمة الموحدة
    result = IndicatorsDataService.calculate_all_indicators(
        df=df,
        df_weekly=df_weekly,
        symbol=symbol,
        target_date=target_date,
        idx=idx,
        w_idx=w_idx
    )
    
    return result


def calculate_and_store_indicators(db: Session, target_date: date = None, target_symbol: str = None, tech_map: dict = None):
    """
    حساب وتخزين جميع المؤشرات لجميع الأسهم
    
    Args:
        db: جلسة قاعدة البيانات
        target_date: التاريخ المستهدف (اختياري)
        target_symbol: رمز سهم محدد (اختياري)
    """
    print("=" * 60)
    print("📊 Starting Stock Indicators Calculation - PINESCRIPT EXACT VERSION")
    if target_symbol:
        print(f"🎯 Target Symbol: {target_symbol}")
    print("=" * 60)
    
    deleted_count = 0
    # نحصل على التاريخ (لا نحذف شيئاً للحفاظ على بيانات SMAs)
    if not target_symbol:
        deleted_count, target_date = resolve_target_date(db, target_date)
    else:
        # نحتاج لتحديد التاريخ إذا لم يُعط
        if not target_date:
            result = db.execute(text("SELECT MAX(date) FROM prices WHERE symbol = :symbol"), {"symbol": target_symbol})
            target_date = result.scalar()
    
    if not target_date:
        print("❌ No price data found.")
        return
    
    print(f"📅 Using latest date: {target_date}")
    
    # جلب جميع الأسهم المتاحة
    # لو الـ tech_map موجودة (Batch Mode) نستخدم رموزها مباشرةً بدل query إضافية
    if tech_map and not target_symbol:
        # Batch Mode: الرموز واضحة من الـ tech_map، نجيب company_name بـ query واحدة
        batch_symbols_list = list(tech_map.keys())
        placeholders = ", ".join(f":sym_{i}" for i in range(len(batch_symbols_list)))
        sym_params = {f"sym_{i}": s for i, s in enumerate(batch_symbols_list)}
        sym_params["target_date"] = target_date
        cn_query = text(f"""
            SELECT DISTINCT ON (symbol) symbol, company_name
            FROM prices
            WHERE symbol IN ({placeholders}) AND date = :target_date
        """)
        cn_result = db.execute(cn_query, sym_params)
        symbols_data = {row[0]: row[1] for row in cn_result.fetchall()}
    elif target_symbol:
        symbols_query = text("""
            SELECT DISTINCT p.symbol, p.company_name
            FROM prices p
            WHERE p.symbol = :symbol
            AND EXISTS (
                SELECT 1 FROM prices p2 
                WHERE p2.symbol = p.symbol 
                AND p2.date = :target_date
            )
            ORDER BY p.symbol
        """)
        symbols_result = db.execute(symbols_query, {"target_date": target_date, "symbol": target_symbol})
        symbols_data = {row[0]: row[1] for row in symbols_result.fetchall()}
    else:
        symbols_query = text("""
            SELECT DISTINCT p.symbol, p.company_name
            FROM prices p
            WHERE p.date <= :target_date
            AND EXISTS (
                SELECT 1 FROM prices p2 
                WHERE p2.symbol = p.symbol 
                AND p2.date = :target_date
            )
            ORDER BY p.symbol
        """)
        symbols_result = db.execute(symbols_query, {"target_date": target_date})
        symbols_data = {row[0]: row[1] for row in symbols_result.fetchall()}
    
    total_stocks = len(symbols_data)
    print(f"📈 Found {total_stocks} stocks to process")
    print("-" * 60)
    
    processed = 0
    errors = 0
    successful = 0
    error_details = []
    
    all_indicators_data = []
    
    for symbol, company_name in symbols_data.items():
        try:
            print(f"📊 Processing {symbol} ({company_name})...")
            
            data = calculate_all_indicators_for_stock(db, symbol, target_date)
            
            if not data:
                print(f"⚠️  {symbol}: No data available or insufficient data")
                errors += 1
                error_details.append(f"{symbol}: No data")
                continue

            if target_symbol:
                 print("\n========== FINAL INDICATOR VALUES ==========")
                 for k, v in data.items():
                     if k not in ['price_history', 'weekly_history']: # exclude large data
                         print(f"  {k}: {v}")
                 print("============================================\n")
            
            # إعداد البيانات للإدراج في قاعدة البيانات
            indicator_data = {
                'symbol': symbol,
                'date': target_date,
                'company_name': company_name,
                **data
            }
            
            # Merge pre-calculated technical data (SMAs, 52W, Vol) if available
            if tech_map and symbol in tech_map:
                tech_vals = tech_map[symbol]
                for k, v in tech_vals.items():
                    if k != 'date':  # skip date, already set
                        indicator_data.setdefault(k, v)  # don't overwrite PineScript values
            
            # تنظيف البيانات (تحويل numpy types إلى Python types وتقريب الأرقام لخانين لتطابق TradingView)
            for k, v in indicator_data.items():
                if isinstance(v, (np.float64, np.float32, np.integer)):
                    indicator_data[k] = round(float(v), 2) if not pd.isna(v) else None
                elif isinstance(v, np.bool_):
                    indicator_data[k] = bool(v)
                elif isinstance(v, float):
                    indicator_data[k] = round(v, 2) if not pd.isna(v) else None
                elif isinstance(v, (list, dict, np.ndarray, pd.Series)):
                    indicator_data[k] = None  # لا نخزن القوائم في قاعدة البيانات
            
            all_indicators_data.append(indicator_data)
            processed += 1
            
            if processed % 10 == 0:
                print(f"✅ Processed {processed}/{total_stocks} stocks...")
                print(f"   Last: {symbol} - Score: {data.get('score', 0)} | Final Signal: {data.get('final_signal', False)}")
                
        except Exception as e:
            print(f"❌ Error processing {symbol}: {e}")
            import traceback
            traceback.print_exc()
            errors += 1
            error_details.append(f"{symbol}: {str(e)}")

    print("-" * 60)
    print(f"💾 Saving {len(all_indicators_data)} stock indicators to database...")
    
    if all_indicators_data:
        # استخدام SessionLocal منفصلة وطازجة لعملية الحفظ لتجنب أي Session متعطلة أو منتهية أثناء الحساب
        from app.core.database import SessionLocal
        save_db = SessionLocal()
        try:
            for i, indicator_data in enumerate(all_indicators_data):
                for attempt in range(2):
                    try:
                        stmt = insert(StockIndicator).values(indicator_data)
                        stmt = stmt.on_conflict_do_update(
                            index_elements=['symbol', 'date'],
                            set_={k: v for k, v in indicator_data.items() if k not in ['symbol', 'date', 'created_at']}
                        )
                        save_db.execute(stmt)
                        
                        # Commit every 25 records to keep transactions short
                        if (i + 1) % 25 == 0:
                            save_db.commit()
                        break
                    except Exception as save_item_err:
                        save_db.rollback()
                        if attempt == 0:
                            # Re-establish connection and retry once
                            save_db.close()
                            save_db = SessionLocal()
                        else:
                            raise save_item_err
            
            save_db.commit()
            successful = len(all_indicators_data)
            print("✅ All stock indicators saved successfully.")
        except Exception as e:
            print(f"❌ Error during bulk save: {e}")
            save_db.rollback()
            errors += len(all_indicators_data)
            error_details.append(f"Bulk Save Error: {str(e)}")
        finally:
            save_db.close()

    print("-" * 60)
    print("📊 Calculation Summary:")
    print(f"   🧹 Deleted: {deleted_count}")
    print(f"   ✅ Success: {successful}")
    print(f"   ❌ Errors: {errors}")
    if error_details:
        print("\n   Error Details:")
        for err in error_details[:10]:  # عرض أول 10 أخطاء فقط
            print(f"   - {err}")
    print("=" * 60)
    
    return processed, errors, successful


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Calculate Stock Indicators')
    parser.add_argument('--date', type=str, help='Target date in YYYY-MM-DD format')
    parser.add_argument('--symbol', type=str, help='Target symbol (optional)')
    
    args = parser.parse_args()
    
    db = SessionLocal()
    try:
        target_date = None
        if args.date:
            try:
                target_date = date.fromisoformat(args.date)
            except Exception:
                print(f"❌ Invalid date format: {args.date}. Use YYYY-MM-DD")
                raise

        # تشغيل الحساب مع التاريخ والرمز (إن وُجد)
        calculate_and_store_indicators(db, target_date=target_date, target_symbol=args.symbol)
    finally:
        db.close()