from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Optimized for Supabase - Performance Tuned
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,  # Check connection health before using
    pool_recycle=1800,  # Recycle connections every 30 minutes
    pool_size=10,  # Increased pool size for better concurrency
    max_overflow=20,  # Allow more overflow connections
    pool_timeout=10,  # Reduced timeout for faster failures
    echo=False,  # Disable SQL logging for performance
    future=True,  # Use SQLAlchemy 2.0 style
    connect_args={
        "connect_timeout": 5,  # Faster connection timeout
        "application_name": "akbpo_backend",
        "options": "-c statement_timeout=10s"  # Reduced from 30s to 10s
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
