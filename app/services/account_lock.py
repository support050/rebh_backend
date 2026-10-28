"""Account lockout state — one source of truth for ``is_locked``.

Why this lives in a service instead of ``routes/auth.py``: the lock is read in
three places (login, refresh-token, and every authenticated request through
``app.api.deps``), and ``deps.py`` cannot import the auth router without a
circular dependency. Before this, the two readers disagreed:

* ``_account_is_temporarily_locked`` reset the flag **in memory only**. The
  caller's ``db.commit()`` sat inside the ``if locked:`` branch, so the
  auto-unlock path never reached it, and ``get_db`` closed the session with a
  rollback. The row stayed ``is_locked = true`` forever.
* ``deps.get_current_user`` refused every request while ``is_locked`` was set,
  so the account was trapped in "authenticated but 401 on every call" — while
  ``/refresh-token`` (which only consulted the temp helper) kept minting fresh
  access tokens for the same locked row.

Both readers now go through :func:`resolve_lockout`, which persists the expiry
before answering.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.user import User

logger = logging.getLogger(__name__)


def is_temporarily_locked(user: User) -> bool:
    """Pure predicate on the in-memory row — no session, no writes.

    Returns True while a lock is in force. An *expired* temporary lock clears the
    in-memory fields and returns False; the caller must persist that (use
    :func:`resolve_lockout` whenever a database session is at hand).
    """
    if user.locked_until and user.locked_until > datetime.utcnow():
        return True
    if user.is_locked and user.locked_until and user.locked_until <= datetime.utcnow():
        # Temporary lockout has elapsed — self-heal.
        user.is_locked = False
        user.locked_until = None
        user.failed_login_attempts = 0
        return False
    if user.is_locked and not user.locked_until:
        # Manual/administrative lock with no expiry — only /unlock clears it.
        return True
    return False


def resolve_lockout(db: Session, user: User) -> bool:
    """Is `user` locked right now, healing + COMMITTING an expired lockout.

    The commit is the whole point: without it the reset dies with the request
    and the account stays locked while the code believes it is unlocked.
    """
    was_locked = bool(user.is_locked)
    locked = is_temporarily_locked(user)

    if was_locked and not locked:
        try:
            db.commit()
            logger.info(
                "Expired lockout cleared and persisted user_id=%s", user.id
            )
        except Exception:  # noqa: BLE001 — a failed heal must not mask the answer
            db.rollback()
            logger.exception("Could not persist expired lockout user_id=%s", user.id)

    return locked
