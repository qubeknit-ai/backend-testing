"""
Test script to verify scheduler is working
Run this to check if auto-fetch is configured correctly
"""
from database import SessionLocal
from models import User, UserSettings
from sqlalchemy import func

def test_scheduler():
    db = SessionLocal()
    
    try:
        print("\n=== Auto-Fetch Configuration Test ===\n")
        
        # Get all users
        users = db.query(User).all()
        print(f"Total users: {len(users)}\n")
        
        for user in users:
            print(f"User: {user.email}")
            print(f"  ID: {user.id}")
            
            # Get user settings
            settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
            
            if settings:
                print(f"  Upwork Auto-Fetch: {'✓ ENABLED' if settings.upwork_auto_fetch else '✗ Disabled'}")
                if settings.upwork_auto_fetch:
                    print(f"    Interval: {settings.upwork_auto_fetch_interval} minutes")
                    print(f"    Fetches today: {user.upwork_fetch_count or 0}")
                    print(f"    Last reset: {user.upwork_last_reset}")
                
                print(f"  Freelancer Auto-Fetch: {'✓ ENABLED' if settings.freelancer_auto_fetch else '✗ Disabled'}")
                if settings.freelancer_auto_fetch:
                    print(f"    Interval: {settings.freelancer_auto_fetch_interval} minutes")
                    print(f"    Fetches today: {user.freelancer_fetch_count or 0}")
                    print(f"    Last reset: {user.freelancer_last_reset}")
            else:
                print("  ⚠ No settings found")
            
            print()
        
        # Check for users with auto-fetch enabled
        settings_upwork = db.query(UserSettings).filter(UserSettings.upwork_auto_fetch == True).all()
        settings_freelancer = db.query(UserSettings).filter(UserSettings.freelancer_auto_fetch == True).all()
        
        print(f"Users with Upwork auto-fetch enabled: {len(settings_upwork)}")
        print(f"Users with Freelancer auto-fetch enabled: {len(settings_freelancer)}")
        
        if not settings_upwork and not settings_freelancer:
            print("\n⚠ WARNING: No users have auto-fetch enabled!")
            print("Enable auto-fetch in the frontend to test the scheduler.")
        else:
            print("\n✓ Scheduler should be processing these users every minute")
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    test_scheduler()
