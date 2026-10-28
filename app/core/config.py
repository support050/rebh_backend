# app/core/config.py
"""Centralized, validated application settings. Fails fast on missing required secrets."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import List

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Accepted ENVIRONMENT values. Anything else is a typo, not an environment.
ALLOWED_ENVIRONMENTS = frozenset({"production", "staging", "development", "local", "test"})
# The only values allowed to run with plaintext (non-`Secure`) cookies.
INSECURE_COOKIE_ENVIRONMENTS = frozenset({"development", "local", "test"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        populate_by_name=True,
    )

    # ── Required infrastructure ──────────────────────────────────────────
    DATABASE_URL: str
    REDIS_URL: str
    SECRET_KEY: str = Field(min_length=32)

    # ── Environment ──────────────────────────────────────────────────────
    DEBUG: bool = False
    ENVIRONMENT: str = "production"

    # ── Schema bootstrap ─────────────────────────────────────────────────
    # Alembic is the schema authority; create_all() on boot only masks drift by
    # silently adding missing tables. Keep it on for local/dev convenience and
    # turn it off in production (the Procfile/Docker path runs the migration).
    AUTO_CREATE_TABLES: bool = True

    # ── Email verification enforcement (M-6) ──────────────────────────────
    # ON as the permanent default (2026-10-29): the 12 approved-but-unverified
    # legacy accounts were audited (all zero-data test throwaways), backed up
    # and purged, so no live user can be locked out by this gate. Every
    # registered user must verify their email before accessing the app.
    # It remains a setting (not hardcoded) only as an emergency kill-switch —
    # e.g. if the verification-mail provider is down during an onboarding push.
    ENFORCE_EMAIL_VERIFICATION: bool = True

    # ── JWT ──────────────────────────────────────────────────────────────
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    # ── CORS / URLs (comma-separated in env) ─────────────────────────────
    ALLOWED_ORIGINS_RAW: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000",
        validation_alias="ALLOWED_ORIGINS",
    )
    FRONTEND_URL: str = "http://localhost:3000"

    # ── External APIs ────────────────────────────────────────────────────
    TWELVE_DATA_API_KEY: str | None = None
    FRED_API_KEY: str | None = None
    INTERNAL_API_KEY: str | None = None
    BASE_URL: str = "https://api.twelvedata.com"

    # ── SMTP ─────────────────────────────────────────────────────────────
    SMTP_SERVER: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    FROM_EMAIL: str = "noreply@rebh.ai"
    SMTP_USE_TLS: bool = True
    SMTP_TIMEOUT_SECONDS: int = 30

    # ── OAuth ────────────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    FACEBOOK_CLIENT_ID: str | None = None
    FACEBOOK_CLIENT_SECRET: str | None = None

    # ── Brute-force / session security ───────────────────────────────────
    MAX_FAILED_LOGIN_ATTEMPTS: int = 5
    # Counter for failures against the SAME account from ONE IP. A lockout keyed
    # only on the account is a denial-of-service gift: five typos aimed at
    # somebody else's email lock that person out. The soft cap is the ceiling
    # across any mix of IPs, so a distributed attempt is still bounded.
    MAX_FAILED_LOGIN_ATTEMPTS_SOFT_CAP: int = 15
    LOCKOUT_DURATION_MINUTES: int = 30
    MAX_CONCURRENT_SESSIONS: int = 5

    # ── Pending-approval SSE ─────────────────────────────────────────────
    # A held stream is a held Redis pubsub and a worker connection, so each one
    # gets a hard lifetime plus a credential re-check interval.
    PENDING_SSE_MAX_SECONDS: int = 900
    SSE_RECHECK_SECONDS: int = 15

    # ── Password reset / verification ────────────────────────────────────
    RESET_TOKEN_EXPIRE_MINUTES: int = 15
    VERIFICATION_TOKEN_EXPIRE_MINUTES: int = 60

    # ── Security audit trail ─────────────────────────────────────────────
    # Empty = audit records go to the application log stream only. A path
    # (absolute or relative to the backend root) additionally writes JSON Lines
    # suited to a log drain.
    AUDIT_LOG_PATH: str = ""

    @field_validator("SECRET_KEY")
    @classmethod
    def secret_key_strength(cls, v: str) -> str:
        weak = {"changeme", "secret", "dev", "test", "your-secret-key"}
        if v.strip().lower() in weak:
            raise ValueError("SECRET_KEY is too weak")
        return v

    @field_validator("ENVIRONMENT")
    @classmethod
    def normalize_environment(cls, v: str) -> str:
        """Canonicalise and pin the environment name.

        A typo used to be harmless-looking but catastrophic: any value other
        than the exact string "production" skipped every production hard check
        AND made the auth cookies non-`Secure`. Rejecting unknown names here
        turns that silent misconfiguration into a boot failure.
        """
        normalized = (v or "production").strip().lower()
        if normalized not in ALLOWED_ENVIRONMENTS:
            raise ValueError(
                "ENVIRONMENT must be one of "
                + ", ".join(sorted(ALLOWED_ENVIRONMENTS))
                + f" (got {v!r}). A misspelled name silently disables the "
                "production security checks and the Secure cookie flag."
            )
        return normalized

    @model_validator(mode="after")
    def validate_lockout_thresholds(self) -> "Settings":
        if self.MAX_FAILED_LOGIN_ATTEMPTS_SOFT_CAP < self.MAX_FAILED_LOGIN_ATTEMPTS:
            raise ValueError(
                "MAX_FAILED_LOGIN_ATTEMPTS_SOFT_CAP must be >= MAX_FAILED_LOGIN_ATTEMPTS, "
                "otherwise the cross-IP ceiling is stricter than the same-IP one"
            )
        return self

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        origins = [o.strip() for o in self.ALLOWED_ORIGINS_RAW.split(",") if o.strip()]

        if self.ENVIRONMENT == "production":
            if self.DEBUG:
                raise ValueError(
                    "Invalid configuration: DEBUG must be False when ENVIRONMENT=production"
                )
            if not origins or "*" in origins:
                raise ValueError(
                    "Invalid configuration: ALLOWED_ORIGINS must be an explicit "
                    "non-wildcard list when ENVIRONMENT=production"
                )
            key = (self.INTERNAL_API_KEY or "").strip()
            weak_internal = {"changeme", "secret", "dev", "test", "internal", "your-internal-api-key"}
            if not key or len(key) < 16 or key.lower() in weak_internal:
                raise ValueError(
                    "Invalid configuration: INTERNAL_API_KEY must be set to a strong "
                    "value (min 16 chars) when ENVIRONMENT=production"
                )
        elif self.DEBUG and "*" in origins:
            logger.warning(
                "SECURITY: ALLOWED_ORIGINS contains '*' while DEBUG=True. "
                "Do not use wildcard origins with credentialed cookies."
            )
        return self

    @property
    def ALLOWED_ORIGINS(self) -> List[str]:
        """
        Explicit origin list only. Never expands to ['*'] based on DEBUG.
        Production wildcard is rejected at settings validation time.
        """
        origins = [o.strip() for o in self.ALLOWED_ORIGINS_RAW.split(",") if o.strip()]
        if self.ENVIRONMENT == "production" and ("*" in origins or not origins):
            # Defense in depth if raw value is mutated after init
            raise RuntimeError("Refusing permissive CORS in production")
        return origins

    @property
    def API_KEY(self) -> str | None:
        return self.TWELVE_DATA_API_KEY

    @property
    def cookie_secure(self) -> bool:
        """Fail-secure: cookies carry `Secure` unless we are *explicitly* in a
        local scheme. Unknown names never reach this (validator), but the
        default is still True so a future bypass cannot silently downgrade."""
        return self.ENVIRONMENT not in INSECURE_COOKIE_ENVIRONMENTS


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
