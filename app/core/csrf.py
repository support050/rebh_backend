"""CSRF protection: Origin/Referer check + real double-submit cookie/header token."""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlparse

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.config import settings

logger = logging.getLogger(__name__)

# Paths that may need a CSRF cookie issued before the client has one.
# Mutating requests still require a matching cookie+header once issued via GET /api/auth/csrf.
CSRF_COOKIE_NAME = "csrf_token"


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            path_for_match = request.url.path

            excluded_prefixes = (
                "/api/scraper",
                "/api/public",
                "/api/ingest",
                "/api/reports/admin",
            )
            if not any(path_for_match.startswith(prefix) for prefix in excluded_prefixes):
                csrf_header = request.headers.get("x-csrf-token")
                csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)

                if not csrf_header:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "CSRF verification failed. Missing x-csrf-token header."},
                    )
                if not csrf_cookie:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": "CSRF verification failed. Missing csrf_token cookie. "
                            "Call GET /api/auth/csrf first."
                        },
                    )
                # Reject legacy sentinel "1" even if cookie and header both match it
                if csrf_header == "1" or csrf_cookie == "1":
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "CSRF verification failed. Token mismatch."},
                    )
                # Double-submit: cookie and header must match (constant-time)
                if not secrets.compare_digest(csrf_header, csrf_cookie):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "CSRF verification failed. Token mismatch."},
                    )

                origin = request.headers.get("origin")
                referrer = request.headers.get("referer")
                allowed_origins = settings.ALLOWED_ORIGINS

                origin_valid = False
                if origin:
                    origin_valid = origin in allowed_origins
                elif referrer:
                    try:
                        parsed = urlparse(referrer)
                        referrer_origin = f"{parsed.scheme}://{parsed.netloc}"
                        origin_valid = referrer_origin in allowed_origins
                    except Exception:
                        origin_valid = False
                else:
                    origin_valid = False

                if not origin_valid:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "CSRF verification failed. Invalid origin or referrer."},
                    )

        response = await call_next(request)

        # Seed CSRF cookie when absent — skip if the handler already set it (e.g. GET /api/auth/csrf)
        path = request.url.path.rstrip("/")
        handler_sets_csrf = path.endswith("/auth/csrf")
        if CSRF_COOKIE_NAME not in request.cookies and not handler_sets_csrf:
            try:
                is_secure = settings.ENVIRONMENT == "production"
                response.set_cookie(
                    key=CSRF_COOKIE_NAME,
                    value=secrets.token_urlsafe(32),
                    httponly=False,
                    secure=is_secure,
                    samesite="lax",
                    max_age=14 * 24 * 60 * 60,
                    path="/",
                )
            except Exception:
                pass

        return response
