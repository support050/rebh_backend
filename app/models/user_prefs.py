from sqlalchemy import Column, Integer, Text, DateTime
from sqlalchemy.sql import func
import json
from app.core.database import Base

class UserPreference(Base):
    __tablename__ = "user_prefs"
    
    user_id = Column(Integer, primary_key=True, index=True, nullable=False)
    preferences_json = Column(Text, nullable=False, default="{}")
    
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    @property
    def preferences(self) -> dict:
        try:
            return json.loads(self.preferences_json or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}

    @preferences.setter
    def preferences(self, value):
        if isinstance(value, str):
            self.preferences_json = value
        else:
            self.preferences_json = json.dumps(value if value is not None else {}, ensure_ascii=False)

    def __repr__(self):
        return f"<UserPreference(user_id={self.user_id})>"
