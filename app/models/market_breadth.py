"""
Market Breadth Model
Stores pre-computed % of stocks above each Moving Average by date
"""

from sqlalchemy import Column, Date, Numeric
from app.core.database import Base


class MarketBreadth(Base):
    __tablename__ = "market_breadth"
    
    date = Column(Date, primary_key=True)
    pct_above_20 = Column(Numeric(5, 2), nullable=True)
    pct_above_50 = Column(Numeric(5, 2), nullable=True)
    pct_above_150 = Column(Numeric(5, 2), nullable=True)
    pct_above_200 = Column(Numeric(5, 2), nullable=True)

    # date is the PK; the PK's implicit index is the only one the live table
    # carries (the API reads this table ordered by date). No secondary index is
    # declared, so metadata matches prod exactly and autogenerate stays clean.
    
    def __repr__(self):
        return f"<MarketBreadth(date={self.date})>"
