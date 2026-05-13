
import os
import sys
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("DATABASE_URL not found")
    sys.exit(1)

engine = create_engine(DATABASE_URL)

with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE truelancer_auto_bid_settings ADD COLUMN skill_matching BOOLEAN DEFAULT TRUE"))
        conn.commit()
        print("Successfully added skill_matching column to truelancer_auto_bid_settings")
    except Exception as e:
        print(f"Error: {e}")
