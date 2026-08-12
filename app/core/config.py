# app/core/config.py
"""Centralized, validated application settings. Fails fast on missing required secrets."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import List

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


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

    # ── Cache ────────────────────────────────────────────────────────────
    CACHE_EXPIRE_SECONDS: int = 300
    STOCK_PRICE_CACHE_SECONDS: int = 300

    # ── External APIs ────────────────────────────────────────────────────
    TWELVE_DATA_API_KEY: str | None = None
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
    LOCKOUT_DURATION_MINUTES: int = 30
    MAX_CONCURRENT_SESSIONS: int = 5
    REFRESH_LOCK_TTL_SECONDS: int = 10

    # ── Password reset / verification ────────────────────────────────────
    RESET_TOKEN_EXPIRE_MINUTES: int = 15
    VERIFICATION_TOKEN_EXPIRE_MINUTES: int = 60

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
        return (v or "production").lower()

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
