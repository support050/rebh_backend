import sys
import os
import json
import logging
from datetime import datetime, date, timedelta
from sqlalchemy import create_engine, text
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
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
                percent_off_52w_high, percent_off_52w_low, vol_diff_50_percent,
                sma200_gt_sma200_1m_ago
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
                "sma200_rising": bool(r[9]) if r[9] is not None else False,
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
        
        # 8. حساب التواريخ التاريخية للـ Rotation Trails (1Y, 6M, 3M, 4W, 1W)
        # لكل نقطة: RS + velocity (Δ RS خلال ~4 أسابيع من ذلك التاريخ)
        trail_days = {"1Y": 260, "6M": 130, "3M": 65, "4W": 20, "1W": 5}
        VELOCITY_LOOKBACK_DAYS = 28  # ~4 calendar weeks
        trail_data = {}       # period -> {symbol: rs}
        trail_mom_data = {}   # period -> {symbol: mom}
        for period_name, offset in trail_days.items():
            trail_date = conn.execute(text("""
                SELECT MAX(date) FROM rs_daily_v2 WHERE date <= :d
            """), {"d": target_date - timedelta(days=offset)}).scalar()
            if not trail_date:
                continue
            t_rows = conn.execute(text(
                "SELECT symbol, rs_rating FROM rs_daily_v2 WHERE date = :d"
            ), {"d": trail_date}).fetchall()
            trail_data[period_name] = {r[0]: r[1] for r in t_rows}

            mom_ref_date = conn.execute(text("""
                SELECT MAX(date) FROM rs_daily_v2 WHERE date <= :d
            """), {"d": trail_date - timedelta(days=VELOCITY_LOOKBACK_DAYS)}).scalar()
            if mom_ref_date:
                m_rows = conn.execute(text(
                    "SELECT symbol, rs_rating FROM rs_daily_v2 WHERE date = :d"
                ), {"d": mom_ref_date}).fetchall()
                mom_ref_map = {r[0]: r[1] for r in m_rows}
                trail_mom_data[period_name] = {
                    sym: (float(rs) - float(mom_ref_map[sym]))
                    if rs is not None and mom_ref_map.get(sym) is not None else 0.0
                    for sym, rs in trail_data[period_name].items()
                }
            else:
                trail_mom_data[period_name] = {sym: 0.0 for sym in trail_data[period_name]}

        # Velocity at "now": RS today vs ~4W ago
        now_mom_ref_date = conn.execute(text("""
            SELECT MAX(date) FROM rs_daily_v2 WHERE date <= :d
        """), {"d": target_date - timedelta(days=VELOCITY_LOOKBACK_DAYS)}).scalar()
        now_mom_ref_map = {}
        if now_mom_ref_date:
            nm_rows = conn.execute(text(
                "SELECT symbol, rs_rating FROM rs_daily_v2 WHERE date = :d"
            ), {"d": now_mom_ref_date}).fetchall()
            now_mom_ref_map = {r[0]: r[1] for r in nm_rows}
                
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
            if rs_val is not None and prev_rs is not None:
                if rs_val > prev_rs:
                    dirn = "up"
                    if cat != prev_cat:
                        signals.append("up")  # ⬆ Upgraded Category
                elif rs_val < prev_rs:
                    dirn = "down"
                    if cat != prev_cat:
                        signals.append("dn")  # ⬇ Downgraded Category
                else:
                    dirn = "flat"
            else:
                dirn = "flat"
                
            # Signals that need market-wide context (focus/dist/res) applied in pass 2
            ad_rating = rs_row[9] or "C"

            # 🔥 Burst signal (Kullamagi style: short-term movers, rank_1m >= 95)
            if rs_row[4] is not None and rs_row[4] >= 95:
                signals.append("burst")

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
            sma200_rising = 1 if im.get("sma200_rising") else 0
            
            tt = [
                ["P > 150MA & 200MA", 1 if (close_val > sma150 and close_val > sma200) else 0],
                ["150MA > 200MA", 1 if sma150 > sma200 else 0],
                ["200MA rising (1M)", sma200_rising],
                ["50MA > 150MA > 200MA", 1 if (sma50 > sma150 and sma150 > sma200) else 0],
                ["P > 50MA", 1 if close_val > sma50 else 0],
                ["≥30% above 52W low", 1 if offl >= 30.0 else 0],
                ["Within 25% of 52W high", 1 if offh >= -25.0 else 0],
                ["RS ≥ 70", 1 if (rs_val is not None and rs_val >= 70) else 0]
            ]
            tts = sum(check[1] for check in tt)
            
            # بناء الـ Trailing array لـ Rotation chart
            # mom = Δ RS خلال ~4 أسابيع عند كل نقطة زمنية
            def _safe_rs(val, fallback=0.0):
                return float(val) if val is not None else fallback

            trail_list = []
            for period_name in ["1Y", "6M", "3M", "4W", "1W"]:
                t_val = trail_data.get(period_name, {}).get(sym)
                if t_val is None:
                    t_val = rs_val
                t_mom = trail_mom_data.get(period_name, {}).get(sym, 0.0)
                trail_list.append([period_name, _safe_rs(t_val), float(t_mom) if t_mom is not None else 0.0])

            now_rs = _safe_rs(rs_val)
            now_ref = now_mom_ref_map.get(sym)
            if rs_val is not None and now_ref is not None:
                now_mom = float(rs_val) - float(now_ref)
            elif rs_val is not None and prev_rs is not None:
                now_mom = float(rs_val) - float(prev_rs)
            else:
                now_mom = 0.0
            trail_list.append(["now", now_rs, now_mom])

            # mom field = weekly change (rs - rs1w) for Map / Matrix; trail uses 4W velocity
            if rs_val is not None and prev_rs is not None:
                weekly_mom = float(rs_val) - float(prev_rs)
            else:
                weekly_mom = 0.0

            m1 = rs_row[4]
            m3 = rs_row[5]
            m6 = rs_row[6]
            m9 = rs_row[7]
            m12 = rs_row[8]
            
            # Age calculation safely uses m12 or fallback if None
            effective_m12 = m12 if m12 is not None else 50
            effective_m1 = m1 if m1 is not None else 50
            age = int(effective_m1) - int(effective_m12)
            if age >= 15:
                age_tag = "YOUNG"
            elif age <= -15:
                age_tag = "MATURE"
            else:
                age_tag = "STEADY"

            # Fallback classifications for new listings / unclassified symbols
            STATIC_GROUP_MAP = {
                "4148": ("Capital Goods", "Industrials", "Capital Goods", "Industrial Machinery"),
                "2288": ("Consumer Staples", "Consumer Staples", "Food & Staples Retailing", "Food Retail"),
                "1324": ("Capital Goods", "Industrials", "Capital Goods", "Building Products"),
            }

            raw_grp = rs_row[3] or pm.get("sec")
            raw_sec = pm.get("sec")
            raw_ind = pm.get("ind")
            raw_sub = pm.get("sub")

            if not raw_grp and sym in STATIC_GROUP_MAP:
                fallback_grp, fallback_sec, fallback_ind, fallback_sub = STATIC_GROUP_MAP[sym]
                grp_val = fallback_grp
                sec_val = fallback_sec
                ind_val = fallback_ind
                sub_val = fallback_sub
            else:
                grp_val = raw_grp or "Other"
                sec_val = raw_sec or "Other"
                ind_val = raw_ind or "Other"
                sub_val = raw_sub or "Other"

            stocks_data.append({
                "s": sym,
                "c": rs_row[2] or sym,
                "grp": grp_val,
                "rs": rs_val if rs_val is not None else 1,
                "rs1w": prev_rs if prev_rs is not None else (rs_val if rs_val is not None else 1),
                "cat": cat,
                "sig": list(set(signals)),
                "m1": m1,
                "m3": m3,
                "m6": m6,
                "m9": m9,
                "m12": m12,
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
                "mom": weekly_mom,
                "dirn": dirn,
                "pos": "above_ma" if close_val > sma50 else "below_ma",
                "shariah": pm.get("shariah") or "غير متوافقة",
                "sec": pm.get("sec", "Other"),
                "ind": pm.get("ind", "Other"),
                "sub": pm.get("sub", "Other"),
                "age": age,
                "ageTag": age_tag,
            })

        # ── Pass 2: REBH reference championship rules (same as REBH-RS-Rating-MOBILE.html) ──
        # Group ranks: prefer industry_group_history.rank, else rank by avg RS
        group_rank_map = {}
        ig_date = conn.execute(text(
            "SELECT MAX(date) FROM industry_group_history WHERE date <= :d"
        ), {"d": target_date}).scalar()
        if ig_date:
            ig_rows = conn.execute(text("""
                SELECT industry_group, rank FROM industry_group_history WHERE date = :d
            """), {"d": ig_date}).fetchall()
            group_rank_map = {r[0]: r[1] for r in ig_rows if r[0] and r[1] is not None}

        if not group_rank_map:
            grp_avg = {}
            for st in stocks_data:
                g = st["grp"]
                grp_avg.setdefault(g, []).append(st["rs"])
            ranked = sorted(
                ((g, sum(v) / len(v)) for g, v in grp_avg.items()),
                key=lambda x: x[1], reverse=True
            )
            group_rank_map = {g: i + 1 for i, (g, _) in enumerate(ranked)}

        # Market weekly RS median (MED_D1W)
        deltas = sorted(st["mom"] for st in stocks_data if st.get("rs1w") is not None)
        med_d1w = deltas[len(deltas) // 2] if deltas else 0.0

        focus_candidates = []
        for st in stocks_data:
            g_rank = group_rank_map.get(st["grp"])
            st["gRank"] = g_rank
            gconf = g_rank is not None and g_rank <= 5
            st["gconf"] = gconf

            trail_vals = [p[1] for p in st.get("trail") or [] if isinstance(p[1], (int, float))]
            # Ryan: RS ≥ 70 and at/above every trail checkpoint (1Y high)
            rsnh = (
                st["rs"] >= 70
                and len(trail_vals) >= 4
                and st["rs"] >= max(trail_vals)
            )
            st["rsnh"] = rsnh

            d = st["mom"]  # weekly ΔRS
            # Minervini: gaining RS while market median week is negative
            res = med_d1w < 0 and d is not None and d >= 3 and st["rs"] >= 70
            st["res"] = res

            # Ritchie: was 80+ a week ago and bleeding fast (Δ ≤ -4)
            dist = st.get("rs1w") is not None and st["rs1w"] >= 80 and d <= -4
            st["dist"] = dist

            # Zanger focus raw: elite + not mature + top-5 group
            focus_raw = st["rs"] >= 90 and st["ageTag"] != "MATURE" and gconf
            st["_focusRaw"] = focus_raw
            if focus_raw:
                focus_candidates.append(st)

            # Sync sig flags (remove old focus/dist if any, re-apply reference rules)
            sig = set(st.get("sig") or [])
            sig.discard("focus")
            sig.discard("dist")
            # Keep rsnh in sync with Ryan rule (may already be set in pass 1)
            if rsnh:
                sig.add("rsnh")
            else:
                sig.discard("rsnh")
            if res:
                sig.add("res")
            if dist:
                sig.add("dist")
            # burst kept from pass 1 if present
            st["sig"] = list(sig)

        # Zanger cap: focus list is a hand — top 10 by RS
        focus_candidates.sort(key=lambda x: x["rs"], reverse=True)
        focus_syms = {st["s"] for st in focus_candidates[:10]}
        for st in stocks_data:
            is_focus = st["s"] in focus_syms
            st["focus"] = is_focus
            st.pop("_focusRaw", None)
            if is_focus:
                if "focus" not in st["sig"]:
                    st["sig"].append("focus")
            elif "focus" in st["sig"]:
                st["sig"] = [s for s in st["sig"] if s != "focus"]

        # groups payload (like DATA.groups in reference)
        groups_out = [
            {"name": name, "rank": rank}
            for name, rank in sorted(group_rank_map.items(), key=lambda x: x[1] or 999)
        ]
            
        # 10. إعداد هيكل البيانات النهائي
        output_json = {
            "stocks": stocks_data,
            "groups": groups_out,
            "meta": {
                "date": str(target_date),
                "med_d1w": med_d1w,
            },
        }
        
        # حفظ الملف في مجلد static بالفرونتيند ومجلد الكاش
        output_dir = Path(settings.OUTPUT_DIR) if hasattr(settings, "OUTPUT_DIR") else Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "app" / "static"
        os.makedirs(output_dir, exist_ok=True)
        
        json_path = output_dir / "rs_data.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(output_json, f, ensure_ascii=False, indent=2)
            
        logger.info(f"✅ Successfully exported RS Hub data to {json_path}")
        
        # 11. رفع الملف إلى Cloudflare R2
        r2_account_id = os.getenv("R2_ACCOUNT_ID")
        r2_access_key = os.getenv("R2_ACCESS_KEY_ID")
        r2_secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
        r2_bucket = os.getenv("R2_BUCKET_NAME", "lumivst-xbrl")
        
        if r2_account_id and r2_access_key and r2_secret_key:
            logger.info("☁️ Uploading rs_data.json to Cloudflare R2...")
            try:
                import boto3
                from botocore.config import Config
                endpoint_url = f"https://{r2_account_id}.r2.cloudflarestorage.com"
                s3_client = boto3.client(
                    "s3",
                    endpoint_url=endpoint_url,
                    aws_access_key_id=r2_access_key,
                    aws_secret_access_key=r2_secret_key,
                    config=Config(signature_version="s3v4"),
                )
                s3_client.upload_file(
                    Filename=str(json_path),
                    Bucket=r2_bucket,
                    Key="rs/rs_data.json",
                    ExtraArgs={"ContentType": "application/json"}
                )
                logger.info(f"✅ Successfully uploaded rs_data.json to R2 bucket '{r2_bucket}' at key 'rs/rs_data.json'")
            except Exception as e:
                logger.error(f"❌ Failed to upload rs_data.json to R2: {e}")
        else:
            logger.info("ℹ️ R2 credentials not found in env, skipping R2 upload.")
            
        return True

if __name__ == "__main__":
    export_rs_hub_data()
