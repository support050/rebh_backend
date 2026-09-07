"""
REBH Company Classification Service
Provides the 7-pillar classification framework required by the Developer Package:
1. Industry Class (Growing / Cyclical / Defensive / Financial)
2. Market Form (Monopoly / Oligopoly / Monopolistic Competition / Perfect Competition)
3. Price Elasticity (Inelastic / Unit Elastic / Elastic)
4. BCG Matrix Position (Stars / Cash Cows / Question Marks / Dogs)
5. Competitive Dominance (Market Leader / Strong Challenger / Niche Follower)
6. Retail Path & Pricing Power (B2B Contractual / Retail Brand Power / Commodity Price-Taker)
7. Owner-Editable Overrides: Persists custom user classifications without altering raw source data.
"""
from typing import Dict, Any, Optional
import json
import os
from sqlalchemy import Column, String, DateTime, Text
from sqlalchemy.sql import func
from app.core.database import Base, SessionLocal, engine

class CompanyClassificationOverride(Base):
    __tablename__ = "company_classification_overrides"

    symbol = Column(String(20), primary_key=True, index=True)
    industry_class = Column(String(50), nullable=True)
    market_form = Column(String(50), nullable=True)
    price_elasticity = Column(String(50), nullable=True)
    bcg_position = Column(String(50), nullable=True)
    dominance = Column(String(50), nullable=True)
    retail_path = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

# Create table if not exists
Base.metadata.create_all(bind=engine, tables=[CompanyClassificationOverride.__table__])


# Default sector heuristics mapping
SECTOR_DEFAULTS = {
    "Materials": {
        "industry_class": "Cyclical (دورية)",
        "market_form": "Oligopoly (احتكار قلة)",
        "price_elasticity": "Elastic (مرنة / متأثرة بأسعار السلع)",
        "bcg_position": "Cash Cows (أبقار نقدية)",
        "dominance": "Strong Challenger",
        "retail_path": "Commodity Price-Taker (تسعير سلع عالمية)"
    },
    "Energy": {
        "industry_class": "Cyclical (دورية كبرى)",
        "market_form": "Oligopoly / Regulated Concession",
        "price_elasticity": "Inelastic in Short-Run",
        "bcg_position": "Cash Cows (أبقار نقدية عملاقة)",
        "dominance": "Market Leader (قائد السوق)",
        "retail_path": "B2B Contractual & Export"
    },
    "Banks": {
        "industry_class": "Financial (مالية مصرفية)",
        "market_form": "Regulated Oligopoly (احتكار قلة منظم)",
        "price_elasticity": "Moderate",
        "bcg_position": "Cash Cows",
        "dominance": "Market Leader",
        "retail_path": "Retail Brand & Institutional"
    },
    "Telecommunication Services": {
        "industry_class": "Defensive / Growth (دفاعية مع نمو بيانات)",
        "market_form": "Oligopoly (3-Player Market)",
        "price_elasticity": "Inelastic (عديمة المرونة نسبياً)",
        "bcg_position": "Cash Cows / Stars",
        "dominance": "Market Leader",
        "retail_path": "Direct Consumer Retail & B2B"
    },
    "Food & Beverages": {
        "industry_class": "Defensive (دفاعية استهلاكية أساسية)",
        "market_form": "Monopolistic Competition",
        "price_elasticity": "Inelastic",
        "bcg_position": "Cash Cows",
        "dominance": "Strong Brand Power",
        "retail_path": "Direct Retail FMCG"
    },
    "Health Care": {
        "industry_class": "Growing Defensive (نمو دفاعي)",
        "market_form": "Oligopoly",
        "price_elasticity": "Highly Inelastic (حاجة ضرورية)",
        "bcg_position": "Stars (نجوم نمو)",
        "dominance": "Strong Challenger",
        "retail_path": "Insurance & Private Out-of-Pocket"
    }
}


def get_company_classification(symbol: str, sector: Optional[str] = None) -> Dict[str, Any]:
    """
    Returns the 7-pillar classification for a company, factoring in:
    1. Owner-editable DB override (if saved).
    2. Sector-specific rule heuristic.
    3. General corporate default.
    """
    db = SessionLocal()
    try:
        override = db.query(CompanyClassificationOverride).filter(CompanyClassificationOverride.symbol == str(symbol)).first()
        if override:
            return {
                "symbol": symbol,
                "is_override": True,
                "industry_class": override.industry_class,
                "market_form": override.market_form,
                "price_elasticity": override.price_elasticity,
                "bcg_position": override.bcg_position,
                "dominance": override.dominance,
                "retail_path": override.retail_path,
                "notes": override.notes,
                "source": "Owner-Edited Custom Override"
            }
    finally:
        db.close()

    # Fallback to sector heuristic
    matched_sec = "Other"
    if sector:
        for s_key in SECTOR_DEFAULTS:
            if s_key.lower() in sector.lower():
                matched_sec = s_key
                break

    defaults = SECTOR_DEFAULTS.get(matched_sec, {
        "industry_class": "Growing (متنامية)",
        "market_form": "Monopolistic Competition (منافسة احتكارية)",
        "price_elasticity": "Unit Elastic (معتدلة)",
        "bcg_position": "Question Marks (علامات استفهام)",
        "dominance": "Niche Follower",
        "retail_path": "Mixed Commercial"
    })

    return {
        "symbol": symbol,
        "is_override": False,
        "industry_class": defaults["industry_class"],
        "market_form": defaults["market_form"],
        "price_elasticity": defaults["price_elasticity"],
        "bcg_position": defaults["bcg_position"],
        "dominance": defaults["dominance"],
        "retail_path": defaults["retail_path"],
        "notes": f"Derived from {matched_sec} benchmark archetype",
        "source": "≈ Sector Benchmark Archetype"
    }


def save_company_classification_override(
    symbol: str,
    industry_class: Optional[str] = None,
    market_form: Optional[str] = None,
    price_elasticity: Optional[str] = None,
    bcg_position: Optional[str] = None,
    dominance: Optional[str] = None,
    retail_path: Optional[str] = None,
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """
    Saves an owner-editable classification override for a specific company symbol.
    """
    db = SessionLocal()
    try:
        row = db.query(CompanyClassificationOverride).filter(CompanyClassificationOverride.symbol == str(symbol)).first()
        if not row:
            row = CompanyClassificationOverride(symbol=str(symbol))
            db.add(row)

        if industry_class is not None: row.industry_class = industry_class
        if market_form is not None: row.market_form = market_form
        if price_elasticity is not None: row.price_elasticity = price_elasticity
        if bcg_position is not None: row.bcg_position = bcg_position
        if dominance is not None: row.dominance = dominance
        if retail_path is not None: row.retail_path = retail_path
        if notes is not None: row.notes = notes

        db.commit()
        return {"status": "ok", "symbol": symbol, "message": "Classification override saved successfully"}
    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()
