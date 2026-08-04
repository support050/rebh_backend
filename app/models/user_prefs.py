from sqlalchemy import Column, Integer, Text, DateTime
from sqlalchemy.sql import func
from app.core.database import Base

class UserPreference(Base):
    __tablename__ = "user_prefs"
    
    user_id = Column(Integer, primary_key=True, index=True, nullable=False)
    preferences_json = Column(Text, nullable=False, default="{}")
    
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<UserPreference(user_id={self.user_id})>"
