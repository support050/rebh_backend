"""Shared guards: internal (machine-to-machine) key verification.

The internal key fronts the scraper/ingest surface. It is a secret like any
other, so it is compared in constant time and a missing configuration is a
service fault (503) rather than a 5xx that describes our internals.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from app.core.config import settings


def verify_internal_key(x_internal_key: str | None = Header(None, alias="X-Internal-Key")):
    """
    Dependency to verify internal API key for scrape/ingest endpoints.
    Never logs the presented or configured key.
    """
    expected_key = settings.INTERNAL_API_KEY
    if not expected_key:
        # The server is misconfigured, not the caller. A generic 503 keeps the
        # response from advertising which env var is missing.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporarily unavailable",
            headers={"Retry-After": "60"},
        )

    if not x_internal_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Internal API key required",
        )

    # Constant-time: `!=` short-circuits on the first differing byte, which
    # leaks the key length and its common prefix to an off-site attacker who
    # can measure round-trip times over many requests.
    if not secrets.compare_digest(x_internal_key, expected_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid internal API key",
        )

    return True
