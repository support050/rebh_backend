# app/core/config.py

import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

class Settings:
    def __init__(self):
        self.DATABASE_URL = os.getenv("DATABASE_URL")
        self.REDIS_URL = os.getenv("REDIS_URL")
        self.CACHE_EXPIRE_SECONDS = int(os.getenv("CACHE_EXPIRE_SECONDS", "300"))
        self.STOCK_PRICE_CACHE_SECONDS = int(os.getenv("STOCK_PRICE_CACHE_SECONDS", "300"))
        self.API_KEY = os.getenv("TWELVE_DATA_API_KEY")
        self.BASE_URL = "https://api.twelvedata.com"
        
        # إعدادات إضافية مهمة للإنتاج
        self.DEBUG = os.getenv("DEBUG", "False").lower() == "true"
        self.ENVIRONMENT = os.getenv("ENVIRONMENT", "production")

        # ⚠️ تحديث الـ CORS للإنتاج
        allowed_origins_str = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000,https://www.rebh.ai,https://rebh.ai")
        if self.DEBUG:
            self.ALLOWED_ORIGINS = ["*"]
            logger.warning(
                "SECURITY: DEBUG=True sets ALLOWED_ORIGINS=['*']. "
                "This is incompatible with allow_credentials=True in production. "
                "Never deploy with DEBUG=True."
            )
        else:
            # CORS origins = frontend domains that call the API (not the backend URL itself).
            # Set ALLOWED_ORIGINS env on Render, e.g. https://rebh.ai,https://www.rebh.ai,<vercel-url>
            self.ALLOWED_ORIGINS = [origin.strip() for origin in allowed_origins_str.split(",") if origin.strip()]

        # Email Settings
        self.SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self.SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
        self.SMTP_USER = os.getenv("SMTP_USER", "")
        self.SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
        self.FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@rebh.ai")

        # Social Login
        self.GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
        self.GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
        self.FACEBOOK_CLIENT_ID = os.getenv("FACEBOOK_CLIENT_ID")
        self.FACEBOOK_CLIENT_SECRET = os.getenv("FACEBOOK_CLIENT_SECRET")
        
        # Frontend URL for OAuth redirects
        self.FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

settings = Settings()


