"""Security audit trail (OWASP A09).

Before this module, approvals, demotions, deletions and pipeline triggers only
existed inside free-form application logs, so "who revoked whom, and did it
succeed?" was not answerable. Every privileged or irreversible identity action
now emits ONE structured line through the dedicated `security.audit` logger.

Deliberately log-backed rather than a DB table: no migration, no new failure
mode inside the revocation path (an audit write can never block a security
action), and it survives a partial transaction rollback. The line format is a
stable contract — `ts/action/actor_id/actor_email/target_user_id/outcome/
detail` — so it can be shipped by a log drain and queried, and swapped for a
table later without touching the call sites.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from app.core.config import settings

audit_logger = logging.getLogger("security.audit")
audit_logger.setLevel(logging.INFO)
# Application logs must not double-print audit lines; the dedicated handler owns them.
audit_logger.propagate = False

_json_handler = None
if settings.AUDIT_LOG_PATH:
    try:
        _json_handler = logging.FileHandler(settings.AUDIT_LOG_PATH, encoding="utf-8")
        _json_handler.setFormatter(logging.Formatter("%(message)s"))
        audit_logger.addHandler(_json_handler)
    except Exception:  # noqa: BLE001 — audit config must never break boot
        logging.getLogger(__name__).warning(
            "AUDIT_LOG_PATH unusable — falling back to stdout audit lines"
        )

_stdout_handler = None
if _json_handler is None:
    _stdout_handler = logging.StreamHandler()
    _stdout_handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.addHandler(_stdout_handler)


def audit_event(
    action: str,
    *,
    actor_id: int | None = None,
    actor_email: str | None = None,
    target_user_id: int | str | None = None,
    outcome: str = "success",
    **detail,
) -> None:
    """Emit one structured audit record. Never raises.

    `detail` is for non-sensitive context only — no passwords, no tokens, no
    key material.
    """
    record = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "action": action,
        "actor_id": actor_id,
        "actor_email": actor_email,
        "target_user_id": target_user_id,
        "outcome": outcome,
    }
    if detail:
        record["detail"] = detail
    try:
        audit_logger.info(json.dumps(record, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001 — observability must not break the request
        logging.getLogger(__name__).exception("Could not emit audit event %s", action)
