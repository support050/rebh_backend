
import os
from sqlalchemy import create_engine, inspect, text
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def inspect_db():
    if not DATABASE_URL:
        print("❌ DATABASE_URL not found in .env")
        return

    print(f"Connecting to database...")
    try:
        engine = create_engine(DATABASE_URL)
        inspector = inspect(engine)
        
        # Get all table names
        tables = inspector.get_table_names()
        
        print(f"\nFound {len(tables)} tables in the database:\n")
        print(f"{'Table Name':<30} | {'Rows':<10}")
        print("-" * 45)
        
        with engine.connect() as connection:
            for table_name in sorted(tables):
                try:
                    result = connection.execute(text(f"SELECT COUNT(*) FROM \"{table_name}\""))
                    count = result.scalar()
                    print(f"{table_name:<30} | {count:<10}")
                except Exception as e:
                    print(f"{table_name:<30} | Error: {e}")
                    
    except Exception as e:
        print(f"❌ Connection error: {e}")

if __name__ == "__main__":
    inspect_db()
