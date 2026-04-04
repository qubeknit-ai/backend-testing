from fastapi import HTTPException, Depends
from sqlalchemy.orm import Session
from datetime import datetime
from sqlalchemy import func
import json
from models import *
from auth_utils import verify_token
from database import SessionLocal

def get_db():
    """Database session optimized for Supabase transaction pooler"""
    from database import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_user_by_email(email: str, db: Session):
    """Helper function to get current user from email"""
    from models import User
    user = db.query(User).filter(func.lower(User.email) == email.lower()).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user

def get_system_settings(db: Session):
    """Get or create system settings"""
    from models import SystemSettings
    settings = db.query(SystemSettings).first()
    if not settings:
        settings = SystemSettings(
            default_upwork_limit=5,
            default_freelancer_limit=5,
            default_freelancer_plus_limit=3
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings

def check_and_reset_daily_limit(user, platform: str, db: Session):
    """
    Check and reset daily limit for a platform if needed
    Returns (current_count, daily_limit, can_fetch)
    """
    # Get limits from system settings
    system_settings = get_system_settings(db)
    LIMITS = {
        "upwork": system_settings.default_upwork_limit,
        "freelancer": system_settings.default_freelancer_limit,
        "freelancer_plus": system_settings.default_freelancer_plus_limit
    }
    DAILY_LIMIT = LIMITS.get(platform, 5)  # Default to 5 if platform not found
    now = datetime.utcnow()
    
    # Get the appropriate fields based on platform
    if platform == "upwork":
        count_field = "upwork_fetch_count"
        reset_field = "upwork_last_reset"
    elif platform == "freelancer":
        count_field = "freelancer_fetch_count"
        reset_field = "freelancer_last_reset"
    elif platform == "freelancer_plus":
        count_field = "freelancer_plus_fetch_count"
        reset_field = "freelancer_plus_last_reset"
    else:
        raise ValueError(f"Unknown platform: {platform}")
    
    last_reset = getattr(user, reset_field)
    current_count = getattr(user, count_field) or 0
    
    # Check if it's a new day (reset at midnight UTC)
    if last_reset:
        last_reset_date = last_reset.date()
        current_date = now.date()
        
        if current_date > last_reset_date:
            # New day, reset counter
            setattr(user, count_field, 0)
            setattr(user, reset_field, now)
            current_count = 0
            db.commit()
            print(f"Reset {platform} daily fetch count for user {user.email}")
    else:
        # First time fetching, initialize
        setattr(user, reset_field, now)
        setattr(user, count_field, 0)
        current_count = 0
        db.commit()
    
    can_fetch = current_count < DAILY_LIMIT
    remaining = DAILY_LIMIT - current_count
    
    return current_count, DAILY_LIMIT, can_fetch, remaining

def verify_admin(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    """Verify that the current user is an admin"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    user = get_user_by_email(email, db)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

def prepare_freelancer_request(credentials):
    """Prepare headers and cookies for Freelancer API requests"""
    headers = {"Content-Type": "application/json"}
    cookies = {}
    
    # Use cookies if available (faster than OAuth)
    if credentials.cookies:
        try:
            cookie_data = credentials.cookies if isinstance(credentials.cookies, dict) else json.loads(credentials.cookies)
            
            # Set up cookies for the request
            if cookie_data.get("GETAFREE_USER_ID"):
                cookies["GETAFREE_USER_ID"] = cookie_data["GETAFREE_USER_ID"]
            if cookie_data.get("GETAFREE_AUTH_HASH_V2"):
                cookies["GETAFREE_AUTH_HASH_V2"] = cookie_data["GETAFREE_AUTH_HASH_V2"]
            if cookie_data.get("XSRF_TOKEN"):
                cookies["XSRF-TOKEN"] = cookie_data["XSRF_TOKEN"]
                headers["X-XSRF-TOKEN"] = cookie_data["XSRF_TOKEN"]
            if cookie_data.get("session2"):
                cookies["session2"] = cookie_data["session2"]
            if cookie_data.get("qfence"):
                cookies["qfence"] = cookie_data["qfence"]
            
            print(f"🍪 Using cookies: {list(cookies.keys())}")
        except Exception as e:
            print(f"⚠️ Error parsing cookies: {e}")
    
    # Fallback to OAuth token if no cookies or as backup
    if credentials.access_token and credentials.access_token != "using_cookies":
        headers["Authorization"] = f"Bearer {credentials.access_token}"
        headers["freelancer-oauth-v1"] = credentials.access_token
        print("🔑 Using OAuth token")
    
    return headers, cookies
