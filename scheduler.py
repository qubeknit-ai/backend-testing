"""
Background scheduler for auto-fetch jobs
Runs independently of frontend, works 24/7
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime
import httpx
import os
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from database import SessionLocal
from models import User
import logging

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Webhook URLs
UPWORK_WEBHOOK_URL = os.getenv('UPWORK_WEBHOOK_URL')
FREELANCER_WEBHOOK_URL = os.getenv('FREELANCER_WEBHOOK_URL')

# Initialize scheduler
scheduler = BackgroundScheduler()


def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        return db
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        return None


async def fetch_upwork_for_user(user_id: int, user_email: str):
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
        if not user.upwork_auto_fetch:
            logger.info(f"Upwork auto-fetch disabled for {user_email}")
            return
        
        # Check daily limit
        if user.upwork_fetches_today >= user.upwork_daily_limit:
            logger.warning(f"Upwork daily limit reached for {user_email}, disabling auto-fetch")
            user.upwork_auto_fetch = False
            db.commit()
            return
        
        # Trigger webhook
        if not UPWORK_WEBHOOK_URL:
            logger.error("UPWORK_WEBHOOK_URL not configured")
            return
        
        logger.info(f"[Auto-Fetch] Triggering Upwork for {user_email} at {datetime.now()}")
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                UPWORK_WEBHOOK_URL,
                json={"user_email": user_email}
            )
            
            if response.status_code == 200:
                # Increment fetch count
                user.upwork_fetches_today += 1
                db.commit()
                logger.info(f"[Auto-Fetch] Upwork success for {user_email}. Remaining: {user.upwork_daily_limit - user.upwork_fetches_today}")
            else:
                logger.error(f"[Auto-Fetch] Upwork failed for {user_email}: {response.status_code}")
    
    except Exception as e:
        logger.error(f"[Auto-Fetch] Upwork error for {user_email}: {e}")
    finally:
        db.close()


async def fetch_freelancer_for_user(user_id: int, user_email: str):
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
        if not user.freelancer_auto_fetch:
            logger.info(f"Freelancer auto-fetch disabled for {user_email}")
            return
        
        # Check daily limit
        if user.freelancer_fetches_today >= user.freelancer_daily_limit:
            logger.warning(f"Freelancer daily limit reached for {user_email}, disabling auto-fetch")
            user.freelancer_auto_fetch = False
            db.commit()
            return
        
        # Trigger webhook
        if not FREELANCER_WEBHOOK_URL:
            logger.error("FREELANCER_WEBHOOK_URL not configured")
            return
        
        logger.info(f"[Auto-Fetch] Triggering Freelancer for {user_email} at {datetime.now()}")
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                FREELANCER_WEBHOOK_URL,
                json={"user_email": user_email}
            )
            
            if response.status_code == 200:
                # Increment fetch count
                user.freelancer_fetches_today += 1
                db.commit()
                logger.info(f"[Auto-Fetch] Freelancer success for {user_email}. Remaining: {user.freelancer_daily_limit - user.freelancer_fetches_today}")
            else:
                logger.error(f"[Auto-Fetch] Freelancer failed for {user_email}: {response.status_code}")
    
    except Exception as e:
        logger.error(f"[Auto-Fetch] Freelancer error for {user_email}: {e}")
    finally:
        db.close()


def check_and_run_auto_fetch():
    """Check all users and run auto-fetch for those who have it enabled"""
    db = get_db()
    if not db:
        return
    
    try:
        # Get all users with auto-fetch enabled
        users_upwork = db.query(User).filter(User.upwork_auto_fetch == True).all()
        users_freelancer = db.query(User).filter(User.freelancer_auto_fetch == True).all()
        
        logger.info(f"[Scheduler] Checking auto-fetch: {len(users_upwork)} Upwork, {len(users_freelancer)} Freelancer")
        
        # Process Upwork users
        for user in users_upwork:
            import asyncio
            asyncio.run(fetch_upwork_for_user(user.id, user.email))
        
        # Process Freelancer users
        for user in users_freelancer:
            import asyncio
            asyncio.run(fetch_freelancer_for_user(user.id, user.email))
    
    except Exception as e:
        logger.error(f"[Scheduler] Error in check_and_run_auto_fetch: {e}")
    finally:
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
