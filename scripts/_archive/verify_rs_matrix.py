"""
اسكربت التحقق من صحة بيانات RS Matrix
يقرأ مباشرة من قاعدة البيانات (بدون حاجة لـ API أو authentication)
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from app.core.config import settings


def get_category(rs):
    if rs >= 90: return 'STRONG'
    if rs >= 80: return 'IMPROVE'
    if rs >= 70: return 'NEUTRAL'
    return 'WEAK'


def main():
    print("🔍 بدء التحقق من بيانات RS Matrix (من قاعدة البيانات مباشرة)...\n")

    engine = create_engine(str(settings.DATABASE_URL))

    with engine.connect() as conn:
        # 1. Get latest date
        latest_date = conn.execute(text("SELECT MAX(date) FROM rs_daily_v2")).scalar()
        if not latest_date:
            print("❌ لا توجد بيانات في جدول rs_daily_v2!")
            return

        prev_date = conn.execute(text(
            "SELECT MAX(date) FROM rs_daily_v2 WHERE date < :d"
        ), {"d": latest_date}).scalar()

        print(f"📅 آخر تاريخ: {latest_date}")
        print(f"📅 التاريخ السابق: {prev_date}\n")

        # 2. Get current data
        rows = conn.execute(text("""
            SELECT symbol, rs_rating, company_name FROM rs_daily_v2
            WHERE date = :d AND rs_rating IS NOT NULL
            ORDER BY rs_rating DESC
        """), {"d": latest_date}).fetchall()

        print(f"✅ عدد الأسهم اليوم: {len(rows)}")

        # 3. Get previous data
        prev_map = {}
        if prev_date:
            prev_rows = conn.execute(text("""
                SELECT symbol, rs_rating, company_name FROM rs_daily_v2
                WHERE date = :d AND rs_rating IS NOT NULL
            """), {"d": prev_date}).fetchall()
            prev_map = {r[0]: r[1] for r in prev_rows}
            print(f"✅ عدد الأسهم أمس: {len(prev_map)}")

        # 4. Analyze
        categories = {'STRONG': [], 'IMPROVE': [], 'NEUTRAL': [], 'WEAK': []}
        up_stocks = []
        down_stocks = []
        same_stocks = []
        category_movers = []  # stocks that changed category
        missing_prev = 0

        for row in rows:
            symbol, rs, name = row[0], row[1], row[2] or row[0]
            cat = get_category(rs)
            categories[cat].append({'symbol': symbol, 'name': name, 'rs': rs})

            prev_rs = prev_map.get(symbol)
            if prev_rs is None:
                missing_prev += 1
                continue

            if prev_rs < rs:
                up_stocks.append({'symbol': symbol, 'name': name, 'prev': prev_rs, 'current': rs, 'diff': rs - prev_rs})
            elif prev_rs > rs:
                down_stocks.append({'symbol': symbol, 'name': name, 'prev': prev_rs, 'current': rs, 'diff': rs - prev_rs})
            else:
                same_stocks.append(symbol)

            # Check category change
            prev_cat = get_category(prev_rs)
            if cat != prev_cat:
                cat_order = ['WEAK', 'NEUTRAL', 'IMPROVE', 'STRONG']
                direction = '↑' if cat_order.index(cat) > cat_order.index(prev_cat) else '↓'
                category_movers.append({
                    'symbol': symbol,
                    'name': name,
                    'prev_rs': prev_rs,
                    'current_rs': rs,
                    'from_cat': prev_cat,
                    'to_cat': cat,
                    'direction': direction,
                })

        # 5. Print results
        print(f"\n{'='*60}")
        print("📊 توزيع الأسهم حسب الفئة:")
        print(f"{'='*60}")
        total = len(rows)
        for cat_name in ['STRONG', 'IMPROVE', 'NEUTRAL', 'WEAK']:
            count = len(categories[cat_name])
            pct = (count / total * 100) if total > 0 else 0
            print(f"  {cat_name:10s}: {count:4d} ({pct:5.1f}%)")

        print(f"\n{'='*60}")
        print("📈 حركة الأسهم مقارنة بالأمس:")
        print(f"{'='*60}")
        print(f"  🔼 ارتفع:   {len(up_stocks)}")
        print(f"  🔽 انخفض:   {len(down_stocks)}")
        print(f"  ➡️  ثابت:    {len(same_stocks)}")
        print(f"  ⚠️  بدون سابق: {missing_prev}")

        # 6. Category movers (what the frontend shows as "FROM STRONG", "FROM NEUTRAL", etc.)
        print(f"\n{'='*60}")
        print("🔄 أسهم غيّرت الفئة (اللي بيظهر عليها FROM badge):")
        print(f"{'='*60}")
        if category_movers:
            for m in sorted(category_movers, key=lambda x: x['current_rs'], reverse=True):
                arrow_color = '🟢' if m['direction'] == '↑' else '🔴'
                print(f"  {arrow_color} {m['symbol']:6s} {m['name']:20s}  {m['prev_rs']:3d} → {m['current_rs']:3d}  | FROM {m['from_cat']} → {m['to_cat']}")
        else:
            print("  (لا توجد أسهم غيرت فئتها)")

        # 7. Frontend logic verification
        print(f"\n{'='*60}")
        print("✓ التحقق من منطق العرض في الواجهة:")
        print(f"{'='*60}")

        # The green ↑ arrow in stock name area
        name_arrows = [s for s in up_stocks]
        print(f"  سهم أخضر ↑ (بجانب الاسم): {len(name_arrows)} سهم")
        if name_arrows[:5]:
            for s in sorted(name_arrows, key=lambda x: x['diff'], reverse=True)[:5]:
                print(f"    • {s['symbol']:6s} {s['name']:20s}: {s['prev']} → {s['current']} (+{s['diff']})")

        # FROM badge (category movers only)
        print(f"  FROM badge (تغيير فئة): {len(category_movers)} سهم")

        # 8. Top performers
        if up_stocks:
            top = max(up_stocks, key=lambda x: x['diff'])
            print(f"\n🏆 أكبر ارتفاع: {top['symbol']} ({top['name']}) (+{top['diff']})")
        if down_stocks:
            worst = min(down_stocks, key=lambda x: x['diff'])
            print(f"📉 أكبر انخفاض: {worst['symbol']} ({worst['name']}) ({worst['diff']})")

        print(f"\n✅ التحقق اكتمل بنجاح!")


if __name__ == "__main__":
    main()
