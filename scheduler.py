"""
Background scheduler for auto-fetch jobs
Runs independently of frontend, works 24/7
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timedelta
import httpx
import os
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from database import SessionLocal
from models import User, UserSettings, SystemSettings
import logging
import asyncio

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Webhook URLs
UPWORK_WEBHOOK_URL = os.getenv('UPWORK_WEBHOOK_URL')
FREELANCER_WEBHOOK_URL = os.getenv('FREELANCER_WEBHOOK_URL')

# Initialize scheduler
scheduler = BackgroundScheduler()

# Track last fetch time for each user/platform
last_fetch_times = {}


def get_db():
    """Get database session"""
    try:
        db = SessionLocal()
        return db
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        return None


def check_and_reset_daily_limit(user: User, platform: str, db: Session):
    """Check and reset daily limit if needed"""
    system_settings = db.query(SystemSettings).first()
    if not system_settings:
        system_settings = SystemSettings(
            default_upwork_limit=5,
            default_freelancer_limit=5,
            default_freelancer_plus_limit=3
        )
        db.add(system_settings)
        db.commit()
    
    LIMITS = {
        "upwork": system_settings.default_upwork_limit,
        "freelancer": system_settings.default_freelancer_limit,
    }
    DAILY_LIMIT = LIMITS.get(platform, 5)
    now = datetime.utcnow()
    
    if platform == "upwork":
        count_field = "upwork_fetch_count"
        reset_field = "upwork_last_reset"
    elif platform == "freelancer":
        count_field = "freelancer_fetch_count"
        reset_field = "freelancer_last_reset"
    else:
        return 0, DAILY_LIMIT, False
    
    last_reset = getattr(user, reset_field)
    current_count = getattr(user, count_field) or 0
    
    # Check if it's a new day
    if last_reset:
        if now.date() > last_reset.date():
            # Reset counter
            setattr(user, count_field, 0)
            setattr(user, reset_field, now)
            current_count = 0
            db.commit()
            logger.info(f"[{platform.upper()}] Reset daily limit for user {user.email}")
    else:
        # First time, set reset time
        setattr(user, reset_field, now)
        db.commit()
    
    remaining = DAILY_LIMIT - current_count
    can_fetch = current_count < DAILY_LIMIT
    
    return current_count, DAILY_LIMIT, can_fetch


async def fetch_upwork_for_user(user_id: int, user_email: str, settings: UserSettings):
    """Fetch Upwork jobs for a specific user"""
    db = get_db()
    if not db:
        return
    
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            logger.warning(f"User {user_email} not found")
            return
        
        # Check if auto-fetch is enabled
        if not settings.upwork_auto_fetch:
            return
        
        # Check if enough time has passed since last fetch
        key = f"upwork_{user_id}"
        now = datetime.utcnow()
        interval_minutes = settings.upwork_auto_fetch_interval or 2
        
        if key in last_fetch_times:
            time_since_last = (now - last_fetch_times[key]).total_seconds() / 60
            if time_since_last < interval_minutes:
                return  # Not enough time passed
        
        # Check and reset daily limit
        current_count, daily_limit, can_fetch = check_and_reset_daily_limit(user, "upwork", db)
        
        if not can_fetch:
            logger.warning(f"Upwork daily limit reached for {user_email}, disabling auto-fetch")
            settings.upwork_auto_fetch = False
            db.commit()
            return
        
        # Trigger webhook
        if not UPWORK_WEBHOOK_URL:
            logger.error("UPWORK_WEBHOOK_URL not configured")
            return
        
        logger.info(f"[Auto-Fetch] Triggering Upwork for {user_email} at {now.strftime('%H:%M:%S')}")
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                UPWORK_WEBHOOK_URL,
                json={"user_email": user_email}
            )
            
            if response.status_code == 200:
                # Update last fetch time
                last_fetch_times[key] = now
                
                # Increment fetch count
                user.upwork_fetch_count = (user.upwork_fetch_count or 0) + 1
                db.commit()
                
                remaining = daily_limit - user.upwork_fetch_count
                logger.info(f"[Auto-Fetch] Upwork success for {user_email}. Remaining: {remaining}/{daily_limit}")
                
                # Disable if limit reached
                if remaining <= 0:
                    settings.upwork_auto_fetch = False
                    db.commit()
                    logger.warning(f"[Auto-Fetch] Upwork limit reached for {user_email}, auto-fetch disabled")
            else:
                logger.error(f"[Auto-Fetch] Upwork failed for {user_email}: {response.status_code}")
    
    except Exception as e:
        logger.error(f"[Auto-Fetch] Upwork error for {user_email}: {e}")
    finally:
        if db:
            db.close()


async def fetch_freelancer_for_user(user_id: int, user_email: str, settings: UserSettings):
    """Fetch Freelancer jobs for a specific user"""
    db = get_db()
    if not db:
        return
    
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            logger.warning(f"User {user_email} not found")
            return
        
        # Check if auto-fetch is enabled
        if not settings.freelancer_auto_fetch:
            return
        
        # Check if enough time has passed since last fetch
        key = f"freelancer_{user_id}"
        now = datetime.utcnow()
        interval_minutes = settings.freelancer_auto_fetch_interval or 3
        
        if key in last_fetch_times:
            time_since_last = (now - last_fetch_times[key]).total_seconds() / 60
            if time_since_last < interval_minutes:
                return  # Not enough time passed
        
        # Check and reset daily limit
        current_count, daily_limit, can_fetch = check_and_reset_daily_limit(user, "freelancer", db)
        
        if not can_fetch:
            logger.warning(f"Freelancer daily limit reached for {user_email}, disabling auto-fetch")
            settings.freelancer_auto_fetch = False
            db.commit()
            return
        
        # Trigger webhook
        if not FREELANCER_WEBHOOK_URL:
            logger.error("FREELANCER_WEBHOOK_URL not configured")
            return
        
        logger.info(f"[Auto-Fetch] Triggering Freelancer for {user_email} at {now.strftime('%H:%M:%S')}")
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                FREELANCER_WEBHOOK_URL,
                json={"user_email": user_email}
            )
            
            if response.status_code == 200:
                # Update last fetch time
                last_fetch_times[key] = now
                
                # Increment fetch count
                user.freelancer_fetch_count = (user.freelancer_fetch_count or 0) + 1
                db.commit()
                
                remaining = daily_limit - user.freelancer_fetch_count
                logger.info(f"[Auto-Fetch] Freelancer success for {user_email}. Remaining: {remaining}/{daily_limit}")
                
                # Disable if limit reached
                if remaining <= 0:
                    settings.freelancer_auto_fetch = False
                    db.commit()
                    logger.warning(f"[Auto-Fetch] Freelancer limit reached for {user_email}, auto-fetch disabled")
            else:
                logger.error(f"[Auto-Fetch] Freelancer failed for {user_email}: {response.status_code}")
    
    except Exception as e:
        logger.error(f"[Auto-Fetch] Freelancer error for {user_email}: {e}")
    finally:
        if db:
            db.close()


def check_and_run_auto_fetch():
    """Check all users and run auto-fetch for those who have it enabled"""
    db = get_db()
    if not db:
        return
    
    try:
        # Get all user settings with auto-fetch enabled
        settings_upwork = db.query(UserSettings).filter(UserSettings.upwork_auto_fetch == True).all()
        settings_freelancer = db.query(UserSettings).filter(UserSettings.freelancer_auto_fetch == True).all()
        
        if settings_upwork or settings_freelancer:
            logger.info(f"[Scheduler] Checking auto-fetch: {len(settings_upwork)} Upwork, {len(settings_freelancer)} Freelancer")
        
        # Process Upwork users
        for settings in settings_upwork:
            user = db.query(User).filter(User.id == settings.user_id).first()
            if user:
                asyncio.run(fetch_upwork_for_user(user.id, user.email, settings))
        
        # Process Freelancer users
        for settings in settings_freelancer:
            user = db.query(User).filter(User.id == settings.user_id).first()
            if user:
                asyncio.run(fetch_freelancer_for_user(user.id, user.email, settings))
    
    except Exception as e:
        logger.error(f"[Scheduler] Error in check_and_run_auto_fetch: {e}")
    finally:
        if db:
            db.close()


def start_scheduler():
    """Start the background scheduler"""
    # Run every 1 minute to check for users who need auto-fetch
    scheduler.add_job(
        check_and_run_auto_fetch,
        trigger=IntervalTrigger(minutes=1),
        id='auto_fetch_checker',
        name='Check and run auto-fetch for all users',
        replace_existing=True
    )
    
    scheduler.start()
    logger.info("[Scheduler] Background auto-fetch scheduler started")


def stop_scheduler():
    """Stop the background scheduler"""
    scheduler.shutdown()
    logger.info("[Scheduler] Background auto-fetch scheduler stopped")
