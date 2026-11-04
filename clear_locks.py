from app.core.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()
try:
    result = db.execute(text("""
        UPDATE job_runs 
        SET status = 'failed', 
            finished_at = NOW(), 
            error_message = 'Cleared stale lock' 
        WHERE status = 'running'
    """))
    db.commit()
    print(f"Cleared {result.rowcount} stuck locks successfully!")
except Exception as e:
    print(f"Error: {e}")
    db.rollback()
finally:
    db.close()
