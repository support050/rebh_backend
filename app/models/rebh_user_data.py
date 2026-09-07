"""
User Data Models for REBH Platform:
- AnalystNote: per-user per-symbol notes appearing in company reports and views.
- TradeJournal: persistent trade entries (shares, entry/exit price, rationale, P&L, status).
- UserWatchlist: persistent per-user stock symbols watchlist.
"""
from sqlalchemy import Column, Integer, String, Float, Text, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func
from app.core.database import Base


class AnalystNote(Base):
    __tablename__ = "analyst_notes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    note = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_analyst_note_user_symbol"),
    )

    def __repr__(self):
        return f"<AnalystNote(user_id={self.user_id}, symbol='{self.symbol}')>"


class TradeJournal(Base):
    __tablename__ = "trade_journals"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    trade_type = Column(String(10), nullable=False, default="buy")  # buy | sell
    shares = Column(Float, nullable=False)
    buy_price = Column(Float, nullable=False)
    sell_price = Column(Float, nullable=True)
    status = Column(String(20), nullable=False, default="active")  # active | closed
    reason = Column(Text, nullable=True)
    exit_reason = Column(Text, nullable=True)
    trade_date = Column(String(30), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<TradeJournal(id={self.id}, user_id={self.user_id}, symbol='{self.symbol}', status='{self.status}')>"


class UserWatchlist(Base):
    __tablename__ = "user_watchlists"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    notes = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_user_watchlist_user_symbol"),
    )

    def __repr__(self):
        return f"<UserWatchlist(user_id={self.user_id}, symbol='{self.symbol}')>"


class CouncilCompanyChecklist(Base):
    """
    Council 31-point interactive audit checklists per symbol:
    - fisher_scores: JSON dict of {itemId: score (0, 0.5, 1)}
    - danger_flags: JSON list of ticked danger sign ids
    - red_flags: JSON list of ticked red flag ids
    - bank_flags: JSON list of ticked bank flag ids
    - notes: Freeform text
    """
    __tablename__ = "council_company_checklists"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    fisher_scores = Column(Text, nullable=True)  # JSON string
    danger_flags = Column(Text, nullable=True)   # JSON string
    red_flags = Column(Text, nullable=True)      # JSON string
    bank_flags = Column(Text, nullable=True)     # JSON string
    notes = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_council_checklist_user_symbol"),
    )

    def __repr__(self):
        return f"<CouncilCompanyChecklist(user_id={self.user_id}, symbol='{self.symbol}')>"

