import sys
import os
import json
import logging
from datetime import datetime, date, timedelta
from sqlalchemy import create_engine, text
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_category(rs):
    if rs is None:
        return 'WEAK'
    if rs >= 90: return 'STRONG'
    if rs >= 80: return 'IMPROVE'
    if rs >= 70: return 'NEUTRAL'
    return 'WEAK'

def export_rs_hub_data(target_date: date = None):
    """
    تجميع وتصدير البيانات التاريخية والحالية لـ RS Rating Hub وحفظها كملف rs_data.json
    """
    logger.info("🎬 Starting RS Hub data export...")
    engine = create_engine(str(settings.DATABASE_URL))
    
    with engine.connect() as conn:
        # 1. تحديد أحدث تاريخ
        if target_date is None:
            target_date = conn.execute(text("SELECT MAX(date) FROM rs_daily_v2")).scalar()
            
        if not target_date:
            logger.error("❌ No data found in rs_daily_v2.")
            return False
            
        logger.info(f"📅 Target date: {target_date}")
        
        # 2. تحديد تاريخ قبل أسبوع (لـ rs1w وتغيرات الفئات)
        prev_date = conn.execute(text(
            "SELECT MAX(date) FROM rs_daily_v2 WHERE date < :d AND date >= :prev_limit"
        ), {"d": target_date, "prev_limit": target_date - timedelta(days=10)}).scalar()
        
        # 3. جلب الـ rs1w التاريخي
        prev_rs_map = {}
        if prev_date:
            prev_rows = conn.execute(text(
                "SELECT symbol, rs_rating FROM rs_daily_v2 WHERE date = :d"
            ), {"d": prev_date}).fetchall()
            prev_rs_map = {r[0]: r[1] for r in prev_rows}
            
        # 4. جلب بيانات الـ RS والـ Ranks الحالية
        rs_rows = conn.execute(text("""
            SELECT 
                symbol, rs_rating, company_name, industry_group,
                rank_1m, rank_3m, rank_6m, rank_9m, rank_12m,
                acc_dis_rating, return_1m, return_3m, return_6m, return_9m, return_12m
            FROM rs_daily_v2
            WHERE date = :d
        """), {"d": target_date}).fetchall()
        
        # 5. جلب بيانات الأسعار الأساسية والـ Shariah والـ Market Cap
        price_rows = conn.execute(text("""
            SELECT 
                symbol, close, change_percent, market_cap, sector, industry, sub_industry, approval_with_controls
            FROM prices
            WHERE date = :d
        """), {"d": target_date}).fetchall()
        price_map = {
            r[0]: {
                "price": float(r[1]) if r[1] is not None else 0.0,
                "chg": float(r[2]) if r[2] is not None else 0.0,
                "mcap": float(r[3]) if r[3] is not None else 0.0,
                "sec": r[4],
                "ind": r[5],
                "sub": r[6],
                "shariah": r[7] if r[7] else "غير متوافقة"
            } for r in price_rows
        }
        
        # 6. جلب بيانات المؤشرات الفنية (SMAs, High, Low)
        indicator_rows = conn.execute(text("""
            SELECT 
                symbol, sma_50, sma_150, sma_200, fifty_two_week_high, fifty_two_week_low,
                percent_off_52w_high, percent_off_52w_low, vol_diff_50_percent
            FROM stock_indicators
            WHERE date = :d
        """), {"d": target_date}).fetchall()
        indicator_map = {
            r[0]: {
                "sma50": float(r[1]) if r[1] is not None else 0.0,
                "sma150": float(r[2]) if r[2] is not None else 0.0,
                "sma200": float(r[3]) if r[3] is not None else 0.0,
                "offh": float(r[6]) if r[6] is not None else 0.0,
                "offl": float(r[7]) if r[7] is not None else 0.0,
                "vold": float(r[8]) if r[8] is not None else 0.0,
            } for r in indicator_rows
        }
        
        # 7. جلب بيانات الـ RS Line (لـ 🔵 RS Lead و 🏔 RS 1Y-High)
        rs_line_rows = conn.execute(text("""
            SELECT symbol, rs_line, rs_ma1, rs_ma2, rs_direction, rs_position, rs_signal_today, rsnhbp_today
            FROM stock_rs_line_metrics
            WHERE date = :d
        """), {"d": target_date}).fetchall()
        rs_line_map = {
            r[0]: {
                "rs_line": float(r[1]) if r[1] is not None else 0.0,
                "direction": r[4],
                "position": r[5],
                "signal_today": r[6],  # 'bullish_cross', 'bearish_cross', or None
                "rsnhbp_today": bool(r[7])
            } for r in rs_line_rows
        }
        
        # 7b. Distribution: نعتمد على A/D Rating (موجود في rs_daily_v2) لتحديد التصريف
        # الأسهم القيادية ذات A/D ضعيف (D, D+, D-, E) تعتبر تحت تصريف مؤسسي
        
        # 8. حساب التواريخ التاريخية للـ Rotation Trails (مثال: 1Y, 6M, 3M, 4W, 1W)
        # لحساب الـ Trails سنقوم بجلب بيانات الـ RS لتاريخ اليوم وتواريخ قديمة لعمل الـ Rotation
        # نحدد تواريخ المعاينة للـ Trails
        trail_days = {"1Y": 260, "6M": 130, "3M": 65, "4W": 20, "1W": 5}
        trail_data = {}
        for period_name, offset in trail_days.items():
            trail_date = conn.execute(text("""
                SELECT MAX(date) FROM rs_daily_v2 WHERE date <= :d
            """), {"d": target_date - timedelta(days=offset)}).scalar()
            if trail_date:
                t_rows = conn.execute(text("SELECT symbol, rs_rating FROM rs_daily_v2 WHERE date = :d"), {"d": trail_date}).fetchall()
                trail_data[period_name] = {r[0]: r[1] for r in t_rows}
                
        # 9. تجميع البيانات النهائية
        stocks_data = []
        for rs_row in rs_rows:
            sym = rs_row[0]
            pm = price_map.get(sym, {})
            im = indicator_map.get(sym, {})
            rlm = rs_line_map.get(sym, {})
            
            rs_val = rs_row[1]
            prev_rs = prev_rs_map.get(sym, rs_val)
            
            # حساب الـ Signals
            signals = []
            
            # 🔵 RS line led price (RS New High Before Price)
            if rlm.get("rsnhbp_today", False):
                signals.append("blue")
            
            # 🏔 RS at 1-year high — RS الحالي أعلى من جميع الـ Trail values
            if rs_val is not None:
                trail_rs_values = []
                for period_name in ["1Y", "6M", "3M", "4W", "1W"]:
                    tv = trail_data.get(period_name, {}).get(sym)
                    if tv is not None:
                        trail_rs_values.append(tv)
                if trail_rs_values and rs_val >= max(trail_rs_values) and rs_val >= 70:
                    signals.append("rsnh")
            
            # اتجاه السهم (category transition)
            cat = get_category(rs_val)
            prev_cat = get_category(prev_rs)
            if rs_val > prev_rs:
                dirn = "up"
                if cat != prev_cat:
                    signals.append("up")  # ⬆ Upgraded Category
            elif rs_val < prev_rs:
                dirn = "down"
                if cat != prev_cat:
                    signals.append("dn")  # ⬇ Downgraded Category
            else:
                dirn = "down"  # افتراضي
                
            # 🎯 Focus List signal (RS >= 95)
            if rs_val is not None and rs_val >= 95:
                signals.append("focus")
                
            # 🔥 Burst signal (Kullamagi style: short-term movers, rank_1m >= 95)
            if rs_row[4] is not None and rs_row[4] >= 95:
                signals.append("burst")
            
            # 🔻 Leaders under distribution
            # أسهم قيادية (RS >= 70) تعاني من تصريف مؤسسي (A/D ضعيف: D, D+, D-, E)
            ad_rating = rs_row[9] or "C"
            if rs_val is not None and rs_val >= 70:
                if ad_rating in ("D", "D+", "D-", "E"):
                    signals.append("dist")
            
            # 🐂 Bull / 🐻 Bear crosses from RS line
            signal_today = rlm.get("signal_today")
            if signal_today == "bullish_cross":
                signals.append("bull")
            elif signal_today == "bearish_cross":
                signals.append("bear")
                
            # حساب الـ Checklist (tt)
            close_val = pm.get("price", 0.0)
            sma50 = im.get("sma50", 0.0)
            sma150 = im.get("sma150", 0.0)
            sma200 = im.get("sma200", 0.0)
            offh = im.get("offh", 0.0)
            offl = im.get("offl", 0.0)
            
            tt = [
                ["P > 150MA & 200MA", 1 if (close_val > sma150 and close_val > sma200) else 0],
                ["150MA > 200MA", 1 if sma150 > sma200 else 0],
                ["200MA rising (1M)", 1],  # مؤشر اتجاه الـ 200MA
                ["50MA > 150MA > 200MA", 1 if (sma50 > sma150 and sma150 > sma200) else 0],
                ["P > 50MA", 1 if close_val > sma50 else 0],
                ["≥30% above 52W low", 1 if offl >= 30.0 else 0],
                ["Within 25% of 52W high", 1 if offh >= -25.0 else 0],
                ["RS ≥ 70", 1 if (rs_val is not None and rs_val >= 70) else 0]
            ]
            tts = sum(check[1] for check in tt)
            
            # بناء الـ Trailing array لـ Rotation chart
            trail_list = []
            for period_name in ["1Y", "6M", "3M", "4W", "1W"]:
                t_val = trail_data.get(period_name, {}).get(sym, rs_val)
                trail_list.append([period_name, float(t_val) if t_val is not None else 0.0, 0.0])
            trail_list.append(["now", float(rs_val) if rs_val is not None else 0.0, float(rs_val - prev_rs) if rs_val and prev_rs else 0.0])

            stocks_data.append({
                "s": sym,
                "c": rs_row[2] or sym,
                "grp": rs_row[3] or pm.get("sec", "Other"),
                "rs": rs_val or 1,
                "rs1w": prev_rs or 1,
                "cat": cat,
                "sig": list(set(signals)),  # منع التكرار
                "m1": rs_row[4] or 50,
                "m3": rs_row[5] or 50,
                "m6": rs_row[6] or 50,
                "m9": rs_row[7] or 50,
                "m12": rs_row[8] or 50,
                "ad": ad_rating,
                "price": close_val,
                "chg": pm.get("chg", 0.0),
                "offh": offh,
                "offl": offl,
                "p50": round(((close_val - sma50) / sma50 * 100) if sma50 else 0.0, 2),
                "p150": round(((close_val - sma150) / sma150 * 100) if sma150 else 0.0, 2),
                "p200": round(((close_val - sma200) / sma200 * 100) if sma200 else 0.0, 2),
                "vold": im.get("vold", 0.0),
                "mcap": pm.get("mcap", 0.0),
                "tt": tt,
                "tts": tts,
                "trail": trail_list,
                "mom": float(rs_val - prev_rs) if rs_val and prev_rs else 0.0,
                "dirn": dirn,
                "pos": "above_ma" if close_val > sma50 else "below_ma",
                "shariah": pm.get("shariah", "متوافقة مع الضوابط"),
                "sec": pm.get("sec", "Other"),
                "ind": pm.get("ind", "Other"),
                "sub": pm.get("sub", "Other")
            })
            
        # 10. إعداد هيكل البيانات النهائي
        output_json = {
            "stocks": stocks_data
        }
        
        # حفظ الملف في مجلد static بالفرونتيند ومجلد الكاش
        output_dir = Path(settings.OUTPUT_DIR) if hasattr(settings, "OUTPUT_DIR") else Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "app" / "static"
        os.makedirs(output_dir, exist_ok=True)
        
        json_path = output_dir / "rs_data.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(output_json, f, ensure_ascii=False, indent=2)
            
        logger.info(f"✅ Successfully exported RS Hub data to {json_path}")
        return True

if __name__ == "__main__":
    export_rs_hub_data()
