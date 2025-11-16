"""
Migration script to add SystemSettings table
Run this after updating models.py
"""
from database import engine, Base
from models import SystemSettings
from sqlalchemy.orm import Session
from database import SessionLocal

def migrate():
    print("Creating SystemSettings table...")
    
    # Create all tables (will only create new ones)
    Base.metadata.create_all(bind=engine)
    
    # Initialize default settings
    db = SessionLocal()
    try:
        existing = db.query(SystemSettings).first()
        if not existing:
            print("Creating default system settings...")
            settings = SystemSettings(
                default_upwork_limit=5,
                default_freelancer_limit=5,
                default_freelancer_plus_limit=3
            )
            db.add(settings)
            db.commit()
            print("✅ Default system settings created")
        else:
            print("✅ System settings already exist")
    except Exception as e:
        print(f"❌ Error: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    migrate()
