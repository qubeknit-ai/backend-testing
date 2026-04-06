from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("❌ ERROR: DATABASE_URL environment variable is not set!")
    # In Vercel, this will show up in the logs and help diagnose connectivity issues.
    # We raise an Informative error instead of just letting create_engine fail with None.
    raise ValueError("DATABASE_URL is missing. Please set it in your environment or Vercel dashboard.")

# Serverless-optimised for Vercel + Supabase pooler (port 6543)
# Low pool_size prevents connection exhaustion when many Vercel instances run concurrently
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,     # Drop stale connections before use
    pool_recycle=300,       # 5 min — short recycle suits serverless lifetimes
    pool_size=15,           # 15 per instance; GitHub Action runs all users in one instance
    max_overflow=10,        # Max 25 total per instance
    pool_timeout=30,        # Wait up to 30s to acquire a connection
    echo=False,
    future=True,
    connect_args={
        "connect_timeout": 10,
        "application_name": "akbpo_backend",
        "options": "-c statement_timeout=30s"
    }
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
