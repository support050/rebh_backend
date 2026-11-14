import os
import sys

# Add project root to sys.path so we can import app modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal
from app.models.valuation_zones import ValuationZone

ZONES = [
    {
        "label": "Golden Zone",
        "label_ar": "منطقة ذهبية نادرة",
        "price_from": 4400,
        "price_to": 4700,
        "return_pct_low": 21,
        "return_pct_high": 24,
        "color_code": "green",
        "order_seq": 1
    },
    {
        "label": "Zone 2",
        "label_ar": "السعر من 5000 لـ 5300",
        "price_from": 5000,
        "price_to": 5300,
        "return_pct_low": 16,
        "return_pct_high": 19,
        "color_code": "yellow",
        "order_seq": 2
    },
    {
        "label": "Zone 3",
        "label_ar": "السعر من 5600 لـ 6400",
        "price_from": 5600,
        "price_to": 6400,
        "return_pct_low": 10,
        "return_pct_high": 14,
        "color_code": "orange",
        "order_seq": 3
    },
    {
        "label": "Zone 4",
        "label_ar": "السعر من 7000 لـ 7500",
        "price_from": 7000,
        "price_to": 7500,
        "return_pct_low": 4,
        "return_pct_high": 6,
        "color_code": "red",
        "order_seq": 4
    },
    {
        "label": "Zone 5 (Current)",
        "label_ar": "المنطقة المناسبة بناء على العوائد المرتفعة الان",
        "price_from": 8100,
        "price_to": 99999, # Infinity/Above
        "return_pct_low": None,
        "return_pct_high": None,
        "color_code": "red",
        "order_seq": 5
    }
]

def seed_zones():
    db = SessionLocal()
    try:
        # Clear existing
        db.query(ValuationZone).delete()
        
        for zone_data in ZONES:
            zone = ValuationZone(**zone_data)
            db.add(zone)
            
        db.commit()
        print("✅ Valuation zones seeded successfully.")
    except Exception as e:
        db.rollback()
        print(f"❌ Error seeding zones: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    seed_zones()
