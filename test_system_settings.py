"""
Test script to verify SystemSettings table and functionality
"""
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from database import SessionLocal
from models import SystemSettings

def test_system_settings():
    print("Testing SystemSettings...")
    db = SessionLocal()
    
    try:
        # Get settings
        settings = db.query(SystemSettings).first()
        
        if settings:
            print("✅ System Settings Found:")
            print(f"  Upwork Limit: {settings.default_upwork_limit}")
            print(f"  Freelancer Limit: {settings.default_freelancer_limit}")
            print(f"  Freelancer Plus Limit: {settings.default_freelancer_plus_limit}")
            print(f"  Upwork Webhook: {settings.upwork_webhook_url or 'Not set'}")
            print(f"  Freelancer Webhook: {settings.freelancer_webhook_url or 'Not set'}")
            print(f"  Freelancer Plus Webhook: {settings.freelancer_plus_webhook_url or 'Not set'}")
        else:
            print("❌ No system settings found")
            
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    test_system_settings()
