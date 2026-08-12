from fastapi import Header, HTTPException, status

from app.core.config import settings


def verify_internal_key(x_internal_key: str | None = Header(None, alias="X-Internal-Key")):
    """
    Dependency to verify internal API key for scrape/ingest endpoints.
    Never logs the presented or configured key.
    """
    expected_key = settings.INTERNAL_API_KEY
    if not expected_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal API key not configured",
        )

    if not x_internal_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Internal API key required",
        )

    if x_internal_key != expected_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid internal API key",
        )

    return True
