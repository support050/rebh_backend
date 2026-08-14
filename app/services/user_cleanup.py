"""Shared helper for safely deleting a user and all FK-dependent data."""

import logging

from sqlalchemy.orm import Session

from app.models.user import User
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
      - user_prefs         (user_id PK, no FK constraint — orphan cleanup)
      - users.approved_by  (self-ref FK with SET NULL — explicit for safety)

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

    # User preferences (no FK constraint, but data should be cleaned)
    counts["user_prefs"] = (
        db.query(UserPreference)
        .filter(UserPreference.user_id == user_id)
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
