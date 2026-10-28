"""Shared helper for safely deleting a user and all FK-dependent data."""

import logging

from sqlalchemy.orm import Session

from app.models.user import User
from app.models.breadth import UserDashboard
from app.models.rebh_user_data import (
    AnalystNote,
    CouncilCompanyChecklist,
    TradeJournal,
    UserWatchlist,
)
from app.models.user_prefs import UserPreference
from app.models.wallet import (
    WalletPosition,
    WalletSetting,
    WalletTrade,
    WalletWeeklyStudy,
)

logger = logging.getLogger(__name__)


def delete_user_related_data(db: Session, user_id: int) -> dict:
    """
    Delete all rows from FK-dependent tables that belong to the given user.

    Must be called within the same transaction as the user row deletion.
    Does NOT commit — the caller is responsible for db.commit().

    Tables cleaned (order matters for FK safety):
      - wallet_positions   (FK → users.id, no CASCADE)
      - wallet_trades      (FK → users.id, no CASCADE)
      - wallet_settings    (FK → users.id, no CASCADE)
      - wallet_weekly_studies (FK → users.id, no CASCADE)
      - analyst_notes         (per-user thesis notes)
      - trade_journals        (per-user trade entries)
      - user_watchlists       (per-user saved symbols)
      - council_company_checklists (per-user 31-point checklists)
      - user_prefs         (user_id PK, no FK constraint — orphan cleanup)
      - user_dashboards    (user_id, no FK constraint — CR3 breadth layouts)
      - users.approved_by  (self-ref FK with SET NULL — explicit for safety)

    The REBH user-data tables declare ondelete="CASCADE", but that only reaches
    tables created from that DDL; the deletes below are explicit so a user never
    leaves rows behind on a database where the constraint was never applied.

    Returns a dict of {table_name: rows_deleted} for audit logging.
    """
    counts = {}

    # Wallet data (FK → users.id, no CASCADE defined)
    counts["wallet_positions"] = (
        db.query(WalletPosition)
        .filter(WalletPosition.user_id == user_id)
        .delete(synchronize_session=False)
    )
    counts["wallet_trades"] = (
        db.query(WalletTrade)
        .filter(WalletTrade.user_id == user_id)
        .delete(synchronize_session=False)
    )
    counts["wallet_settings"] = (
        db.query(WalletSetting)
        .filter(WalletSetting.user_id == user_id)
        .delete(synchronize_session=False)
    )
    counts["wallet_weekly_studies"] = (
        db.query(WalletWeeklyStudy)
        .filter(WalletWeeklyStudy.user_id == user_id)
        .delete(synchronize_session=False)
    )

    # REBH per-user data (notes / journal / watchlist / council checklists)
    counts["analyst_notes"] = (
        db.query(AnalystNote)
        .filter(AnalystNote.user_id == user_id)
        .delete(synchronize_session=False)
    )
    counts["trade_journals"] = (
        db.query(TradeJournal)
        .filter(TradeJournal.user_id == user_id)
        .delete(synchronize_session=False)
    )
    counts["user_watchlists"] = (
        db.query(UserWatchlist)
        .filter(UserWatchlist.user_id == user_id)
        .delete(synchronize_session=False)
    )
    counts["council_company_checklists"] = (
        db.query(CouncilCompanyChecklist)
        .filter(CouncilCompanyChecklist.user_id == user_id)
        .delete(synchronize_session=False)
    )

    # User preferences (no FK constraint, but data should be cleaned)
    counts["user_prefs"] = (
        db.query(UserPreference)
        .filter(UserPreference.user_id == user_id)
        .delete(synchronize_session=False)
    )

    # Breadth custom dashboards (CR3, no FK constraint — orphan cleanup)
    counts["user_dashboards"] = (
        db.query(UserDashboard)
        .filter(UserDashboard.user_id == user_id)
        .delete(synchronize_session=False)
    )

    # Clear self-referencing approved_by on other users
    # (FK has ondelete=SET NULL, but be explicit for safety)
    db.query(User).filter(User.approved_by == user_id).update(
        {"approved_by": None}, synchronize_session=False
    )

    total = sum(counts.values())
    if total > 0:
        logger.info(
            "Deleted %d related rows for user_id=%d: %s",
            total,
            user_id,
            counts,
        )

    return counts
