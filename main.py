from fastapi import FastAPI, HTTPException, Depends, status, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func, text, Float
from typing import List, Optional
from datetime import datetime, timedelta
import httpx
import os
from dotenv import load_dotenv
from functools import lru_cache
import asyncio
from concurrent.futures import ThreadPoolExecutor
from cache_utils import cached, cleanup_cache
import threading
import time
from schemas import UserSignup, UserLogin, Token, UserResponse, SettingsUpdate, SettingsResponse, UserProfileUpdate, TalentCreate, TalentUpdate, TalentResponse, FreelancerCredentialsCreate, FreelancerCredentialsResponse, FreelancerCredentialsUpdate
from auth import get_password_hash, verify_password, create_access_token, verify_token, SECRET_KEY, ALGORITHM
import json
from urllib.parse import unquote

load_dotenv()

app = FastAPI()

# Start cache cleanup task
def start_cache_cleanup():
    """Start periodic cache cleanup"""
    def cleanup_task():
        while True:
            try:
                cleanup_cache()
                time.sleep(300)  # Clean up every 5 minutes
            except Exception as e:
                print(f"Cache cleanup error: {e}")
                time.sleep(60)  # Wait 1 minute on error
    
    cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()

# Start cleanup on app startup
start_cache_cleanup()



# Lazy import database to avoid connection on startup
def init_db():
    try:
        from database import engine, Base
        Base.metadata.create_all(bind=engine)
        return True
    except Exception as e:
        print(f"Database initialization failed: {e}")
        return False

def get_db():
    """Database session optimized for Supabase transaction pooler"""
    db = None
    try:
        from database import SessionLocal
        db = SessionLocal()
        
        # Optimize session for transaction pooler
        db.execute(text("SET statement_timeout = '30s'"))
        
        yield db
    except Exception as e:
        print(f"Database connection failed: {e}")
        if db:
            try:
                db.rollback()
            except:
                pass  # Ignore rollback errors in transaction pooler
        raise HTTPException(status_code=500, detail="Database connection failed")
    finally:
        if db is not None:
            try:
                db.close()
            except:
                pass  # Ignore close errors in transaction pooler

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add performance middleware
from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Add response time header middleware
@app.middleware("http")
async def add_process_time_header(request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response

@app.post("/api/leads/bulk")
async def receive_leads_from_n8n(payload: dict, db: Session = Depends(get_db)):
    """
    Endpoint for N8N to send scraped leads back to backend
    Expected payload: {
        "user_id": 1,
        "leads": [
            {
                "platform": "Upwork",
                "title": "Job Title",
                "budget": "$500",
                "posted": "2 hours ago",
                "status": "Pending",
                "score": "8",
                "description": "Job description",
                "url": "https://..."
            },
            ...
        ]
    }
    """
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import Lead, Notification
        from dateutil import parser
        
        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        
        leads_data = payload.get("leads", [])
        if not leads_data:
            return {"success": True, "message": "No leads to save", "count": 0}
        
        saved_count = 0
        
        for lead_data in leads_data:
            # Skip if lead doesn't have essential data
            title = lead_data.get("title", lead_data.get("titlle"))
            platform = lead_data.get("platform")
            
            if not title or not platform:
                print(f"Skipping lead with missing title or platform: {lead_data}")
                continue
            
            # Check if lead already exists by title and platform for this user
            existing_lead = db.query(Lead).filter(
                Lead.user_id == user_id,
                Lead.title == title,
                Lead.platform == platform
            ).first()
            
            if not existing_lead:
                # Parse posted_time if available
                posted_time = None
                if lead_data.get("posted_time"):
                    try:
                        posted_time = parser.parse(lead_data.get("posted_time"))
                    except:
                        posted_time = None
                
                new_lead = Lead(
                    user_id=user_id,
                    platform=platform,
                    title=title,
                    budget=str(lead_data.get("budget", "")),
                    posted=lead_data.get("posted", ""),
                    posted_time=posted_time,
                    status=lead_data.get("status", "Pending"),
                    score=str(lead_data.get("Score", lead_data.get("score", "—"))),
                    description=lead_data.get("description", ""),
                    proposal=lead_data.get("Proposal", ""),
                    url=lead_data.get("url", "")
                )
                db.add(new_lead)
                saved_count += 1
        
        db.commit()
        
        # Create notification for user
        if saved_count > 0:
            notification = Notification(
                user_id=user_id,
                type="success",
                title="Jobs Fetched Successfully",
                message=f"Successfully fetched {saved_count} new jobs from {leads_data[0].get('platform', 'platform')}"
            )
            db.add(notification)
            db.commit()
        
        print(f"✅ Saved {saved_count} leads for user_id={user_id}")
        return {"success": True, "message": f"Saved {saved_count} leads", "count": saved_count}
        
    except Exception as e:
        print(f"Error saving leads from N8N: {str(e)}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.post("/api/sync-send")
async def sync_send(payload: dict, db: Session = Depends(get_db)):
    try:
        # Prepare headers with authentication
        headers = {
            "Content-Type": "application/json"
        }
        
        # Add API key if configured
        api_key = os.getenv("N8N_WEBHOOK_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key
            # Alternative: headers["Authorization"] = f"Bearer {api_key}"
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                os.getenv("N8N_SEND_WEBHOOK_URL"),
                json=payload,
                headers=headers
            )
            return response.json()
    except Exception as e:
        print(f"Error in sync_send: {str(e)}")
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.get("/api/sync-receive")
async def sync_receive(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    """
    Legacy endpoint - now requires authentication
    Fetches leads and saves them for the current user
    """
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        user = get_user_by_email(email, db)
        
        print(f"Fetching from: {os.getenv('N8N_RECEIVE_WEBHOOK_URL')} for user {user.email}")
        
        # Prepare headers with authentication
        headers = {"Content-Type": "application/json"}
        
        # Add API key if configured
        api_key = os.getenv("N8N_WEBHOOK_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Changed to POST to match the webhook configuration
            response = await client.post(
                os.getenv("N8N_RECEIVE_WEBHOOK_URL"),
                headers=headers
            )
            print(f"Response status: {response.status_code}")
            print(f"Response content: {response.text[:200]}")
            
            if response.status_code != 200:
                print(f"N8N returned error: {response.text}")
                raise HTTPException(status_code=response.status_code, detail=f"N8N webhook failed: {response.text}")
            
            # Check if response has content
            if not response.text or response.text.strip() == "":
                print("Empty response from N8N, returning empty list")
                return []
            
            try:
                leads_data = response.json()
            except Exception as json_error:
                print(f"JSON parse error: {json_error}, response text: {response.text}")
                return []
            
            print(f"Received {len(leads_data) if isinstance(leads_data, list) else 'unknown'} leads")
            
            # Save leads to database for current user
            try:
                from models import Lead
                for lead_data in leads_data:
                    # Skip if lead doesn't have essential data
                    title = lead_data.get("title", lead_data.get("titlle"))
                    platform = lead_data.get("platform")
                    
                    if not title or not platform:
                        print(f"Skipping lead with missing title or platform: {lead_data}")
                        continue
                    
                    # Check if lead already exists by title and platform for this user
                    existing_lead = db.query(Lead).filter(
                        Lead.user_id == user.id,
                        Lead.title == title,
                        Lead.platform == platform
                    ).first()
                    
                    if not existing_lead:
                        # Parse posted_time if available
                        posted_time = None
                        if lead_data.get("posted_time"):
                            try:
                                from dateutil import parser
                                posted_time = parser.parse(lead_data.get("posted_time"))
                            except:
                                posted_time = None
                        
                        new_lead = Lead(
                            user_id=user.id,
                            platform=platform,
                            title=title,
                            budget=str(lead_data.get("budget", "")),
                            posted=lead_data.get("posted", ""),
                            posted_time=posted_time,
                            status=lead_data.get("status", "Pending"),
                            score=str(lead_data.get("Score", lead_data.get("score", "—"))),
                            description=lead_data.get("description", ""),
                            proposal=lead_data.get("Proposal", ""),
                            url=lead_data.get("url", "")
                        )
                        db.add(new_lead)
                
                db.commit()
            except Exception as db_error:
                print(f"Database save failed: {db_error}")
                if db:
                    db.rollback()
            
            return leads_data
    except httpx.TimeoutException as e:
        print(f"Timeout error: {str(e)}")
        raise HTTPException(status_code=504, detail="Load On server Plz try again Later")
    except Exception as e:
        print(f"Error in sync_receive: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

async def trigger_webhook_async(webhook_url: str, payload: dict, headers: dict):
    """Async webhook trigger with shorter timeout and better error handling"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:  # Reduced from 600s to 30s
            response = await client.post(webhook_url, json=payload, headers=headers)
            return {
                "success": response.status_code == 200,
                "status_code": response.status_code,
                "response_text": response.text[:500] if response.text else 'empty'
            }
    except httpx.TimeoutException:
        return {"success": False, "error": "Request timeout", "status_code": 504}
    except Exception as e:
        return {"success": False, "error": str(e), "status_code": 500}

@app.post("/api/fetch-upwork")
async def fetch_upwork(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import UserSettings
        user = get_user_by_email(email, db)
        
        # Check and reset daily limit if needed
        current_count, daily_limit, can_fetch, remaining = check_and_reset_daily_limit(user, "upwork", db)
        
        if not can_fetch:
            raise HTTPException(
                status_code=429,
                detail=f"Daily limit reached. You can fetch Upwork jobs {daily_limit} times per day. Limit resets at midnight UTC."
            )
        
        # Get user's settings
        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
        if not settings:
            raise HTTPException(status_code=400, detail="Load On server Plz try again Later")
        
        webhook_url = os.getenv("UPWORK_WEBHOOK_URL")
        if not webhook_url:
            raise HTTPException(status_code=500, detail="UPWORK_WEBHOOK_URL not configured in environment")
        
        # Increment fetch count immediately to prevent double-fetching
        user.upwork_fetch_count += 1
        db.commit()
        
        # Prepare payload with user context
        payload = {
            "user_id": user.id,
            "user_email": user.email,
            "settings": {
                "job_categories": settings.upwork_job_categories,
                "max_jobs": settings.upwork_max_jobs,
                "payment_verified": settings.upwork_payment_verified
            }
        }
        
        # Prepare headers with authentication
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv("N8N_WEBHOOK_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key
        
        print(f"Triggering Upwork webhook for user {user.email}")
        
        # Trigger webhook asynchronously with timeout
        webhook_result = await trigger_webhook_async(webhook_url, payload, headers)
        
        remaining = daily_limit - user.upwork_fetch_count
        
        if not webhook_result["success"]:
            # If webhook failed, we still return success since we've triggered the process
            print(f"Webhook failed but fetch initiated: {webhook_result.get('error', 'Unknown error')}")
        
        return {
            "success": True,
            "message": "Upwork jobs fetch initiated successfully",
            "status": webhook_result.get("status_code", 200),
            "fetch_count": user.upwork_fetch_count,
            "daily_limit": daily_limit,
            "remaining": remaining
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error triggering Upwork webhook: {str(e)}")
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.post("/api/fetch-freelancer")
async def fetch_freelancer(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import UserSettings
        user = get_user_by_email(email, db)
        
        # Check and reset daily limit if needed
        current_count, daily_limit, can_fetch, remaining = check_and_reset_daily_limit(user, "freelancer", db)
        
        if not can_fetch:
            raise HTTPException(
                status_code=429,
                detail=f"Daily limit reached. You can fetch Freelancer jobs {daily_limit} times per day. Limit resets at midnight UTC."
            )
        
        # Get user's settings
        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
        if not settings:
            raise HTTPException(status_code=400, detail="Load On server Plz try again Later")
        
        webhook_url = os.getenv("FREELANCER_WEBHOOK_URL")
        print(f"Triggering Freelancer webhook for user {user.email}: {webhook_url}")
        
        if not webhook_url:
            raise HTTPException(status_code=500, detail="FREELANCER_WEBHOOK_URL not configured in environment")
        
        # Prepare payload with user context
        payload = {
            "user_id": user.id,
            "user_email": user.email,
            "settings": {
                "job_category": settings.freelancer_job_category,
                "max_jobs": settings.freelancer_max_jobs
            }
        }
        
        # Prepare headers with authentication
        headers = {"Content-Type": "application/json"}
        
        # Add API key if configured
        api_key = os.getenv("N8N_WEBHOOK_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key
            print(f"Using API key authentication: {api_key[:10]}...")
        else:
            print("WARNING: N8N_WEBHOOK_API_KEY not set - webhook may fail authentication")
        
        async with httpx.AsyncClient(timeout=600.0) as client:
            response = await client.post(
                webhook_url,
                json=payload,
                headers=headers
            )
            print(f"Freelancer webhook response status: {response.status_code}")
            print(f"Freelancer webhook response: {response.text[:500] if response.text else 'empty'}")
            
            # Check if response contains N8N workflow error
            if response.status_code != 200:
                error_detail = "Unable to fetch jobs. Please try again later."
                try:
                    error_json = response.json()
                    if "Unused Respond to Webhook" in str(error_json):
                        error_detail = "Workflow configuration error. Please contact support."
                    elif "not registered" in str(error_json).lower():
                        error_detail = "N8N Webhook Not Found: The webhook at 'https://n8n.srv1128153.hstgr.cloud/webhook/upwork001' is not registered or the workflow is not active. Please check: 1) Workflow is activated (toggle ON), 2) Webhook path matches, 3) Workflow is saved"
                    elif error_json.get("message"):
                        # Don't expose internal error messages to users
                        error_detail = "Service temporarily unavailable. Please try again."
                except:
                    pass
                raise HTTPException(status_code=response.status_code, detail=error_detail)
            
            # Increment fetch count after successful fetch
            user.freelancer_fetch_count += 1
            db.commit()
            
            remaining = daily_limit - user.freelancer_fetch_count
            print(f"User {user.email} fetched Freelancer. Count: {user.freelancer_fetch_count}/{daily_limit}, Remaining: {remaining}")
            
            return {
                "success": True,
                "message": "Freelancer jobs fetch triggered successfully",
                "status": response.status_code,
                "fetch_count": user.freelancer_fetch_count,
                "daily_limit": daily_limit,
                "remaining": remaining
            }
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Load On server Plz try again Later")
    except Exception as e:
        print(f"Error triggering Freelancer webhook: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.post("/api/fetch-freelancer-plus")
async def fetch_freelancer_plus(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import UserSettings
        user = get_user_by_email(email, db)
        
        # Check and reset daily limit if needed
        current_count, daily_limit, can_fetch, remaining = check_and_reset_daily_limit(user, "freelancer_plus", db)
        
        if not can_fetch:
            raise HTTPException(
                status_code=429,
                detail=f"Daily limit reached. You can fetch Freelancer Plus jobs {daily_limit} times per day. Limit resets at midnight UTC."
            )
        
        # Get user's settings
        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
        if not settings:
            raise HTTPException(status_code=400, detail="Load On server Plz try again Later")
        
        webhook_url = os.getenv("FREELANCER_PLUS_WEBHOOK_URL")
        print(f"Triggering Freelancer Plus webhook for user {user.email}: {webhook_url}")
        
        if not webhook_url:
            raise HTTPException(status_code=500, detail="FREELANCER_PLUS_WEBHOOK_URL not configured in environment")
        
        # Prepare payload with user context
        payload = {
            "user_id": user.id,
            "user_email": user.email,
            "settings": {
                "job_category": settings.freelancer_job_category,
                "max_jobs": settings.freelancer_max_jobs
            }
        }
        
        # Prepare headers with authentication
        headers = {"Content-Type": "application/json"}
        
        # Add API key if configured
        api_key = os.getenv("N8N_WEBHOOK_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key
            print(f"Using API key authentication: {api_key[:10]}...")
        else:
            print("WARNING: N8N_WEBHOOK_API_KEY not set - webhook may fail authentication")
        
        async with httpx.AsyncClient(timeout=600.0) as client:
            response = await client.post(
                webhook_url,
                json=payload,
                headers=headers
            )
            print(f"Freelancer Plus webhook response status: {response.status_code}")
            print(f"Freelancer Plus webhook response: {response.text[:500] if response.text else 'empty'}")
            
            # Check if response contains N8N workflow error
            if response.status_code != 200:
                error_detail = "Unable to fetch jobs. Please try again later."
                try:
                    error_json = response.json()
                    if "Unused Respond to Webhook" in str(error_json):
                        error_detail = "Workflow configuration error. Please contact support."
                    elif "not registered" in str(error_json).lower():
                        error_detail = "N8N Webhook Not Found: The webhook at 'https://n8n.srv1128153.hstgr.cloud/webhook/upwork001' is not registered or the workflow is not active. Please check: 1) Workflow is activated (toggle ON), 2) Webhook path matches, 3) Workflow is saved"
                    elif error_json.get("message"):
                        # Don't expose internal error messages to users
                        error_detail = "Service temporarily unavailable. Please try again."
                except:
                    pass
                raise HTTPException(status_code=response.status_code, detail=error_detail)
            
            # Increment fetch count after successful fetch
            user.freelancer_plus_fetch_count += 1
            db.commit()
            
            remaining = daily_limit - user.freelancer_plus_fetch_count
            print(f"User {user.email} fetched Freelancer Plus. Count: {user.freelancer_plus_fetch_count}/{daily_limit}, Remaining: {remaining}")
            
            return {
                "success": True, 
                "message": "Freelancer Plus jobs fetch triggered successfully", 
                "status": response.status_code,
                "fetch_count": user.freelancer_plus_fetch_count,
                "daily_limit": daily_limit,
                "remaining": remaining
            }
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Load On server Plz try again Later")
    except Exception as e:
        print(f"Error triggering Freelancer Plus webhook: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.get("/api/fetch-limits")
async def get_fetch_limits(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    """Get remaining fetches for all platforms"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        user = get_user_by_email(email, db)
        
        # Check and reset for each platform
        upwork_count, upwork_limit, upwork_can_fetch, upwork_remaining = check_and_reset_daily_limit(user, "upwork", db)
        freelancer_count, freelancer_limit, freelancer_can_fetch, freelancer_remaining = check_and_reset_daily_limit(user, "freelancer", db)
        freelancer_plus_count, freelancer_plus_limit, freelancer_plus_can_fetch, freelancer_plus_remaining = check_and_reset_daily_limit(user, "freelancer_plus", db)
        
        return {
            "upwork": {
                "fetch_count": upwork_count,
                "daily_limit": upwork_limit,
                "remaining": upwork_remaining,
                "can_fetch": upwork_can_fetch
            },
            "freelancer": {
                "fetch_count": freelancer_count,
                "daily_limit": freelancer_limit,
                "remaining": freelancer_remaining,
                "can_fetch": freelancer_can_fetch
            },
            "freelancer_plus": {
                "fetch_count": freelancer_plus_count,
                "daily_limit": freelancer_plus_limit,
                "remaining": freelancer_plus_remaining,
                "can_fetch": freelancer_plus_can_fetch
            }
        }
    except Exception as e:
        print(f"Error checking limits: {e}")
        return {
            "upwork": {"fetch_count": 0, "daily_limit": 5, "remaining": 5, "can_fetch": True},
            "freelancer": {"fetch_count": 0, "daily_limit": 5, "remaining": 5, "can_fetch": True},
            "freelancer_plus": {"fetch_count": 0, "daily_limit": 5, "remaining": 5, "can_fetch": True}
        }



@app.get("/api/leads")
async def get_leads(
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    platform: Optional[str] = Query(None, description="Filter by platform"),
    status: Optional[str] = Query(None, description="Filter by status"),
    email: str = Depends(verify_token), 
    db: Session = Depends(get_db)
):
    try:
        if db is None:
            return {"leads": [], "total": 0, "page": page, "limit": limit}
        
        from models import Lead
        user = get_user_by_email(email, db)
        
        # Build query with filters
        query = db.query(Lead).filter(Lead.user_id == user.id, Lead.visible == True)
        
        if platform:
            query = query.filter(Lead.platform == platform)
        if status:
            query = query.filter(Lead.status == status)
        
        # Get total count for pagination
        total = query.count()
        
        # Apply pagination and ordering
        offset = (page - 1) * limit
        leads = query.order_by(Lead.updated_at.desc()).offset(offset).limit(limit).all()
        
        return {
            "leads": [
                {
                    "id": lead.id,
                    "platform": lead.platform,
                    "title": lead.title,
                    "budget": lead.budget,
                    "bids": lead.bids if hasattr(lead, 'bids') else 0,
                    "cost": lead.cost if hasattr(lead, 'cost') else 0,
                    "posted": lead.posted,
                    "posted_time": lead.posted_time.isoformat() if lead.posted_time else None,
                    "status": lead.status,
                    "score": lead.score,
                    "description": lead.description,
                    "Proposal": lead.proposal,
                    "url": lead.url,
                    "avg_bid_price": lead.avg_bid_price if hasattr(lead, 'avg_bid_price') else None,
                    "created_at": lead.created_at.isoformat() if lead.created_at else None,
                    "updated_at": lead.updated_at.isoformat() if lead.updated_at else None
                }
                for lead in leads
            ],
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit
        }
    except Exception as e:
        print(f"❌ Error fetching leads: {e}")
        import traceback
        traceback.print_exc()
        return {"leads": [], "total": 0, "page": page, "limit": limit}

@app.get("/api/dashboard/pipeline")
async def get_pipeline_stats(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    """
    Get pipeline breakdown with lead counts and values per stage
    """
    try:
        if db is None:
            return {"pipeline": []}
        
        from models import Lead
        user = get_user_by_email(email, db)
        
        # Define pipeline stages in order
        stages = ["New", "Proposal Sent", "Approved", "Closed"]
        
        # Map database status values to pipeline stages
        status_mapping = {
            "Pending": "New",
            "AI Drafted": "Proposal Sent",
            "Approved": "Approved",
            "Closed": "Closed",
            "Sent": "Proposal Sent",
            "Won": "Closed",
            "Lost": "Closed"
        }
        
        # Get only this user's leads
        all_leads = db.query(Lead).filter(Lead.user_id == user.id).all()
        
        # Initialize pipeline data
        pipeline_data = {stage: {"count": 0, "value": 0} for stage in stages}
        
        # Process each lead
        for lead in all_leads:
            # Map status to pipeline stage
            db_status = lead.status or "Pending"
            stage = status_mapping.get(db_status, "New")
            
            # Increment count
            pipeline_data[stage]["count"] += 1
            
            # Extract and add budget value
            if lead.budget:
                try:
                    # Extract numeric value from budget string
                    budget_str = str(lead.budget).replace('$', '').replace(',', '').strip()
                    # Handle ranges like "$500-$1000" - take average
                    if '-' in budget_str:
                        parts = budget_str.split('-')
                        low = float(''.join(c for c in parts[0] if c.isdigit() or c == '.'))
                        high = float(''.join(c for c in parts[1] if c.isdigit() or c == '.'))
                        value = (low + high) / 2
                    else:
                        # Extract first number found
                        value = float(''.join(c for c in budget_str if c.isdigit() or c == '.'))
                    pipeline_data[stage]["value"] += int(value)
                except (ValueError, AttributeError):
                    pass
        
        # Convert to list format
        pipeline = [
            {
                "stage": stage,
                "count": pipeline_data[stage]["count"],
                "value": pipeline_data[stage]["value"]
            }
            for stage in stages
        ]
        
        return {"pipeline": pipeline}
    except Exception as e:
        print(f"Error fetching pipeline stats: {e}")
        import traceback
        traceback.print_exc()
        return {"pipeline": []}

@lru_cache(maxsize=100)
def get_dashboard_stats_cached(user_id: int, cache_key: str):
    """Cached dashboard stats - cache_key includes timestamp for cache invalidation"""
    from database import SessionLocal
    from models import Lead
    
    db = SessionLocal()
    try:
        # Use optimized queries with database aggregation
        base_query = db.query(Lead).filter(Lead.user_id == user_id, Lead.visible == True)
        
        # Get counts using database aggregation
        total_leads = base_query.count()
        ai_drafted = base_query.filter(Lead.status == "AI Drafted").count()
        approved = base_query.filter(Lead.proposal_accepted == True).count()
        
        # Get low score count with database query
        low_score = base_query.filter(
            Lead.score.notin_(['—', '', None]),
            func.cast(Lead.score, Float) < 7
        ).count()
        
        # Platform distribution using database aggregation
        platform_stats = db.query(
            Lead.platform,
            func.count(Lead.id).label('count')
        ).filter(
            Lead.user_id == user_id,
            Lead.visible == True
        ).group_by(Lead.platform).all()
        
        total_with_platform = sum(stat.count for stat in platform_stats)
        platform_distribution = [
            {
                "name": stat.platform or "Unknown",
                "value": round((stat.count / total_with_platform * 100), 1) if total_with_platform > 0 else 0,
                "count": stat.count
            }
            for stat in platform_stats
        ]
        
        # Timeline data (last 30 days) using database aggregation
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        timeline_stats = db.query(
            func.date(Lead.created_at).label('date'),
            func.count(Lead.id).label('total'),
            func.sum(func.case([(Lead.status.in_(["AI Drafted", "Approved"]), 1)], else_=0)).label('proposals')
        ).filter(
            Lead.user_id == user_id,
            Lead.visible == True,
            Lead.created_at >= thirty_days_ago
        ).group_by(func.date(Lead.created_at)).order_by(func.date(Lead.created_at)).all()
        
        timeline_data = [
            {
                "date": stat.date.strftime("%b %d") if stat.date else "Unknown",
                "total": int(stat.total),
                "proposals": int(stat.proposals or 0)
            }
            for stat in timeline_stats
        ]
        
        if not timeline_data:
            timeline_data = [{"date": datetime.utcnow().strftime("%b %d"), "total": 0, "proposals": 0}]
        
        return {
            "total_leads": total_leads,
            "ai_drafted": ai_drafted,
            "low_score": low_score,
            "approved": approved,
            "platform_distribution": platform_distribution,
            "timeline_data": timeline_data
        }
    finally:
        db.close()

@app.get("/api/dashboard/stats")
async def get_dashboard_stats(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    try:
        if db is None:
            return {
                "total_leads": 0,
                "ai_drafted": 0,
                "low_score": 0,
                "approved": 0,
                "platform_distribution": [],
                "timeline_data": []
            }
        
        user = get_user_by_email(email, db)
        
        # Create cache key with 5-minute granularity for cache invalidation
        cache_timestamp = int(datetime.utcnow().timestamp() // 300)  # 5-minute buckets
        cache_key = f"{user.id}_{cache_timestamp}"
        
        # Use cached function
        return get_dashboard_stats_cached(user.id, cache_key)
        
    except Exception as e:
        print(f"Error fetching dashboard stats: {e}")
        import traceback
        traceback.print_exc()
        return {
            "total_leads": 0,
            "ai_drafted": 0,
            "low_score": 0,
            "approved": 0,
            "platform_distribution": [],
            "timeline_data": []
        }

@app.put("/api/leads/{lead_id}/proposal")
async def update_lead_proposal(
    lead_id: int,
    proposal_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import Lead
        user = get_user_by_email(email, db)
        
        # Find the lead and verify ownership
        lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found or access denied")
        
        # Debug: Print received data
        print(f"Received proposal_data: {proposal_data}")
        print(f"Status in proposal_data: {'status' in proposal_data}")
        print(f"Status value: {proposal_data.get('status')}")
        
        # Update the proposal
        lead.proposal = proposal_data.get("proposal", "")
        
        # Update status if provided
        if "status" in proposal_data:
            print(f"Updating status from {lead.status} to {proposal_data.get('status')}")
            lead.status = proposal_data.get("status")
        
        lead.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(lead)
        
        print(f"Updated proposal for lead {lead_id}, status: {lead.status}")
        return {
            "success": True,
            "message": "Proposal updated successfully",
            "lead": {
                "id": lead.id,
                "platform": lead.platform,
                "title": lead.title,
                "budget": lead.budget,
                "posted": lead.posted,
                "posted_time": lead.posted_time.isoformat() if lead.posted_time else None,
                "status": lead.status,
                "score": lead.score,
                "description": lead.description,
                "Proposal": lead.proposal,
                "url": lead.url,
                "updated_at": lead.updated_at.isoformat() if lead.updated_at else None
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error updating proposal: {e}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.put("/api/leads/{lead_id}/approve")
async def approve_lead(
    lead_id: int,
    email: str = Depends(verify_token)
):
    """Approve a lead - simplified version that works like the extension"""
    try:
        print(f"Approving lead {lead_id} for user: {email}")
        
        # Try to update in database if available, but don't fail if DB is down
        lead_data = None
        try:
            from database import SessionLocal
            from models import Lead, User
            
            db = SessionLocal()
            try:
                user = db.query(User).filter(User.email == email).first()
                if user:
                    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
                    if lead:
                        # Update the status to Approved
                        lead.status = "Approved"
                        lead.updated_at = datetime.utcnow()
                        
                        db.commit()
                        db.refresh(lead)
                        
                        lead_data = {
                            "id": lead.id,
                            "platform": lead.platform,
                            "title": lead.title,
                            "budget": lead.budget,
                            "posted": lead.posted,
                            "posted_time": lead.posted_time.isoformat() if lead.posted_time else None,
                            "status": lead.status,
                            "score": lead.score,
                            "description": lead.description,
                            "Proposal": lead.proposal,
                            "url": lead.url,
                            "updated_at": lead.updated_at.isoformat() if lead.updated_at else None
                        }
                        print(f"✅ Successfully updated lead {lead_id} in database")
            finally:
                db.close()
        except Exception as db_error:
            print(f"⚠️ Database update failed, but continuing: {db_error}")
            # Continue without database - create a mock response
            lead_data = {
                "id": lead_id,
                "platform": "Unknown",
                "title": "Lead approved without database",
                "budget": "",
                "posted": "",
                "posted_time": None,
                "status": "Approved",
                "score": "",
                "description": "",
                "Proposal": "",
                "url": "",
                "updated_at": datetime.utcnow().isoformat()
            }
        
        print(f"✅ Approved lead {lead_id}")
        return {
            "success": True,
            "message": "Lead approved successfully",
            "lead": lead_data
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error approving lead: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/api/chat")
async def send_chat_message(
    chat_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """
    Send chat message to N8N webhook and save to database
    Expected payload: {
        "message": "user message",
        "lead_id": 123,
        "proposal": "proposal text",
        "description": "job description"
    }
    """
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import Lead, ChatMessage
        user = get_user_by_email(email, db)
        
        # Get lead_id from chat_data
        lead_id = chat_data.get("lead_id")
        if not lead_id:
            raise HTTPException(status_code=400, detail="lead_id is required")
        
        # Verify lead ownership
        lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found or access denied")
        
        user_message = chat_data.get("message", "")
        
        # Save user message to database
        user_chat_message = ChatMessage(
            user_id=user.id,
            lead_id=lead_id,
            message=user_message,
            sender="user"
        )
        db.add(user_chat_message)
        db.commit()
        
        webhook_url = os.getenv("CHAT_WEBHOOK_URL")
        if not webhook_url:
            raise HTTPException(status_code=500, detail="CHAT_WEBHOOK_URL not configured")
        
        # Prepare payload for N8N
        payload = {
            "user_id": user.id,
            "user_email": user.email,
            "lead_id": lead_id,
            "message": user_message,
            "proposal": chat_data.get("proposal", lead.proposal),
            "description": chat_data.get("description", lead.description),
            "title": lead.title,
            "platform": lead.platform
        }
        
        # Prepare headers with authentication
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv("N8N_WEBHOOK_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key
        
        print(f"Sending chat message to N8N for lead {lead_id}")
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                webhook_url,
                json=payload,
                headers=headers
            )
            
            print(f"Chat webhook response status: {response.status_code}")
            print(f"Chat webhook response: {response.text}")
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to send message to AI assistant"
                )
            
            # Parse N8N response format: [{"output": "..."}]
            try:
                ai_response = response.json()
                output_text = ""
                
                # Handle array response from N8N
                if isinstance(ai_response, list) and len(ai_response) > 0:
                    output_text = ai_response[0].get("output", "")
                # Handle direct object response
                elif isinstance(ai_response, dict):
                    output_text = ai_response.get("output", ai_response.get("response", "Response received"))
                else:
                    output_text = str(ai_response)
                
                # Save AI response to database
                ai_chat_message = ChatMessage(
                    user_id=user.id,
                    lead_id=lead_id,
                    message=output_text,
                    sender="ai"
                )
                db.add(ai_chat_message)
                db.commit()
                
                return {
                    "success": True,
                    "response": output_text
                }
            except Exception as e:
                print(f"Error parsing AI response: {e}")
                return {
                    "success": True,
                    "response": response.text
                }
                
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error sending chat message: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to send message")

@app.get("/api/chat/history/{lead_id}")
async def get_chat_history(
    lead_id: int,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get chat history for a specific lead"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import Lead, ChatMessage
        user = get_user_by_email(email, db)
        
        # Verify lead ownership
        lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found or access denied")
        
        # Get chat messages for this lead
        messages = db.query(ChatMessage).filter(
            ChatMessage.lead_id == lead_id,
            ChatMessage.user_id == user.id
        ).order_by(ChatMessage.created_at.asc()).all()
        
        return {
            "success": True,
            "messages": [
                {
                    "id": msg.id,
                    "text": msg.message,
                    "sender": msg.sender,
                    "timestamp": msg.created_at.isoformat()
                }
                for msg in messages
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error retrieving chat history: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to retrieve chat history")

@app.delete("/api/leads/clean")
async def clean_leads(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import Lead
        user = get_user_by_email(email, db)
        
        # Log before hiding
        total_leads_before = db.query(Lead).count()
        user_leads_before = db.query(Lead).filter(Lead.user_id == user.id, Lead.visible == True).count()
        print(f"[CLEAN LEADS] User: {user.email} (ID: {user.id})")
        print(f"[CLEAN LEADS] Total leads in DB: {total_leads_before}")
        print(f"[CLEAN LEADS] User's visible leads: {user_leads_before}")
        
        # Hide only this user's visible leads by setting visible=False
        hidden_count = db.query(Lead).filter(Lead.user_id == user.id, Lead.visible == True).update({"visible": False})
        db.commit()
        
        # Log after hiding
        user_visible_after = db.query(Lead).filter(Lead.user_id == user.id, Lead.visible == True).count()
        print(f"[CLEAN LEADS] Hidden: {hidden_count} leads")
        print(f"[CLEAN LEADS] User's visible leads remaining: {user_visible_after}")
        
        return {"success": True, "message": f"Hidden {hidden_count} leads for {user.email}", "count": hidden_count}
    except Exception as e:
        print(f"Error cleaning leads: {e}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.post("/api/auth/signup", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def signup(user_data: UserSignup, db: Session = Depends(get_db)):
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    from models import User
    
    # Check if user already exists (case-insensitive)
    existing_user = db.query(User).filter(func.lower(User.email) == user_data.email.lower()).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # Create new user (store email in lowercase for consistency)
    hashed_password = get_password_hash(user_data.password)
    new_user = User(email=user_data.email.lower(), hashed_password=hashed_password)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    return new_user

@app.post("/api/auth/login", response_model=Token)
async def login(user_data: UserLogin, db: Session = Depends(get_db)):
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    from models import User
    
    # Find user (case-insensitive email search)
    user = db.query(User).filter(func.lower(User.email) == user_data.email.lower()).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )
    
    # Verify password
    if not verify_password(user_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )
    
    # Create access token
    access_token = create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/api/auth/debug")
async def debug_auth(request: Request):
    """Debug endpoint to check authentication headers and token format"""
    try:
        auth_header = request.headers.get("authorization")
        print(f"🔍 [DEBUG_AUTH] Authorization header: {auth_header}")
        
        if not auth_header:
            return {
                "error": "No authorization header found",
                "headers": dict(request.headers)
            }
        
        if not auth_header.startswith("Bearer "):
            return {
                "error": "Invalid authorization header format",
                "auth_header": auth_header,
                "expected_format": "Bearer <token>"
            }
        
        token = auth_header.split(" ")[1]
        print(f"🔍 [DEBUG_AUTH] Extracted token: {token[:30]}...")
        
        # Try to decode without verification first
        try:
            import jwt
            unverified_payload = jwt.decode(token, options={"verify_signature": False})
            print(f"🔍 [DEBUG_AUTH] Unverified payload: {unverified_payload}")
            
            return {
                "success": True,
                "token_format": "valid_jwt",
                "token_length": len(token),
                "token_parts": len(token.split('.')),
                "unverified_payload": unverified_payload,
                "secret_key_prefix": SECRET_KEY[:10],
                "algorithm": ALGORITHM
            }
        except Exception as decode_error:
            print(f"🔍 [DEBUG_AUTH] Decode error: {decode_error}")
            return {
                "error": "Could not decode token",
                "token_length": len(token),
                "token_parts": len(token.split('.')),
                "decode_error": str(decode_error),
                "secret_key_prefix": SECRET_KEY[:10],
                "algorithm": ALGORITHM
            }
            
    except Exception as e:
        print(f"🔍 [DEBUG_AUTH] General error: {e}")
        return {
            "error": "Debug failed",
            "exception": str(e)
        }

@app.get("/api/auth/me", response_model=UserResponse)
async def get_current_user(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    from models import User
    
    # Find user (case-insensitive)
    user = db.query(User).filter(func.lower(User.email) == email.lower()).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return user

@app.get("/")
async def root():
    """Fast root endpoint with minimal processing"""
    return {"status": "ok", "service": "akbpo-api", "version": "1.0"}


@cached(ttl=30, key_prefix="health_")  # Cache for 30 seconds
def _check_db_status():
    """Cached database status check optimized for transaction pooler"""
    try:
        from db_utils import quick_db_check
        if quick_db_check():
            return "connected"
        else:
            return "disconnected"
    except Exception as e:
        return f"error: {str(e)[:50]}"

@app.get("/api/health")
async def health_check():
    """Fast health check with caching"""
    return {
        "status": "running",
        "database": _check_db_status(),
        "timestamp": int(time.time())
    }

@app.get("/api/n8n/settings/{user_id}")
async def get_settings_for_n8n(user_id: int, db: Session = Depends(get_db)):
    """
    Endpoint for n8n to fetch user-specific settings
    Usage: GET /api/n8n/settings/{user_id}
    """
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    from models import UserSettings
    
    # Get user settings
    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if not settings:
        # Return default settings if none exist
        return {
            "upwork": {
                "job_categories": ["Web Development"],
                "max_jobs": 3,
                "payment_verified": False
            },
            "freelancer": {
                "job_category": "Web Development",
                "max_jobs": 3
            }
        }
    
    # Use defaults if values are empty
    upwork_categories = settings.upwork_job_categories if settings.upwork_job_categories else ["Web Development"]
    upwork_max_jobs = settings.upwork_max_jobs if settings.upwork_max_jobs else 3
    freelancer_category = settings.freelancer_job_category if settings.freelancer_job_category else "Web Development"
    freelancer_max_jobs = settings.freelancer_max_jobs if settings.freelancer_max_jobs else 3
    
    return {
        "upwork": {
            "job_categories": upwork_categories,
            "max_jobs": upwork_max_jobs,
            "payment_verified": settings.upwork_payment_verified
        },
        "freelancer": {
            "job_category": freelancer_category,
            "max_jobs": freelancer_max_jobs
        }
    }

@app.get("/api/settings", response_model=SettingsResponse)
async def get_settings(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    from models import UserSettings
    user = get_user_by_email(email, db)
    
    # Get or create user settings
    settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
    if not settings:
        settings = UserSettings(
            user_id=user.id,
            upwork_job_categories=["Web Development"],
            upwork_max_jobs=3,
            upwork_payment_verified=False,
            freelancer_job_category="Web Development",
            freelancer_max_jobs=3
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)
    
    return settings

@app.put("/api/settings", response_model=SettingsResponse)
async def update_settings(
    settings_data: SettingsUpdate,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    from models import UserSettings
    user = get_user_by_email(email, db)
    
    # Get or create user settings
    settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
    if not settings:
        settings = UserSettings(user_id=user.id)
        db.add(settings)
    
    # Update settings
    if settings_data.upwork_job_categories is not None:
        settings.upwork_job_categories = settings_data.upwork_job_categories
    if settings_data.upwork_max_jobs is not None:
        settings.upwork_max_jobs = settings_data.upwork_max_jobs
    if settings_data.upwork_payment_verified is not None:
        settings.upwork_payment_verified = settings_data.upwork_payment_verified
    if settings_data.freelancer_job_category is not None:
        settings.freelancer_job_category = settings_data.freelancer_job_category
    if settings_data.freelancer_max_jobs is not None:
        settings.freelancer_max_jobs = settings_data.freelancer_max_jobs
    if settings_data.ai_agent_min_score is not None:
        settings.ai_agent_min_score = settings_data.ai_agent_min_score
    if settings_data.ai_agent_max_score is not None:
        settings.ai_agent_max_score = settings_data.ai_agent_max_score
    if settings_data.ai_agent_model is not None:
        settings.ai_agent_model = settings_data.ai_agent_model
    if settings_data.ai_agent_max_bids_freelancer is not None:
        settings.ai_agent_max_bids_freelancer = settings_data.ai_agent_max_bids_freelancer
    if settings_data.ai_agent_max_connects_upwork is not None:
        settings.ai_agent_max_connects_upwork = settings_data.ai_agent_max_connects_upwork
    
    settings.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(settings)
    
    return settings


# User Profile endpoints
@app.get("/api/profile", response_model=UserResponse)
async def get_profile(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    """Get current user profile"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    from models import UserSettings
    user = get_user_by_email(email, db)
    
    # Get user settings to include ai_agent_model
    settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
    
    # Create response with ai_agent_model from settings
    user_dict = {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "name": user.name,
        "telegram_chat_id": user.telegram_chat_id,
        "country": user.country,
        "ai_agent_model": settings.ai_agent_model if settings else "gpt-4"
    }
    
    return user_dict

@app.put("/api/profile", response_model=UserResponse)
async def update_profile(
    profile_data: UserProfileUpdate,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Update current user profile"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    from models import User
    user = get_user_by_email(email, db)
    
    # Update profile fields
    if profile_data.name is not None:
        user.name = profile_data.name
    if profile_data.telegram_chat_id is not None:
        user.telegram_chat_id = profile_data.telegram_chat_id
    if profile_data.country is not None:
        user.country = profile_data.country
    
    user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    
    return user


# Notification endpoints
@app.post("/api/notifications/webhook")
async def receive_notification_webhook(payload: dict, db: Session = Depends(get_db)):
    """
    Receive notifications from n8n webhook
    Expected payload: {
        "user_id": 1,
        "type": "success|info|warning|error",
        "title": "Notification Title",
        "message": "Notification message"
    }
    """
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import Notification
        
        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        
        notification = Notification(
            user_id=user_id,
            type=payload.get("type", "info"),
            title=payload.get("title", "Notification"),
            message=payload.get("message", ""),
            read=False
        )
        
        db.add(notification)
        db.commit()
        db.refresh(notification)
        
        return {
            "success": True,
            "message": "Notification received",
            "notification_id": notification.id
        }
    except Exception as e:
        print(f"Error saving notification: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.get("/api/notifications")
async def get_notifications(
    limit: int = 50,
    email: str = Depends(verify_token)
):
    """Get user's notifications - simplified version that works like the extension"""
    try:
        print(f"Fetching notifications for user: {email}")
        
        notifications = []
        
        # Try to get notifications from database if available
        try:
            from database import SessionLocal
            from models import Notification, User
            
            db = SessionLocal()
            try:
                user = db.query(User).filter(User.email == email).first()
                if user:
                    db_notifications = db.query(Notification)\
                        .filter(Notification.user_id == user.id)\
                        .order_by(Notification.created_at.desc())\
                        .limit(limit)\
                        .all()
                    
                    notifications = [
                        {
                            "id": n.id,
                            "type": n.type,
                            "title": n.title,
                            "message": n.message,
                            "read": n.read,
                            "created_at": n.created_at.isoformat() if n.created_at else None
                        }
                        for n in db_notifications
                    ]
                    print(f"✅ Found {len(notifications)} notifications in database")
            finally:
                db.close()
        except Exception as db_error:
            print(f"⚠️ Database connection failed, returning empty notifications: {db_error}")
            # Return empty notifications list when database is unavailable
            notifications = []
        
        return {
            "notifications": notifications
        }
        
    except Exception as e:
        print(f"❌ Error fetching notifications: {str(e)}")
        import traceback
        traceback.print_exc()
        # Return empty notifications instead of failing
        return {
            "notifications": []
        }

@app.put("/api/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: int,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Mark a notification as read"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import Notification
        user = get_user_by_email(email, db)
        
        notification = db.query(Notification).filter(
            Notification.id == notification_id,
            Notification.user_id == user.id
        ).first()
        if not notification:
            raise HTTPException(status_code=404, detail="Notification not found or access denied")
        
        notification.read = True
        notification.updated_at = datetime.utcnow()
        db.commit()
        
        return {"success": True, "message": "Notification marked as read"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error updating notification: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.put("/api/notifications/mark-all-read")
async def mark_all_notifications_read(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Mark all user's notifications as read"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import Notification
        user = get_user_by_email(email, db)
        
        db.query(Notification).filter(Notification.user_id == user.id).update({"read": True, "updated_at": datetime.utcnow()})
        db.commit()
        
        return {"success": True, "message": "All notifications marked as read"}
    except Exception as e:
        print(f"Error updating notifications: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.delete("/api/notifications/{notification_id}")
async def delete_notification(
    notification_id: int,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Delete a notification"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import Notification
        user = get_user_by_email(email, db)
        
        notification = db.query(Notification).filter(
            Notification.id == notification_id,
            Notification.user_id == user.id
        ).first()
        if not notification:
            raise HTTPException(status_code=404, detail="Notification not found or access denied")
        
        db.delete(notification)
        db.commit()
        
        return {"success": True, "message": "Notification deleted"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error deleting notification: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")


# Admin endpoints
def verify_admin(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    """Verify that the current user is an admin"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    user = get_user_by_email(email, db)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

@app.get("/api/admin/stats")
async def get_admin_stats(user = Depends(verify_admin), db: Session = Depends(get_db)):
    """Get system-wide statistics for admin dashboard"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import User, Lead
        from datetime import datetime, timedelta
        
        # Total users
        total_users = db.query(User).count()
        
        # Total leads
        total_leads = db.query(Lead).count()
        
        # Today's fetches (count users who fetched today)
        today = datetime.utcnow().date()
        today_fetches = 0
        all_users = db.query(User).all()
        for u in all_users:
            if u.upwork_last_reset and u.upwork_last_reset.date() == today:
                today_fetches += u.upwork_fetch_count or 0
            if u.freelancer_last_reset and u.freelancer_last_reset.date() == today:
                today_fetches += u.freelancer_fetch_count or 0
            if u.freelancer_plus_last_reset and u.freelancer_plus_last_reset.date() == today:
                today_fetches += u.freelancer_plus_fetch_count or 0
        
        # Platform breakdown with revenue
        platform_counts = {}
        platform_revenue = {}
        all_leads = db.query(Lead).all()
        
        # Calculate total revenue and proposal success rate
        total_revenue = 0
        total_proposals_sent = 0
        total_proposals_accepted = 0
        
        for lead in all_leads:
            platform = lead.platform or "Unknown"
            platform_counts[platform] = platform_counts.get(platform, 0) + 1
            
            # Track revenue per platform
            lead_revenue = getattr(lead, 'revenue', 0) or 0
            platform_revenue[platform] = platform_revenue.get(platform, 0) + lead_revenue
            total_revenue += lead_revenue
            
            # Track proposal success
            if getattr(lead, 'proposal_sent', False):
                total_proposals_sent += 1
                if getattr(lead, 'proposal_accepted', False):
                    total_proposals_accepted += 1
        
        total_with_platform = sum(platform_counts.values())
        platform_breakdown = [
            {
                "name": platform,
                "count": count,
                "percentage": round((count / total_with_platform * 100), 1) if total_with_platform > 0 else 0,
                "revenue": platform_revenue.get(platform, 0)
            }
            for platform, count in platform_counts.items()
        ]
        
        # Calculate success rate
        success_rate = round((total_proposals_accepted / total_proposals_sent * 100), 1) if total_proposals_sent > 0 else 0
        
        return {
            "totalUsers": total_users,
            "totalLeads": total_leads,
            "todayFetches": today_fetches,
            "platformBreakdown": platform_breakdown,
            "totalRevenue": total_revenue,
            "proposalsSent": total_proposals_sent,
            "proposalsAccepted": total_proposals_accepted,
            "successRate": success_rate
        }
    except Exception as e:
        print(f"Error fetching admin stats: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.get("/api/admin/users")
async def get_all_users(user = Depends(verify_admin), db: Session = Depends(get_db)):
    """Get all users with their stats"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import User, Lead
        
        all_users = db.query(User).all()
        users_data = []
        
        for u in all_users:
            # Count total leads for this user
            leads_count = db.query(Lead).filter(Lead.user_id == u.id).count()
            
            # Count approved leads (proposal_accepted = True)
            approved_leads_count = db.query(Lead).filter(
                Lead.user_id == u.id,
                Lead.proposal_accepted == True
            ).count()
            
            users_data.append({
                "id": u.id,
                "email": u.email,
                "name": u.name,
                "role": u.role,
                "upwork_fetch_count": u.upwork_fetch_count or 0,
                "freelancer_fetch_count": u.freelancer_fetch_count or 0,
                "freelancer_plus_fetch_count": u.freelancer_plus_fetch_count or 0,
                "leads_count": leads_count,
                "approved_leads_count": approved_leads_count,
                "created_at": u.created_at.isoformat() if u.created_at else None
            })
        
        return {"users": users_data}
    except Exception as e:
        print(f"Error fetching users: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.put("/api/admin/users/{user_id}")
async def update_user(
    user_id: int,
    update_data: dict,
    admin_user = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    """Update user role and limits"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import User
        
        target_user = db.query(User).filter(User.id == user_id).first()
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Update role if provided
        if "role" in update_data:
            target_user.role = update_data["role"]
        
        # Note: Daily limits are currently hardcoded in check_and_reset_daily_limit
        # For now, we'll store them in user settings or add columns to User model
        # This is a placeholder for future implementation
        
        target_user.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(target_user)
        
        return {
            "success": True,
            "message": "User updated successfully",
            "user": {
                "id": target_user.id,
                "email": target_user.email,
                "role": target_user.role
            }
        }
    except Exception as e:
        print(f"Error updating user: {e}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.delete("/api/admin/users/{user_id}")
async def delete_user(
    user_id: int,
    admin_user = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    """Delete a user and all their data"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import User
        
        # Prevent admin from deleting themselves
        if admin_user.id == user_id:
            raise HTTPException(status_code=400, detail="Cannot delete your own account")
        
        target_user = db.query(User).filter(User.id == user_id).first()
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Delete user (cascade will delete leads, settings, notifications)
        db.delete(target_user)
        db.commit()
        
        return {
            "success": True,
            "message": f"User {target_user.email} deleted successfully"
        }
    except Exception as e:
        print(f"Error deleting user: {e}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.post("/api/admin/users/{user_id}/reset-fetch-count")
async def reset_user_fetch_count(
    user_id: int,
    admin_user = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    """Reset fetch counts for a user"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import User
        
        target_user = db.query(User).filter(User.id == user_id).first()
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Reset all fetch counts
        target_user.upwork_fetch_count = 0
        target_user.freelancer_fetch_count = 0
        target_user.freelancer_plus_fetch_count = 0
        target_user.upwork_last_reset = datetime.utcnow()
        target_user.freelancer_last_reset = datetime.utcnow()
        target_user.freelancer_plus_last_reset = datetime.utcnow()
        
        db.commit()
        
        return {
            "success": True,
            "message": f"Fetch counts reset for {target_user.email}"
        }
    except Exception as e:
        print(f"Error resetting fetch count: {e}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.get("/api/admin/settings")
async def get_admin_settings(user = Depends(verify_admin), db: Session = Depends(get_db)):
    """Get system-wide settings"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        # Get settings from database
        system_settings = get_system_settings(db)
        
        return {
            "default_upwork_limit": system_settings.default_upwork_limit,
            "default_freelancer_limit": system_settings.default_freelancer_limit,
            "default_freelancer_plus_limit": system_settings.default_freelancer_plus_limit,
            "default_upwork_max_jobs": getattr(system_settings, 'default_upwork_max_jobs', 3),
            "default_freelancer_max_jobs": getattr(system_settings, 'default_freelancer_max_jobs', 3)
        }
    except Exception as e:
        print(f"Error fetching admin settings: {e}")
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.put("/api/admin/settings")
async def update_admin_settings(
    settings_data: dict,
    user = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    """Update system-wide settings"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        # Get or create system settings
        system_settings = get_system_settings(db)
        
        # Update daily limits
        if "default_upwork_limit" in settings_data:
            system_settings.default_upwork_limit = settings_data["default_upwork_limit"]
        if "default_freelancer_limit" in settings_data:
            system_settings.default_freelancer_limit = settings_data["default_freelancer_limit"]
        if "default_freelancer_plus_limit" in settings_data:
            system_settings.default_freelancer_plus_limit = settings_data["default_freelancer_plus_limit"]
        
        # Update max jobs per fetch
        if "default_upwork_max_jobs" in settings_data:
            system_settings.default_upwork_max_jobs = settings_data["default_upwork_max_jobs"]
        if "default_freelancer_max_jobs" in settings_data:
            system_settings.default_freelancer_max_jobs = settings_data["default_freelancer_max_jobs"]
        
        system_settings.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(system_settings)
        
        print(f"✅ Admin settings updated: Upwork Limit={system_settings.default_upwork_limit}, Freelancer Limit={system_settings.default_freelancer_limit}, Freelancer+ Limit={system_settings.default_freelancer_plus_limit}")
        print(f"   Max Jobs: Upwork={getattr(system_settings, 'default_upwork_max_jobs', 3)}, Freelancer={getattr(system_settings, 'default_freelancer_max_jobs', 3)}")
        
        return {
            "success": True,
            "message": "Settings updated successfully",
            "settings": {
                "default_upwork_limit": system_settings.default_upwork_limit,
                "default_freelancer_limit": system_settings.default_freelancer_limit,
                "default_freelancer_plus_limit": system_settings.default_freelancer_plus_limit,
                "default_upwork_max_jobs": getattr(system_settings, 'default_upwork_max_jobs', 3),
                "default_freelancer_max_jobs": getattr(system_settings, 'default_freelancer_max_jobs', 3)
            }
        }
    except Exception as e:
        print(f"Error updating admin settings: {e}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/analytics")
async def get_admin_analytics(
    range: str = "7d",
    user = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    """Get analytics data for admin dashboard"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import User, Lead
        from datetime import datetime, timedelta
        
        # Parse time range
        days = 7
        if range == "30d":
            days = 30
        elif range == "90d":
            days = 90
        
        start_date = datetime.utcnow() - timedelta(days=days)
        
        # Fetch trends (mock data for now - would need to track fetch history)
        fetch_trends = []
        for i in range(days):
            date = start_date + timedelta(days=i)
            fetch_trends.append({
                "date": date.strftime("%b %d"),
                "upwork": 0,  # Would need fetch history table
                "freelancer": 0,
                "freelancer_plus": 0
            })
        
        # User activity
        user_activity = []
        for i in range(days):
            date = start_date + timedelta(days=i)
            date_end = date + timedelta(days=1)
            
            # Count users created on this day
            new_users = db.query(User).filter(
                User.created_at >= date,
                User.created_at < date_end
            ).count()
            
            user_activity.append({
                "date": date.strftime("%b %d"),
                "active_users": 0,  # Would need activity tracking
                "new_users": new_users
            })
        
        # Platform performance
        platform_stats = {}
        all_leads = db.query(Lead).filter(Lead.created_at >= start_date).all()
        
        for lead in all_leads:
            platform = lead.platform or "Unknown"
            if platform not in platform_stats:
                platform_stats[platform] = {"total": 0}
            platform_stats[platform]["total"] += 1
        
        platform_performance = [
            {
                "name": platform,
                "total_leads": stats["total"],
                "avg_per_fetch": round(stats["total"] / days, 1),
                "success_rate": 95  # Mock data
            }
            for platform, stats in platform_stats.items()
        ]
        
        return {
            "fetchTrends": fetch_trends,
            "userActivity": user_activity,
            "platformPerformance": platform_performance
        }
    except Exception as e:
        print(f"Error fetching analytics: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/leads")
async def get_admin_leads(user = Depends(verify_admin), db: Session = Depends(get_db)):
    """Get all leads from all users for admin analytics"""
    try:
        if db is None:
            return {"leads": []}
        
        from models import Lead
        
        # Get all leads from all users
        leads = db.query(Lead).order_by(Lead.updated_at.desc()).all()
        
        return {
            "leads": [
                {
                    "id": lead.id,
                    "platform": lead.platform,
                    "title": lead.title,
                    "budget": lead.budget,
                    "bids": lead.bids if hasattr(lead, 'bids') else 0,
                    "cost": lead.cost if hasattr(lead, 'cost') else 0,
                    "posted": lead.posted,
                    "posted_time": lead.posted_time.isoformat() if lead.posted_time else None,
                    "status": lead.status,
                    "score": lead.score,
                    "description": lead.description,
                    "category": lead.category if hasattr(lead, 'category') else 'Uncategorized',
                    "Proposal": lead.proposal,
                    "proposal_accepted": lead.proposal_accepted if hasattr(lead, 'proposal_accepted') else False,
                    "url": lead.url,
                    "avg_bid_price": lead.avg_bid_price if hasattr(lead, 'avg_bid_price') else None,
                    "created_at": lead.created_at.isoformat() if lead.created_at else None,
                    "updated_at": lead.updated_at.isoformat() if lead.updated_at else None,
                    "user_id": lead.user_id
                }
                for lead in leads
            ]
        }
    except Exception as e:
        print(f"❌ Error fetching admin leads: {e}")
        import traceback
        traceback.print_exc()
        return {"leads": []}


# Talent endpoints
@app.post("/api/talents", status_code=status.HTTP_201_CREATED)
async def create_talent(
    talent_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Create a new talent entry"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import Talent
        from schemas import TalentCreate
        
        user = get_user_by_email(email, db)
        
        # Validate input
        talent_create = TalentCreate(**talent_data)
        
        # Create new talent
        new_talent = Talent(
            user_id=user.id,
            name=talent_create.name,
            description=talent_create.description,
            rate=talent_create.rate,
            rating=talent_create.rating,
            reviews=talent_create.reviews,
            skills=talent_create.skills or [],
            location=talent_create.location,
            profile_url=talent_create.profile_url,
            image_url=talent_create.image_url
        )
        
        db.add(new_talent)
        db.commit()
        db.refresh(new_talent)
        
        return {
            "id": new_talent.id,
            "user_id": new_talent.user_id,
            "name": new_talent.name,
            "description": new_talent.description,
            "rate": new_talent.rate,
            "rating": new_talent.rating,
            "reviews": new_talent.reviews,
            "skills": new_talent.skills,
            "location": new_talent.location,
            "profile_url": new_talent.profile_url,
            "image_url": new_talent.image_url,
            "created_at": new_talent.created_at.isoformat() if new_talent.created_at else None,
            "updated_at": new_talent.updated_at.isoformat() if new_talent.updated_at else None
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error creating talent: {str(e)}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create talent")

@app.get("/api/talents")
async def get_talents(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get all talents for the current user"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import Talent
        user = get_user_by_email(email, db)
        
        talents = db.query(Talent).filter(Talent.user_id == user.id).order_by(Talent.created_at.desc()).all()
        
        return {
            "talents": [
                {
                    "id": t.id,
                    "user_id": t.user_id,
                    "name": t.name,
                    "description": t.description,
                    "rate": t.rate,
                    "rating": t.rating,
                    "reviews": t.reviews,
                    "skills": t.skills,
                    "location": t.location,
                    "profile_url": t.profile_url,
                    "image_url": t.image_url,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "updated_at": t.updated_at.isoformat() if t.updated_at else None
                }
                for t in talents
            ]
        }
    except Exception as e:
        print(f"Error fetching talents: {str(e)}")
        return {"talents": []}

@app.get("/api/talents/{talent_id}")
async def get_talent(
    talent_id: int,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get a specific talent by ID"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import Talent
        user = get_user_by_email(email, db)
        
        talent = db.query(Talent).filter(Talent.id == talent_id, Talent.user_id == user.id).first()
        if not talent:
            raise HTTPException(status_code=404, detail="Talent not found or access denied")
        
        return {
            "id": talent.id,
            "user_id": talent.user_id,
            "name": talent.name,
            "description": talent.description,
            "rate": talent.rate,
            "rating": talent.rating,
            "reviews": talent.reviews,
            "skills": talent.skills,
            "location": talent.location,
            "profile_url": talent.profile_url,
            "image_url": talent.image_url,
            "created_at": talent.created_at.isoformat() if talent.created_at else None,
            "updated_at": talent.updated_at.isoformat() if talent.updated_at else None
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching talent: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch talent")

@app.put("/api/talents/{talent_id}")
async def update_talent(
    talent_id: int,
    talent_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Update a talent entry"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import Talent
        from schemas import TalentUpdate
        
        user = get_user_by_email(email, db)
        
        talent = db.query(Talent).filter(Talent.id == talent_id, Talent.user_id == user.id).first()
        if not talent:
            raise HTTPException(status_code=404, detail="Talent not found or access denied")
        
        # Validate input
        talent_update = TalentUpdate(**talent_data)
        
        # Update fields
        if talent_update.name is not None:
            talent.name = talent_update.name
        if talent_update.description is not None:
            talent.description = talent_update.description
        if talent_update.rate is not None:
            talent.rate = talent_update.rate
        if talent_update.rating is not None:
            talent.rating = talent_update.rating
        if talent_update.reviews is not None:
            talent.reviews = talent_update.reviews
        if talent_update.skills is not None:
            talent.skills = talent_update.skills
        if talent_update.location is not None:
            talent.location = talent_update.location
        if talent_update.profile_url is not None:
            talent.profile_url = talent_update.profile_url
        if talent_update.image_url is not None:
            talent.image_url = talent_update.image_url
        
        talent.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(talent)
        
        return {
            "id": talent.id,
            "user_id": talent.user_id,
            "name": talent.name,
            "description": talent.description,
            "rate": talent.rate,
            "rating": talent.rating,
            "reviews": talent.reviews,
            "skills": talent.skills,
            "location": talent.location,
            "profile_url": talent.profile_url,
            "image_url": talent.image_url,
            "created_at": talent.created_at.isoformat() if talent.created_at else None,
            "updated_at": talent.updated_at.isoformat() if talent.updated_at else None
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error updating talent: {str(e)}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update talent")

@app.delete("/api/talents/{talent_id}")
async def delete_talent(
    talent_id: int,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Delete a talent entry"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import Talent
        user = get_user_by_email(email, db)
        
        talent = db.query(Talent).filter(Talent.id == talent_id, Talent.user_id == user.id).first()
        if not talent:
            raise HTTPException(status_code=404, detail="Talent not found or access denied")
        
        db.delete(talent)
        db.commit()
        
        return {"success": True, "message": "Talent deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error deleting talent: {str(e)}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Failed to delete talent")

# Freelancer Extension API Endpoints
from pydantic import BaseModel
from typing import Optional

class BidRequest(BaseModel):
    access_token: str
    project_id: int
    bidder_id: int
    amount: float
    period: int = 7
    description: str
    milestone_percentage: int = 100
    freelancer_cookies: Optional[str] = None

class ProjectsRequest(BaseModel):
    access_token: str
    limit: int = 20
    freelancer_cookies: Optional[str] = None

class MessageRequest(BaseModel):
    thread_id: int
    message: str
    access_token: str
    freelancer_cookies: Optional[str] = None

@app.get("/api/freelancer/test")
async def test_freelancer():
    """Test if we can reach Freelancer API"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://www.freelancer.com/api/projects/0.1/projects/active/?limit=1",
                timeout=10.0
            )
            return {
                "success": True,
                "status_code": response.status_code,
                "message": "Can reach Freelancer API"
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

@app.get("/api/freelancer/debug")
async def debug_freelancer_credentials(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Debug endpoint to check freelancer credentials for current user"""
    try:
        from models import User, FreelancerCredentials
        
        # Get user
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return {"error": "User not found", "user_email": email}
        
        # Get credentials
        credentials = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if not credentials:
            return {
                "user_email": email,
                "user_id": user.id,
                "has_credentials": False,
                "message": "No freelancer credentials found for this user"
            }
        
        return {
            "user_email": email,
            "user_id": user.id,
            "has_credentials": True,
            "credentials": {
                "id": credentials.id,
                "user_id": credentials.user_id,
                "freelancer_user_id": credentials.freelancer_user_id,
                "validated_username": credentials.validated_username,
                "validated_email": credentials.validated_email,
                "is_validated": credentials.is_validated,
                "has_access_token": bool(credentials.access_token),
                "has_csrf_token": bool(credentials.csrf_token),
                "has_auth_hash": bool(credentials.auth_hash),
                "has_cookies": bool(credentials.cookies),
                "created_at": credentials.created_at.isoformat() if credentials.created_at else None,
                "updated_at": credentials.updated_at.isoformat() if credentials.updated_at else None,
                "last_validated": credentials.last_validated.isoformat() if credentials.last_validated else None
            }
        }
        
    except Exception as e:
        return {
            "error": str(e),
            "user_email": email
        }

@app.post("/api/bid/place")
async def place_bid(bid: BidRequest):
    """Place a bid on a Freelancer project"""
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            
            print(f"Attempting bid on project {bid.project_id}...")
            
            # Prepare headers - mimic browser request exactly
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Origin": "https://www.freelancer.com",
                "Referer": f"https://www.freelancer.com/projects/{bid.project_id}",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            }
            
            # Use OAuth token if available
            if bid.access_token and bid.access_token != 'using_cookies':
                print("Using OAuth token for authentication")
                headers["Authorization"] = f"Bearer {bid.access_token}"
                headers["freelancer-oauth-v1"] = bid.access_token
            else:
                print("Using cookie-based authentication (No OAuth token provided)")
            
            # Parse ALL cookies from the JSON cookie string
            cookies_dict = {}
            csrf_token = None
            user_id = None
            auth_hash = None
            
            if bid.freelancer_cookies:
                try:
                    # Parse JSON cookie object
                    cookie_data = json.loads(bid.freelancer_cookies)
                    
                    user_id = cookie_data.get('GETAFREE_USER_ID')
                    auth_hash = cookie_data.get('GETAFREE_AUTH_HASH_V2')
                    csrf_token = cookie_data.get('XSRF_TOKEN')
                    session2 = cookie_data.get('session2', '')
                    qfence = cookie_data.get('qfence', '')
                    cookieconsent_status = cookie_data.get('cookieconsent_status', '')
                    
                    # MANDATORY: Check all required cookies exist
                    if not user_id or not auth_hash:
                        return {
                            "success": False,
                            "error": "Missing required cookies (GETAFREE_USER_ID or GETAFREE_AUTH_HASH_V2). Please extract credentials again.",
                            "status_code": 400
                        }
                    
                    if not session2:
                        return {
                            "success": False,
                            "error": "session2 cookie missing - this is required for bidding. Please log out and log back into Freelancer.com, then extract credentials again.",
                            "status_code": 400
                        }
                    
                    # Make CSRF token optional - try bidding without it if missing
                    if not csrf_token:
                        print("Warning: XSRF-TOKEN cookie missing - will attempt bidding without CSRF protection")
                        csrf_token = ""  # Set empty string to avoid None errors
                    
                    # Set ALL required cookies
                    cookies_dict['GETAFREE_USER_ID'] = user_id
                    cookies_dict['GETAFREE_AUTH_HASH_V2'] = auth_hash
                    if csrf_token:
                        cookies_dict['XSRF-TOKEN'] = csrf_token
                    cookies_dict['session2'] = session2
                    
                    if qfence:
                        cookies_dict['qfence'] = qfence
                    
                    if cookieconsent_status:
                        cookies_dict['cookieconsent_status'] = cookieconsent_status
                    
                    # Add CSRF headers only if token is available
                    if csrf_token:
                        headers["X-CSRF-Token"] = csrf_token
                        headers["X-XSRF-TOKEN"] = csrf_token
                        headers["x-csrf-token"] = csrf_token
                        headers["x-xsrf-token"] = csrf_token
                        print(f"  All CSRF headers set with token: {csrf_token[:20]}...")
                    else:
                        print("  No CSRF token available - proceeding without CSRF headers")
                    headers["x-requested-with"] = "XMLHttpRequest"
                    headers["freelancer-auth-v2"] = f"{user_id};{auth_hash}"
                    headers["sec-ch-ua-platform"] = '"Windows"'
                    
                    print(f"Using Freelancer session cookies:")
                    print(f"  GETAFREE_USER_ID: {user_id}")
                    print(f"  GETAFREE_AUTH_HASH_V2: {auth_hash[:30]}...")
                    print(f"  XSRF-TOKEN: {csrf_token[:20] if csrf_token else 'Not provided'}...")
                    print(f"  session2: {'Present' if session2 else 'MISSING (REQUIRED)'}")
                    print(f"  qfence: {'Present' if qfence else 'Missing'}")
                    print(f"  cookieconsent_status: {'Present' if cookieconsent_status else 'Missing'}")
                    print(f"  freelancer-auth-v2: {user_id};{auth_hash[:20]}...")
                    print(f"  x-requested-with: XMLHttpRequest")
                    print(f"  All CSRF headers: X-CSRF-Token, X-XSRF-TOKEN, x-csrf-token, x-xsrf-token")
                        
                except json.JSONDecodeError as e:
                    print(f"Error parsing cookie JSON: {e}")
                    return {
                        "success": False,
                        "error": "Invalid cookie format. Please extract credentials again.",
                        "status_code": 400
                    }
            else:
                if not headers.get("Authorization"):
                    print("Error: No Freelancer cookies provided and no Access Token")
                    return {
                        "success": False,
                        "error": "No authentication credentials. Please extract credentials from Token tab.",
                        "status_code": 401
                    }
                else:
                    print("Warning: No Freelancer cookies provided, relying on Access Token")
            
            # Verify we have credentials (either cookies or token)
            if not cookies_dict and not headers.get("Authorization"):
                return {
                    "success": False,
                    "error": "No authentication credentials provided. Please extract credentials first.",
                    "status_code": 401
                }
            
            # CRITICAL: Ensure bidder_id matches the authenticated user
            if user_id and str(bid.bidder_id) != str(user_id):
                return {
                    "success": False,
                    "error": f"Bidder ID mismatch: bidder_id ({bid.bidder_id}) must match GETAFREE_USER_ID ({user_id}). Please use the correct user ID.",
                    "status_code": 400
                }
            
            # Use the exact endpoint that Freelancer's frontend uses with query parameters
            api_url = "https://www.freelancer.com/api/projects/0.1/bids/?compact=true&new_errors=true&new_pools=true"
            
            # Prepare JSON payload exactly as Freelancer frontend does
            payload = {
                "project_id": bid.project_id,
                "bidder_id": bid.bidder_id,
                "amount": bid.amount,
                "period": bid.period,
                "milestone_percentage": bid.milestone_percentage,
                "highlighted": False,
                "sponsored": False,
                "ip_contract": False,
                "anonymous": False,
                "description": bid.description
            }
            
            # Use JSON content type as Freelancer frontend does
            headers["Content-Type"] = "application/json"
            
            print(f"Attempting API endpoint: {api_url}")
            print(f"JSON payload: {payload}")
            print(f"Cookies: {list(cookies_dict.keys())}")
            print(f"CSRF headers: X-CSRF-TOKEN = {headers.get('X-CSRF-TOKEN', 'Not set')[:20]}...")
            
            response = await client.post(
                api_url,
                headers=headers,
                cookies=cookies_dict,
                json=payload,
                timeout=30.0
            )
            
            print(f"Response status: {response.status_code}")
            print(f"Response headers: {dict(response.headers)}")
            response_text = response.text
            print(f"Response body: {response_text[:1000]}")
            
            # Log full response for debugging
            if response.status_code != 200 and response.status_code != 201:
                print(f"FULL ERROR RESPONSE: {response_text}")
            
            if response.status_code == 200 or response.status_code == 201:
                try:
                    response_data = response.json()
                    print(f"Success! Bid placed: {response_data}")
                    return {
                        "success": True,
                        "data": response_data,
                        "message": f"Bid placed successfully: ${bid.amount}"
                    }
                except Exception as e:
                    print(f"Error parsing response: {e}")
                    return {
                        "success": True,
                        "message": f"Bid placed (status {response.status_code})"
                    }
            else:
                error_text = response.text
                print(f"Bid failed with status {response.status_code}")
                print(f"Error details: {error_text}")
                
                # Parse error message
                try:
                    error_json = response.json()
                    error_msg = error_json.get('message', error_text)
                except:
                    error_msg = error_text
                
                return {
                    "success": False,
                    "error": error_msg,
                    "status_code": response.status_code,
                    "full_response": error_text[:500]
                }
                
    except Exception as e:
        print(f"Exception: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/projects/list")
async def list_projects(request: ProjectsRequest):
    """Get list of active projects based on user skills"""
    try:
        async with httpx.AsyncClient() as client:
            # First get user's skills
            user_skills = []
            try:
                user_response = await client.get(
                    "https://www.freelancer.com/api/users/0.1/self?limit=1&jobs=true&webapp=1&compact=true&new_errors=true&new_pools=true",
                    headers={
                        "Authorization": f"Bearer {request.access_token}",
                        "freelancer-oauth-v1": request.access_token,
                    },
                    timeout=30.0
                )
                
                if user_response.status_code == 200:
                    user_data = user_response.json()
                    user = user_data.get('result', {})
                    
                    # Extract skill IDs from user profile
                    if user.get('jobs'):
                        user_skills = [job['id'] for job in user['jobs']]
                        print(f"Found user skills: {user_skills}")
                        
            except Exception as e:
                print(f"Could not get user skills: {e}")
            
            # Build search URL with user's skills using the API endpoint (repeat jobs[] for each ID)
            if user_skills:
                skills_params = '&'.join([f'jobs[]={skill_id}' for skill_id in user_skills])
                search_url = f"https://www.freelancer.com/api/projects/0.1/projects/active/?compact=true&limit={request.limit}&user_details=true&{skills_params}&languages[]=en"
                print(f"Searching projects with skills: {user_skills}")
                print(f"API Search URL: {search_url}")
                print(f"Equivalent web URL: https://www.freelancer.com/search/projects?projectSkills={','.join(map(str, user_skills))}&projectLanguages=en")
            else:
                search_url = f"https://www.freelancer.com/api/projects/0.1/projects/active/?compact=true&limit={request.limit}&user_details=true&user_recommended=true"
                print("Using recommended projects (no skills found)")
            
            # Fetch projects
            response = await client.get(
                search_url,
                headers={
                    "Authorization": f"Bearer {request.access_token}",
                    "freelancer-oauth-v1": request.access_token,
                },
                timeout=30.0
            )
            
            if response.status_code == 200:
                return {
                    "success": True,
                    "data": response.json(),
                    "user_skills": user_skills,
                    "search_url": search_url
                }
            else:
                return {
                    "success": False,
                    "error": response.text,
                    "status_code": response.status_code
                }
                
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/user/info")
async def get_user_info(request: ProjectsRequest):
    """Get user information and check token scopes"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://www.freelancer.com/api/users/0.1/self/",
                headers={
                    "Authorization": f"Bearer {request.access_token}",
                    "freelancer-oauth-v1": request.access_token,
                },
                timeout=30.0
            )
            
            if response.status_code == 200:
                data = response.json()
                return {
                    "success": True,
                    "data": data,
                    "message": "Token is valid"
                }
            else:
                error_data = response.text
                # Check if it's a scope issue
                if "insufficient_scope" in error_data or response.status_code == 403:
                    return {
                        "success": False,
                        "error": "Token does not have required scopes for bidding",
                        "status_code": response.status_code,
                        "message": "You need to get a token with bid:write permissions. See the OAuth flow documentation."
                    }
                return {
                    "success": False,
                    "error": error_data,
                    "status_code": response.status_code
                }
                
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/token/check-scopes")
async def check_token_scopes(request: ProjectsRequest):
    """Check if token has bidding permissions"""
    try:
        async with httpx.AsyncClient() as client:
            # Try to access a bid-related endpoint to check permissions
            response = await client.get(
                "https://www.freelancer.com/api/users/0.1/self/",
                headers={
                    "Authorization": f"Bearer {request.access_token}",
                    "freelancer-oauth-v1": request.access_token,
                },
                timeout=30.0
            )
            
            if response.status_code == 200:
                return {
                    "success": True,
                    "message": "Token is valid for basic operations",
                    "warning": "Cannot verify bid:write scope without attempting a bid. Token may still lack bidding permissions."
                }
            elif response.status_code == 403:
                return {
                    "success": False,
                    "error": "Token has insufficient scopes",
                    "message": "You need to obtain a token with bid:write, project:read, and user:read scopes"
                }
            else:
                return {
                    "success": False,
                    "error": response.text,
                    "status_code": response.status_code
                }
                
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/message/send")
async def send_message(msg: MessageRequest):
    """Send a message in a Freelancer thread"""
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            
            print(f"Sending message to thread {msg.thread_id}...")
            
            # Prepare headers
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://www.freelancer.com",
                "Referer": f"https://www.freelancer.com/messages/{msg.thread_id}",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
            
            # Add OAuth header if token looks valid
            if msg.access_token and len(msg.access_token) > 50 and msg.access_token != 'using_cookies':
                headers["Authorization"] = f"Bearer {msg.access_token}"
                headers["freelancer-oauth-v1"] = msg.access_token
            
            # Add Freelancer session cookies if provided
            cookies_dict = {}
            if msg.freelancer_cookies:
                try:
                    # Parse JSON cookie object
                    cookie_data = json.loads(msg.freelancer_cookies)
                    
                    user_id = cookie_data.get('GETAFREE_USER_ID')
                    auth_hash = cookie_data.get('GETAFREE_AUTH_HASH_V2')
                    csrf_token = cookie_data.get('XSRF_TOKEN')
                    
                    if user_id and auth_hash:
                        cookies_dict['GETAFREE_USER_ID'] = user_id
                        cookies_dict['GETAFREE_AUTH_HASH_V2'] = auth_hash
                        if csrf_token:
                            cookies_dict['XSRF-TOKEN'] = csrf_token
                        print(f"Using Freelancer session cookies")
                except json.JSONDecodeError:
                    # Fallback to old format
                    parts = msg.freelancer_cookies.split(';')
                    if len(parts) >= 2:
                        cookies_dict['GETAFREE_USER_ID'] = unquote(parts[0])
                        cookies_dict['GETAFREE_AUTH_HASH_V2'] = unquote(parts[1])
                        print(f"Using Freelancer session cookies (legacy format)")
            
            # Send message
            url = f"https://www.freelancer.com/api/messages/0.1/threads/{msg.thread_id}/messages"
            
            response = await client.post(
                url,
                headers=headers,
                cookies=cookies_dict if cookies_dict else None,
                data={
                    "message": msg.message,
                    "source": "chat_box"
                },
                timeout=30.0
            )
            
            print(f"Response status: {response.status_code}")
            print(f"Response body: {response.text[:500]}")
            
            if response.status_code == 200 or response.status_code == 201:
                return {
                    "success": True,
                    "data": response.json(),
                    "message": "Message sent successfully"
                }
            else:
                error_text = response.text
                return {
                    "success": False,
                    "error": error_text,
                    "status_code": response.status_code
                }
                
    except Exception as e:
        print(f"Exception: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# Freelancer Credentials endpoints
@app.post("/api/freelancer/credentials", response_model=FreelancerCredentialsResponse)
async def save_freelancer_credentials(
    credentials: FreelancerCredentialsCreate,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Save or update Freelancer credentials for the authenticated user"""
    if db is None:
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")
    
    try:
        from models import User, FreelancerCredentials
        from datetime import datetime
        
        # Get user
        print(f"🔍 [SAVE_CREDS] Looking for user with email: {email}")
        user = db.query(User).filter(User.email == email).first()
        if not user:
            print(f"❌ [SAVE_CREDS] User not found with email: {email}")
            raise HTTPException(status_code=404, detail="User not found")
        
        print(f"✅ [SAVE_CREDS] Found user: ID={user.id}, Email={user.email}")
        
        # Check if credentials already exist
        existing_creds = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if existing_creds:
            print(f"📝 [SAVE_CREDS] Updating existing credentials for user {user.id}")
        else:
            print(f"🆕 [SAVE_CREDS] Creating new credentials for user {user.id}")
        
        if existing_creds:
            # Update existing credentials
            if credentials.access_token is not None:
                existing_creds.access_token = credentials.access_token
            if credentials.csrf_token is not None:
                existing_creds.csrf_token = credentials.csrf_token
            if credentials.freelancer_user_id is not None:
                existing_creds.freelancer_user_id = str(credentials.freelancer_user_id)
            if credentials.auth_hash is not None:
                existing_creds.auth_hash = credentials.auth_hash
            if credentials.cookies is not None:
                existing_creds.cookies = credentials.cookies
            if credentials.validated_username is not None:
                existing_creds.validated_username = credentials.validated_username
            if credentials.validated_email is not None:
                existing_creds.validated_email = credentials.validated_email
            
            # Mark as validated if we have credentials
            if credentials.access_token or credentials.cookies:
                existing_creds.is_validated = True
                existing_creds.last_validated = datetime.utcnow()
            
            existing_creds.updated_at = datetime.utcnow()
            
            db.commit()
            db.refresh(existing_creds)
            return existing_creds
        else:
            # Create new credentials
            new_creds = FreelancerCredentials(
                user_id=user.id,
                access_token=credentials.access_token,
                csrf_token=credentials.csrf_token,
                freelancer_user_id=str(credentials.freelancer_user_id) if credentials.freelancer_user_id is not None else None,
                auth_hash=credentials.auth_hash,
                cookies=credentials.cookies,
                validated_username=credentials.validated_username,
                validated_email=credentials.validated_email,
                is_validated=bool(credentials.access_token or credentials.cookies),
                last_validated=datetime.utcnow() if (credentials.access_token or credentials.cookies) else None
            )
            
            db.add(new_creds)
            db.commit()
            db.refresh(new_creds)
            return new_creds
            
    except Exception as e:
        print(f"Error saving Freelancer credentials: {e}")
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.get("/api/freelancer/credentials", response_model=FreelancerCredentialsResponse)
async def get_freelancer_credentials(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get Freelancer credentials for the authenticated user"""
    if db is None:
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")
    
    try:
        from models import User, FreelancerCredentials
        
        # Get user
        print(f"🔍 [GET_CREDS] Looking for user with email: {email}")
        user = db.query(User).filter(User.email == email).first()
        if not user:
            print(f"❌ [GET_CREDS] User not found with email: {email}")
            raise HTTPException(status_code=404, detail="User not found")
        
        print(f"✅ [GET_CREDS] Found user: ID={user.id}, Email={user.email}")
        
        # Get credentials
        credentials = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if credentials:
            print(f"📋 [GET_CREDS] Found credentials for user {user.id}: freelancer_user_id={credentials.freelancer_user_id}")
        else:
            print(f"❌ [GET_CREDS] No credentials found for user {user.id}")
        
        if not credentials:
            raise HTTPException(status_code=404, detail="No Freelancer credentials found")
        
        return credentials
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error getting Freelancer credentials: {e}")
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.put("/api/freelancer/credentials", response_model=FreelancerCredentialsResponse)
async def update_freelancer_credentials(
    credentials: FreelancerCredentialsUpdate,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Update Freelancer credentials for the authenticated user"""
    if db is None:
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")
    
    try:
        from models import User, FreelancerCredentials
        from datetime import datetime
        
        # Get user
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Get existing credentials
        existing_creds = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if not existing_creds:
            raise HTTPException(status_code=404, detail="No Freelancer credentials found")
        
        # Update fields
        if credentials.access_token is not None:
            existing_creds.access_token = credentials.access_token
        if credentials.csrf_token is not None:
            existing_creds.csrf_token = credentials.csrf_token
        if credentials.freelancer_user_id is not None:
            existing_creds.freelancer_user_id = str(credentials.freelancer_user_id)
        if credentials.auth_hash is not None:
            existing_creds.auth_hash = credentials.auth_hash
        if credentials.cookies is not None:
            existing_creds.cookies = credentials.cookies
        if credentials.is_validated is not None:
            existing_creds.is_validated = credentials.is_validated
        if credentials.validated_username is not None:
            existing_creds.validated_username = credentials.validated_username
        if credentials.validated_email is not None:
            existing_creds.validated_email = credentials.validated_email
        
        # Update validation timestamp if credentials are being validated
        if credentials.is_validated:
            existing_creds.last_validated = datetime.utcnow()
        
        existing_creds.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(existing_creds)
        return existing_creds
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error updating Freelancer credentials: {e}")
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.delete("/api/freelancer/credentials")
async def delete_freelancer_credentials(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Delete Freelancer credentials for the authenticated user"""
    if db is None:
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")
    
    try:
        from models import User, FreelancerCredentials
        
        # Get user
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Get and delete credentials
        credentials = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if not credentials:
            raise HTTPException(status_code=404, detail="No Freelancer credentials found")
        
        db.delete(credentials)
        db.commit()
        
        return {"message": "Freelancer credentials deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error deleting Freelancer credentials: {e}")
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

# Freelancer API endpoints for frontend integration
@app.get("/api/freelancer/status")
async def get_freelancer_status(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Check if user is connected to Freelancer.com - fast version using cached data"""
    if db is None:
        return {"connected": False, "error": "Database connection failed"}
    
    try:
        from models import User, FreelancerCredentials
        from datetime import datetime, timedelta
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return {"connected": False, "error": "User not found"}
        
        # Check if we have FreelancerCredentials in database (from extension)
        credentials = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if not credentials:
            return {"connected": False, "message": "No Freelancer credentials found. Please use the browser extension to connect."}
        
        # Fast check - if credentials were validated recently (within 1 hour), trust them
        if credentials.is_validated and credentials.last_validated:
            time_since_validation = datetime.utcnow() - credentials.last_validated
            if time_since_validation < timedelta(hours=1):
                print(f"✅ Using cached validation for user {user.email} (validated {time_since_validation.total_seconds():.0f}s ago)")
                
                user_info = None
                if credentials.validated_username:
                    user_info = {
                        "username": credentials.validated_username,
                        "id": credentials.freelancer_user_id,
                        "email": credentials.validated_email,
                        "display_name": credentials.validated_username
                    }
                
                return {
                    "connected": True,
                    "user": user_info,
                    "credentials": {
                        "username": credentials.validated_username,
                        "user_id": credentials.freelancer_user_id,
                        "updated_at": credentials.updated_at.isoformat() if credentials.updated_at else None,
                        "access_token": bool(credentials.access_token and credentials.access_token != "using_cookies"),
                        "cached": True
                    }
                }
        
        # If not recently validated or not validated at all, do a quick validation
        if credentials.access_token or credentials.cookies:
            print(f"🔍 Quick validation for user {user.email}")
            
            # Try a very fast API call to validate
            try:
                headers = {"Content-Type": "application/json"}
                cookies = {}
                
                # Use cookies if available (faster)
                if credentials.cookies:
                    try:
                        cookie_data = credentials.cookies if isinstance(credentials.cookies, dict) else json.loads(credentials.cookies)
                        if cookie_data.get("GETAFREE_USER_ID"):
                            cookies["GETAFREE_USER_ID"] = cookie_data["GETAFREE_USER_ID"]
                        if cookie_data.get("GETAFREE_AUTH_HASH_V2"):
                            cookies["GETAFREE_AUTH_HASH_V2"] = cookie_data["GETAFREE_AUTH_HASH_V2"]
                    except:
                        pass
                
                # Fallback to OAuth
                if credentials.access_token and credentials.access_token != "using_cookies":
                    headers["Authorization"] = f"Bearer {credentials.access_token}"
                    headers["freelancer-oauth-v1"] = credentials.access_token
                
                # Very fast validation call
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(
                        "https://www.freelancer.com/api/users/0.1/self?compact=true",
                        headers=headers,
                        cookies=cookies
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        user_data = data.get("result", {})
                        
                        # Update validation timestamp
                        credentials.is_validated = True
                        credentials.last_validated = datetime.utcnow()
                        if user_data.get("username"):
                            credentials.validated_username = user_data["username"]
                        if user_data.get("id"):
                            credentials.freelancer_user_id = str(user_data["id"])
                        db.commit()
                        
                        user_info = {
                            "username": user_data.get("username") or credentials.validated_username,
                            "id": user_data.get("id") or credentials.freelancer_user_id,
                            "email": user_data.get("email") or credentials.validated_email,
                            "display_name": user_data.get("display_name") or user_data.get("username") or credentials.validated_username
                        }
                        
                        print(f"✅ Quick validation successful for user {user.email}")
                        
                        return {
                            "connected": True,
                            "user": user_info,
                            "credentials": {
                                "username": user_info["username"],
                                "user_id": user_info["id"],
                                "updated_at": credentials.updated_at.isoformat() if credentials.updated_at else None,
                                "access_token": bool(credentials.access_token and credentials.access_token != "using_cookies"),
                                "validated_now": True
                            }
                        }
                    else:
                        print(f"⚠️ Quick validation failed: {response.status_code}")
                        
            except Exception as validation_error:
                print(f"⚠️ Quick validation error: {validation_error}")
        
        return {"connected": False, "message": "Please connect using the browser extension"}
        
    except Exception as e:
        print(f"❌ Error checking Freelancer status: {e}")
        return {"connected": False, "error": str(e)}

@app.get("/api/freelancer/projects")
async def get_freelancer_projects(
    search: str = "",
    minBudget: str = "",
    maxBudget: str = "",
    projectType: str = "all",
    limit: int = 20,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get projects from Freelancer.com using stored credentials - matches extension logic exactly"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, FreelancerCredentials
        import json
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        credentials = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if not credentials or not credentials.is_validated:
            raise HTTPException(status_code=400, detail="Not connected to Freelancer.com")
        
        # Prepare headers and cookies for Freelancer API
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
                
                print(f"🍪 Using cookies for API calls: {list(cookies.keys())}")
            except Exception as e:
                print(f"⚠️ Error parsing cookies: {e}")
        
        # Fallback to OAuth token if no cookies or as backup
        if credentials.access_token and credentials.access_token != "using_cookies":
            headers["Authorization"] = f"Bearer {credentials.access_token}"
            headers["freelancer-oauth-v1"] = credentials.access_token
            print("🔑 Using OAuth token for API calls")
        
        print(f"🔍 Fetching projects for user {user.email}")
        
        # Step 1: First validate credentials by checking user profile
        user_skills = []
        user_validated = False
        
        try:
            user_profile_url = "https://www.freelancer.com/api/users/0.1/self?limit=1&jobs=true&webapp=1&compact=true&new_errors=true&new_pools=true"
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                user_response = await client.get(user_profile_url, headers=headers, cookies=cookies)
                
                if user_response.status_code == 200:
                    user_data = user_response.json()
                    user_profile = user_data.get("result", {})
                    user_validated = True
                    
                    # Extract skill IDs from user profile
                    if user_profile.get("jobs") and len(user_profile["jobs"]) > 0:
                        user_skills = [job["id"] for job in user_profile["jobs"]]
                        skill_names = [job["name"] for job in user_profile["jobs"]]
                        print(f"✓ Found user skills: {user_skills}")
                        print(f"✓ Skill names: {skill_names}")
                    else:
                        print("ℹ️ No skills found in user profile")
                elif user_response.status_code == 401:
                    print(f"❌ Authentication failed: {user_response.status_code}")
                    raise HTTPException(status_code=401, detail="Freelancer credentials expired. Please reconnect to Freelancer using the extension.")
                else:
                    print(f"⚠️ Could not get user profile: {user_response.status_code}")
                    # Continue without skills but with a warning
        except HTTPException:
            raise  # Re-raise HTTP exceptions
        except Exception as e:
            print(f"⚠️ Error getting user skills: {e}")
            # Continue without skills
        
        # If user validation failed completely, return error
        if not user_validated:
            raise HTTPException(status_code=401, detail="Cannot validate Freelancer credentials. Please reconnect to Freelancer using the extension.")
        
        # Step 2: Build search URL based on user skills (exactly like extension)
        if user_skills:
            # Use skills-based search with jobs[] parameters for each skill ID
            skills_params = "&".join([f"jobs[]={skill_id}" for skill_id in user_skills])
            search_url = f"https://www.freelancer.com/api/projects/0.1/projects/active/?compact=true&limit=20&user_details=true&jobs=true&{skills_params}&languages[]=en"
            print(f"🎯 Searching projects with user skills: {user_skills}")
            print(f"🔗 Search URL: {search_url}")
        else:
            # Fallback to recommended projects if no skills found
            search_url = "https://www.freelancer.com/api/projects/0.1/projects/active/?compact=true&limit=20&user_details=true&jobs=true&user_recommended=true"
            print("📋 Using recommended projects (no skills found)")
        
        # Apply additional filters if provided
        url_parts = search_url.split('?')
        base_url = url_parts[0]
        existing_params = url_parts[1] if len(url_parts) > 1 else ""
        
        additional_params = []
        if search:
            additional_params.append(f"query={search}")
        if minBudget:
            additional_params.append(f"min_budget={minBudget}")
        if maxBudget:
            additional_params.append(f"max_budget={maxBudget}")
        
        # No sorting - use Freelancer API default (newest first)
        
        # Combine all parameters
        all_params = existing_params
        if additional_params:
            if all_params:
                all_params += "&" + "&".join(additional_params)
            else:
                all_params = "&".join(additional_params)
        
        final_url = f"{base_url}?{all_params}"
        print(f"🌐 Final API URL: {final_url}")
        
        # Step 3: Fetch projects using the constructed URL
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(final_url, headers=headers, cookies=cookies)
            
            if response.status_code == 401:
                raise HTTPException(status_code=401, detail="Freelancer credentials expired. Please reconnect.")
            elif response.status_code != 200:
                error_text = await response.text()
                print(f"❌ API Error: {response.status_code} - {error_text}")
                raise HTTPException(status_code=response.status_code, detail="Failed to fetch projects from Freelancer")
            
            data = response.json()
            projects = data.get("result", {}).get("projects", [])
            users = data.get("result", {}).get("users", {})
            
            print(f"✅ Successfully fetched {len(projects)} projects")
            
            # Check if we have users data, if not fetch it separately
            if not users and projects:
                print("🔍 No users data in projects response, fetching client details...")
                try:
                    # Get unique owner IDs from projects
                    owner_ids = list(set([
                        str(project.get('owner_id')) 
                        for project in projects 
                        if project.get('owner_id')
                    ]))
                    
                    if owner_ids:
                        print(f"🔍 Fetching details for {len(owner_ids)} project owners: {owner_ids[:5]}...")
                        # Build users API URL
                        users_ids_param = "&".join([f"users[]={uid}" for uid in owner_ids[:20]])
                        users_url = f"https://www.freelancer.com/api/users/0.1/users/?{users_ids_param}&avatar=true&country_details=true&reputation=true&display_info=true"
                        
                        users_response = await client.get(users_url, headers=headers, cookies=cookies)
                        if users_response.status_code == 200:
                            users_data = users_response.json()
                            if 'result' in users_data and 'users' in users_data['result']:
                                users = {
                                    str(user['id']): user 
                                    for user in users_data['result']['users']
                                }
                                print(f"✅ Successfully fetched {len(users)} client details")
                            else:
                                print("⚠️ No users found in users API response")
                        else:
                            print(f"⚠️ Failed to fetch client details: {users_response.status_code}")
                except Exception as e:
                    print(f"⚠️ Error fetching client details: {e}")
            elif users:
                print(f"✅ Users data already available: {len(users)} users")
                # Convert users list to dict if needed
                if isinstance(users, list):
                    users = {
                        str(user['id']): user 
                        for user in users
                    }
            
            # Add metadata about the search
            search_info = {
                "skills_used": len(user_skills) > 0,
                "skill_count": len(user_skills),
                "search_type": "skills-based" if user_skills else "recommended",
                "total_projects": len(projects),
                "users_count": len(users) if users else 0
            }
            
            return {
                "projects": projects,
                "users": users,
                "search_info": search_info
            }
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error fetching Freelancer projects: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to fetch projects")

@app.get("/api/freelancer/projects/count")
async def get_freelancer_projects_count(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get count of available projects"""
    try:
        # For now, return a mock count. In production, this would call the Freelancer API
        return {"count": 25}
    except Exception as e:
        print(f"Error getting project count: {e}")
        return {"count": 0}

@app.get("/api/freelancer/messages/threads")
async def get_freelancer_message_threads(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get message threads from Freelancer.com"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, FreelancerCredentials
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        credentials = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if not credentials or not credentials.is_validated:
            raise HTTPException(status_code=400, detail="Not connected to Freelancer.com")
        
        # Prepare headers and cookies for Freelancer API
        headers, cookies = prepare_freelancer_request(credentials)
        
        api_url = "https://www.freelancer.com/api/messages/0.1/threads/?limit=20&user_details=true"
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(api_url, headers=headers, cookies=cookies)
            
            if response.status_code == 401:
                raise HTTPException(status_code=401, detail="Freelancer credentials expired. Please reconnect.")
            elif response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail="Failed to fetch message threads")
            
            data = response.json()
            
            return data
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching message threads: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch message threads")

@app.get("/api/freelancer/messages/count")
async def get_freelancer_messages_count(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get count of message threads"""
    try:
        # For now, return a mock count. In production, this would call the Freelancer API
        return {"count": 8}
    except Exception as e:
        print(f"Error getting message count: {e}")
        return {"count": 0}

@app.get("/api/freelancer/messages/{thread_id}")
async def get_freelancer_messages(
    thread_id: int,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get messages from a specific thread"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, FreelancerCredentials
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        credentials = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if not credentials or not credentials.is_validated:
            raise HTTPException(status_code=400, detail="Not connected to Freelancer.com")
        
        # Prepare headers and cookies for Freelancer API
        headers, cookies = prepare_freelancer_request(credentials)
        
        api_url = f"https://www.freelancer.com/api/messages/0.1/messages/?threads[]={thread_id}&limit=50"
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(api_url, headers=headers, cookies=cookies)
            
            if response.status_code == 401:
                raise HTTPException(status_code=401, detail="Freelancer credentials expired. Please reconnect.")
            elif response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail="Failed to fetch messages")
            
            data = response.json()
            
            # Add from_me flag based on user ID
            user_id = credentials.freelancer_user_id
            if 'result' in data and 'messages' in data['result']:
                for message in data['result']['messages']:
                    message["from_me"] = str(message.get("from_user")) == str(user_id)
            
            return data
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching messages: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch messages")

@app.post("/api/freelancer/messages/send")
async def send_freelancer_message(
    message_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Send a message to a Freelancer thread"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, FreelancerCredentials
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        credentials = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if not credentials or not credentials.is_validated:
            raise HTTPException(status_code=400, detail="Not connected to Freelancer.com")
        
        thread_id = message_data.get("threadId")
        message = message_data.get("message")
        file_url = message_data.get("fileUrl")
        file_name = message_data.get("fileName")
        
        if not thread_id or (not message and not file_url):
            raise HTTPException(status_code=400, detail="threadId and either message or fileUrl are required")
        
        # Prepare headers and cookies for Freelancer API
        headers, cookies = prepare_freelancer_request(credentials)
        
        api_url = f"https://www.freelancer.com/api/messages/0.1/threads/{thread_id}/messages"
        
        # Check if this is a file attachment
        if file_url and file_name:
            # Try to send as multipart form data with file
            headers["Content-Type"] = "multipart/form-data"
            
            # Prepare multipart form data
            form_data = {
                "message": message or f"📎 {file_name}",
                "source": "chat_box",
                "attachment_url": file_url,
                "attachment_name": file_name
            }
        else:
            # Regular text message
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            form_data = {
                "message": message,
                "source": "chat_box"
            }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(api_url, headers=headers, cookies=cookies, data=form_data)
            
            if response.status_code == 401:
                raise HTTPException(status_code=401, detail="Freelancer credentials expired. Please reconnect.")
            elif response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail="Failed to send message")
            
            return {"success": True, "message": "Message sent successfully"}
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error sending message: {e}")
        raise HTTPException(status_code=500, detail="Failed to send message")

@app.get("/api/freelancer/bids")
async def get_freelancer_bids(
    filter: str = "all",
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get user's bids from Freelancer.com"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, FreelancerCredentials
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        credentials = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if not credentials or not credentials.is_validated:
            raise HTTPException(status_code=400, detail="Not connected to Freelancer.com")
        
        # Prepare headers and cookies for Freelancer API
        headers, cookies = prepare_freelancer_request(credentials)
        
        # Get user's bids
        user_id = credentials.freelancer_user_id
        api_url = f"https://www.freelancer.com/api/projects/0.1/bids/?bidders[]={user_id}&limit=50"
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(api_url, headers=headers, cookies=cookies)
            
            if response.status_code == 401:
                raise HTTPException(status_code=401, detail="Freelancer credentials expired. Please reconnect.")
            elif response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail="Failed to fetch bids")
            
            data = response.json()
            
            # Log the structure for debugging
            print(f"📊 Bids API response structure: {list(data.keys())}")
            if 'result' in data:
                print(f"📊 Result keys: {list(data['result'].keys())}")
                if 'bids' in data['result']:
                    bids = data['result']['bids']
                    print(f"📊 Found {len(bids)} bids")
                    
                    # Get unique project IDs from bids
                    project_ids = list(set([bid.get('project_id') for bid in bids if bid.get('project_id')]))
                    print(f"📊 Need to fetch details for {len(project_ids)} projects")
                    
                    # Check if we already have users data from the bids API
                    if 'users' in data['result'] and data['result']['users']:
                        print(f"✅ Users data already available from bids API: {len(data['result']['users'])} users")
                        
                        # Convert users list to dict if needed
                        if isinstance(data['result']['users'], list):
                            data['result']['users'] = {
                                str(user['id']): user 
                                for user in data['result']['users']
                            }
                            print(f"🔄 Converted users list to dict: {len(data['result']['users'])} users")
                        
                        # Log sample user data
                        if data['result']['users']:
                            first_user_id = list(data['result']['users'].keys())[0]
                            first_user = data['result']['users'][first_user_id]
                            print(f"👤 Sample user {first_user_id}: {first_user.get('display_name', first_user.get('username', 'NO_NAME'))}")
                            print(f"👤 Sample user keys: {list(first_user.keys())[:10]}...")
                    
                    # Check if we already have projects data from the bids API
                    if 'projects' in data['result'] and data['result']['projects']:
                        print(f"✅ Projects data already available from bids API: {len(data['result']['projects'])} projects")
                        
                        # Convert projects list to dict if needed
                        if isinstance(data['result']['projects'], list):
                            data['result']['projects'] = {
                                str(project['id']): project 
                                for project in data['result']['projects']
                            }
                            print(f"🔄 Converted projects list to dict: {len(data['result']['projects'])} projects")
                    
                    # Only fetch additional project details if we don't have them
                    if 'projects' not in data['result'] or not data['result']['projects']:
                        print("🔍 No projects data in bids response, fetching separately...")
                        try:
                            # Build project details API URL
                            project_ids_param = "&".join([f"projects[]={pid}" for pid in project_ids[:20]])  # Limit to 20 projects
                            projects_url = f"https://www.freelancer.com/api/projects/0.1/projects/?{project_ids_param}&user_details=true"
                            
                            projects_response = await client.get(projects_url, headers=headers, cookies=cookies)
                            if projects_response.status_code == 200:
                                projects_data = projects_response.json()
                                if 'result' in projects_data and 'projects' in projects_data['result']:
                                    # Handle both list and dict formats for projects
                                    projects_raw = projects_data['result']['projects']
                                    if isinstance(projects_raw, list):
                                        data['result']['projects'] = {
                                            str(project['id']): project 
                                            for project in projects_raw
                                        }
                                    else:
                                        data['result']['projects'] = projects_raw
                                    
                                    print(f"✅ Fetched {len(data['result']['projects'])} projects separately")
                                    
                                    # Also get users data if available
                                    if 'users' in projects_data['result'] and projects_data['result']['users']:
                                        if isinstance(projects_data['result']['users'], list):
                                            data['result']['users'] = {
                                                str(user['id']): user 
                                                for user in projects_data['result']['users']
                                            }
                                        else:
                                            data['result']['users'] = projects_data['result']['users']
                                        print(f"✅ Also got {len(data['result']['users'])} users from projects API")
                                else:
                                    print("⚠️ No projects found in projects API response")
                            else:
                                print(f"⚠️ Failed to fetch projects: {projects_response.status_code}")
                        except Exception as e:
                            print(f"⚠️ Error fetching projects separately: {e}")
                    else:
                        print("✅ Using existing projects and users data from bids API")
                
                if 'projects' in data['result']:
                    print(f"📊 Found {len(data['result']['projects'])} projects")
            
            return data
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching bids: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch bids")

@app.get("/api/freelancer/bids/count")
async def get_freelancer_bids_count(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get count of user's bids"""
    try:
        # For now, return a mock count. In production, this would call the Freelancer API
        return {"count": 12}
    except Exception as e:
        print(f"Error getting bid count: {e}")
        return {"count": 0}

@app.post("/api/freelancer/bid")
async def place_freelancer_bid(
    bid_data: dict,
    email: str = Depends(verify_token)
):
    """Place a bid on a Freelancer project - simplified version that works like the extension"""
    try:
        print(f"🎯 Placing bid for user: {email}")
        print(f"🎯 Bid data: {bid_data}")
        
        project_id = bid_data.get("projectId")
        amount = bid_data.get("amount")
        message = bid_data.get("message", "")
        period = bid_data.get("period", 7)
        
        if not project_id or not amount:
            raise HTTPException(status_code=400, detail="projectId and amount are required")
        
        print(f"🎯 Project ID: {project_id}, Amount: {amount}, Period: {period}")
        
        # Try to get credentials from database, but don't fail if DB is down
        credentials = None
        freelancer_user_id = None
        access_token = None
        
        try:
            # Try database connection
            from database import SessionLocal
            from models import User, FreelancerCredentials
            
            db = SessionLocal()
            try:
                user = db.query(User).filter(User.email == email).first()
                if user:
                    credentials = db.query(FreelancerCredentials).filter(
                        FreelancerCredentials.user_id == user.id
                    ).first()
                    
                    if credentials:
                        freelancer_user_id = credentials.freelancer_user_id
                        access_token = credentials.access_token
                        print(f"🎯 Found credentials for user ID: {freelancer_user_id}")
            finally:
                db.close()
        except Exception as db_error:
            print(f"⚠️ Database connection failed, trying alternative approach: {db_error}")
            # Continue without database - we'll try to use environment variables or default values
        
        # If no credentials from DB, try environment variables or use defaults
        if not access_token:
            access_token = os.getenv("FREELANCER_ACCESS_TOKEN")
            if not access_token:
                raise HTTPException(
                    status_code=400, 
                    detail="No Freelancer credentials available. Please connect your Freelancer account or check server configuration."
                )
        
        if not freelancer_user_id:
            freelancer_user_id = os.getenv("FREELANCER_USER_ID", "0")  # Default to 0 if not available
        
        # Use the same API approach as the extension
        api_url = f"https://www.freelancer.com/api/projects/0.1/bids/"
        
        # Prepare JSON payload like the extension
        payload = {
            "project_id": int(project_id),
            "bidder_id": int(freelancer_user_id),
            "amount": float(amount),
            "period": int(period),
            "description": message,
            "milestone_percentage": 100
        }
        
        # Prepare headers like the extension
        headers = {
            "Authorization": f"Bearer {access_token}",
            "freelancer-oauth-v1": access_token,
            "Content-Type": "application/json"
        }
        
        print(f"🎯 API URL: {api_url}")
        print(f"🎯 Payload: {payload}")
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(api_url, headers=headers, json=payload)
            
            print(f"🎯 Response status: {response.status_code}")
            print(f"🎯 Response text: {response.text}")
            
            if response.status_code == 401:
                raise HTTPException(status_code=401, detail="Freelancer credentials expired. Please reconnect your account.")
            elif response.status_code == 403:
                raise HTTPException(status_code=403, detail="Access denied. You may not have permission to bid on this project.")
            elif response.status_code == 404:
                raise HTTPException(status_code=404, detail="Project not found or no longer available for bidding.")
            elif response.status_code == 405:
                raise HTTPException(status_code=405, detail="Method not allowed. The project may not accept bids or you may not be eligible to bid.")
            elif response.status_code != 200 and response.status_code != 201:
                try:
                    error_data = response.json()
                    error_message = error_data.get('message', response.text)
                except:
                    error_message = response.text
                raise HTTPException(status_code=response.status_code, detail=f"Freelancer API error: {error_message}")
            
            # Parse successful response
            try:
                result = response.json()
                print(f"🎯 Success response: {result}")
                return {"success": True, "message": "Bid placed successfully", "data": result}
            except:
                return {"success": True, "message": "Bid placed successfully"}
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error placing bid: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.delete("/api/freelancer/bids/{bid_id}/retract")
async def retract_freelancer_bid(
    bid_id: int,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Retract a bid on Freelancer"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, FreelancerCredentials
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        credentials = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if not credentials or not credentials.is_validated:
            raise HTTPException(status_code=400, detail="Not connected to Freelancer.com")
        
        # Prepare headers for Freelancer API
        headers = {"Content-Type": "application/json"}
        
        if credentials.access_token and credentials.access_token != "using_cookies":
            headers["Authorization"] = f"Bearer {credentials.access_token}"
            headers["freelancer-oauth-v1"] = credentials.access_token
        
        api_url = f"https://www.freelancer.com/api/projects/0.1/bids/{bid_id}/"
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(api_url, headers=headers)
            
            if response.status_code == 401:
                raise HTTPException(status_code=401, detail="Freelancer credentials expired. Please reconnect.")
            elif response.status_code not in [200, 204]:
                raise HTTPException(status_code=response.status_code, detail="Failed to retract bid")
            
            return {"success": True, "message": "Bid retracted successfully"}
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error retracting bid: {e}")
        raise HTTPException(status_code=500, detail="Failed to retract bid")

@app.get("/api/freelancer/settings")
async def get_freelancer_settings(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get freelancer automation settings"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, UserSettings
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
        
        # Return default settings if none exist
        if not settings:
            return {
                "settings": {
                    "autoBidEnabled": False,
                    "maxBidsPerDay": 10,
                    "minBudget": 50,
                    "maxBudget": 1000,
                    "bidMessage": "Hi! I'm interested in your project and have the skills needed to deliver high-quality results. Let's discuss the details!",
                    "autoReplyEnabled": False,
                    "autoReplyMessage": "Thank you for your message. I'll get back to you shortly!"
                }
            }
        
        return {
            "settings": {
                "autoBidEnabled": getattr(settings, 'auto_bid_enabled', False),
                "maxBidsPerDay": getattr(settings, 'max_bids_per_day', 10),
                "minBudget": getattr(settings, 'min_budget', 50),
                "maxBudget": getattr(settings, 'max_budget', 1000),
                "bidMessage": getattr(settings, 'bid_message', "Hi! I'm interested in your project and have the skills needed to deliver high-quality results. Let's discuss the details!"),
                "autoReplyEnabled": getattr(settings, 'auto_reply_enabled', False),
                "autoReplyMessage": getattr(settings, 'auto_reply_message', "Thank you for your message. I'll get back to you shortly!")
            }
        }
        
    except Exception as e:
        print(f"Error getting freelancer settings: {e}")
        raise HTTPException(status_code=500, detail="Failed to get settings")

@app.put("/api/freelancer/settings")
async def update_freelancer_settings(
    settings_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Update freelancer automation settings"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, UserSettings
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
        if not settings:
            settings = UserSettings(user_id=user.id)
            db.add(settings)
        
        # Update freelancer-specific settings
        freelancer_settings = settings_data.get("settings", {})
        
        # Note: These fields would need to be added to the UserSettings model
        # For now, we'll just return success
        
        settings.updated_at = datetime.utcnow()
        db.commit()
        
        return {"success": True, "message": "Settings updated successfully"}
        
    except Exception as e:
        print(f"Error updating freelancer settings: {e}")
        raise HTTPException(status_code=500, detail="Failed to update settings")

@app.delete("/api/freelancer/disconnect")
async def disconnect_freelancer(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Disconnect from Freelancer.com by removing credentials"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, FreelancerCredentials
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        credentials = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if credentials:
            db.delete(credentials)
            db.commit()
        
        return {"success": True, "message": "Disconnected from Freelancer.com"}
        
    except Exception as e:
        print(f"Error disconnecting from Freelancer: {e}")
        raise HTTPException(status_code=500, detail="Failed to disconnect")

@app.post("/api/freelancer/refresh-cache")
async def refresh_freelancer_cache(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Force refresh of Freelancer connection cache"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, FreelancerCredentials
        from datetime import datetime
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Reset validation timestamp to force recheck
        credentials = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if credentials:
            credentials.last_validated = None
            db.commit()
        
        return {"success": True, "message": "Cache refreshed"}
        
    except Exception as e:
        print(f"Error refreshing cache: {e}")
        raise HTTPException(status_code=500, detail="Failed to refresh cache")

# Helper function to prepare headers and cookies for Freelancer API calls
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

# Extension integration endpoints
@app.post("/api/freelancer/sync")
async def sync_freelancer_credentials(
    request_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Sync Freelancer credentials from extension to backend"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, FreelancerCredentials
        from datetime import datetime
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Extract data from extension format
        access_token = request_data.get("accessToken") or request_data.get("access_token")
        csrf_token = request_data.get("csrfToken") or request_data.get("csrf_token")
        user_id = request_data.get("userId") or request_data.get("user_id")
        auth_hash = request_data.get("authHash") or request_data.get("auth_hash")
        freelancer_cookies = request_data.get("freelancerCookies") or request_data.get("freelancer_cookies")
        validated_username = request_data.get("validatedUsername") or request_data.get("validated_username")
        validated_email = request_data.get("validatedEmail") or request_data.get("validated_email") or request_data.get("userEmail")
        is_validated = request_data.get("isValidated", True)
        
        print(f"🔄 Syncing Freelancer credentials for user {user.email}")
        print(f"📊 Raw request data: {request_data}")
        print(f"📊 Extracted data: hasToken={bool(access_token)}, hasUserId={bool(user_id)}, isValidated={is_validated}")
        print(f"📊 Token preview: {access_token[:20] if access_token else None}...")
        print(f"📊 Username: {validated_username}, Email: {validated_email}")
        
        # Get or create credentials record
        credentials = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if credentials:
            # Update existing credentials
            if access_token:
                credentials.access_token = access_token
            if csrf_token:
                credentials.csrf_token = csrf_token
            if user_id:
                credentials.freelancer_user_id = str(user_id)
            if auth_hash:
                credentials.auth_hash = auth_hash
            if freelancer_cookies:
                credentials.cookies = freelancer_cookies if isinstance(freelancer_cookies, dict) else json.loads(freelancer_cookies)
            if validated_username:
                credentials.validated_username = validated_username
            if validated_email:
                credentials.validated_email = validated_email
            
            credentials.is_validated = is_validated
            credentials.last_validated = datetime.utcnow() if is_validated else None
            credentials.updated_at = datetime.utcnow()
        else:
            # Create new credentials
            credentials = FreelancerCredentials(
                user_id=user.id,
                access_token=access_token,
                csrf_token=csrf_token,
                freelancer_user_id=str(user_id) if user_id else None,
                auth_hash=auth_hash,
                cookies=freelancer_cookies if isinstance(freelancer_cookies, dict) else (json.loads(freelancer_cookies) if freelancer_cookies else None),
                validated_username=validated_username,
                validated_email=validated_email,
                is_validated=is_validated,
                last_validated=datetime.utcnow() if is_validated else None
            )
            db.add(credentials)
        
        db.commit()
        db.refresh(credentials)
        
        print(f"✅ Successfully synced Freelancer credentials for user {user.email}")
        
        return {
            "success": True,
            "message": "Credentials synced successfully",
            "connected": is_validated
        }
        
    except Exception as e:
        print(f"❌ Error syncing Freelancer credentials: {e}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Failed to sync credentials")

@app.post("/api/user/info")
async def get_user_info_with_cookies(
    request_data: dict
):
    """Get user info using Freelancer cookies - for extension validation"""
    try:
        access_token = request_data.get("access_token")
        freelancer_cookies = request_data.get("freelancer_cookies")
        
        if not access_token and not freelancer_cookies:
            raise HTTPException(status_code=400, detail="access_token or freelancer_cookies required")
        
        # If we have an OAuth token, use it directly
        if access_token and access_token != "using_cookies":
            headers = {
                "Authorization": f"Bearer {access_token}",
                "freelancer-oauth-v1": access_token
            }
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    "https://www.freelancer.com/api/users/0.1/self?compact=true",
                    headers=headers
                )
                
                if response.status_code == 200:
                    data = response.json()
                    return {"success": True, "data": data}
        
        # If using cookies, we'd need to implement cookie-based requests
        # For now, return a mock response
        return {
            "success": False,
            "error": "Cookie-based authentication not implemented in backend"
        }
        
    except Exception as e:
        print(f"Error getting user info: {e}")
        return {"success": False, "error": str(e)}

@app.get("/api/freelancer/profile")
async def get_freelancer_profile(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get live Freelancer profile data using stored credentials - same as extension"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, FreelancerCredentials
        
        print(f"🔍 Looking for user with email: {email}")
        user = db.query(User).filter(User.email == email).first()
        if not user:
            print(f"❌ User not found with email: {email}")
            raise HTTPException(status_code=404, detail="User not found")
        
        print(f"✅ Found user: {user.email} (ID: {user.id})")
        
        # Get stored Freelancer credentials
        credentials = db.query(FreelancerCredentials).filter(
            FreelancerCredentials.user_id == user.id
        ).first()
        
        if not credentials:
            print(f"❌ No FreelancerCredentials found for user ID: {user.id}")
            raise HTTPException(status_code=404, detail="No Freelancer credentials found. Please connect using the browser extension first.")
        
        if not credentials.access_token:
            print(f"❌ No access_token in credentials for user ID: {user.id}")
            raise HTTPException(status_code=404, detail="No access token found. Please reconnect using the browser extension.")
        
        print(f"✅ Found credentials with access_token: {credentials.access_token[:20]}...")
        
        # Use the same approach as projects endpoint - cookies + OAuth fallback
        headers = {"Content-Type": "application/json"}
        cookies = {}
        
        # Use cookies if available (same as projects endpoint)
        if credentials.cookies:
            try:
                import json
                cookie_data = credentials.cookies if isinstance(credentials.cookies, dict) else json.loads(credentials.cookies)
                
                # Set up cookies for the request (same as projects endpoint)
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
                
                print(f"🍪 Using cookies for API calls: {list(cookies.keys())}")
            except Exception as e:
                print(f"⚠️ Error parsing cookies: {e}")
        
        # Also add OAuth token as fallback (same as projects endpoint)
        if credentials.access_token:
            headers.update({
                "Authorization": f"Bearer {credentials.access_token}",
                "freelancer-oauth-v1": credentials.access_token
            })
            print(f"🔑 Using OAuth token for API calls")
        
        print(f"🔄 Making Freelancer API call...")
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Try to get user data with avatar information
            response = await client.get(
                "https://www.freelancer.com/api/users/0.1/self?limit=1&jobs=true&webapp=1&compact=true&new_errors=true&new_pools=true&avatar=true&profile_logo_url=true",
                headers=headers,
                cookies=cookies
            )
            
            print(f"📡 Freelancer API response: {response.status_code}")
            
            if response.status_code == 401:
                print(f"❌ Freelancer API returned 401 - session expired")
                raise HTTPException(status_code=401, detail="Freelancer session expired - please reconnect using the extension")
            
            if response.status_code != 200:
                print(f"❌ Freelancer API error: {response.status_code}")
                raise HTTPException(status_code=response.status_code, detail=f"Freelancer API error: {response.status_code}")
            
            data = response.json()
            user_data = data.get("result")
            
            if not user_data:
                print(f"❌ No user data in Freelancer API response")
                raise HTTPException(status_code=500, detail="No user data received from Freelancer API")
            
            print(f"✅ Successfully got Freelancer profile for: {user_data.get('username')}")
            
            # Try to get avatar URL if not present
            if not any(key in user_data for key in ['avatar', 'avatar_url', 'profile_logo_url', 'logo_url']):
                try:
                    # Try to get avatar from users endpoint
                    avatar_response = await client.get(
                        f"https://www.freelancer.com/api/users/0.1/users/{user_data.get('id')}?avatar=true&profile_logo_url=true",
                        headers=headers,
                        cookies=cookies
                    )
                    if avatar_response.status_code == 200:
                        avatar_data = avatar_response.json()
                        if avatar_data.get('result') and avatar_data['result'].get('users'):
                            user_with_avatar = avatar_data['result']['users'][0]
                            # Merge avatar data into user_data
                            for key in ['avatar', 'avatar_url', 'profile_logo_url', 'logo_url']:
                                if key in user_with_avatar:
                                    user_data[key] = user_with_avatar[key]
                                    print(f"✅ Found avatar field: {key} = {user_with_avatar[key]}")
                except Exception as e:
                    print(f"⚠️ Could not fetch avatar data: {e}")
            
            return user_data
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error getting Freelancer profile: {e}")
        raise HTTPException(status_code=500, detail="Failed to get Freelancer profile")

@app.post("/api/bid/place")
async def place_bid_with_cookies(
    request_data: dict
):
    """Place a bid using extension credentials"""
    try:
        access_token = request_data.get("access_token")
        freelancer_cookies = request_data.get("freelancer_cookies")
        project_id = request_data.get("project_id")
        bidder_id = request_data.get("bidder_id")
        amount = request_data.get("amount")
        period = request_data.get("period", 7)
        description = request_data.get("description", "")
        
        if not project_id or not bidder_id or not amount:
            raise HTTPException(status_code=400, detail="project_id, bidder_id, and amount are required")
        
        # If we have an OAuth token, use it directly
        if access_token and access_token != "using_cookies":
            headers = {
                "Authorization": f"Bearer {access_token}",
                "freelancer-oauth-v1": access_token,
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            form_data = {
                "bidder_id": str(bidder_id),
                "amount": str(amount),
                "period": str(period),
                "description": description,
                "milestone_percentage": "100"
            }
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"https://www.freelancer.com/api/projects/0.1/projects/{project_id}/bids",
                    headers=headers,
                    data=form_data
                )
                
                if response.status_code == 200:
                    return {"success": True, "message": "Bid placed successfully"}
                else:
                    error_text = response.text
                    print(f"Freelancer API Error {response.status_code}: {error_text}")
                    
                    # Try to parse JSON error response to get the actual error message
                    try:
                        error_data = response.json()
                        if isinstance(error_data, dict):
                            # Look for common error message fields
                            actual_error = (
                                error_data.get('message') or 
                                error_data.get('error') or 
                                error_data.get('detail') or
                                error_data.get('error_description') or
                                str(error_data)
                            )
                            return {"success": False, "error": actual_error}
                    except:
                        pass
                    
                    # Fallback to raw error text
                    return {"success": False, "error": error_text or f"API Error {response.status_code}"}
        
        # Cookie-based bidding would be implemented here
        return {
            "success": False,
            "error": "Cookie-based bidding not implemented in backend"
        }
        
    except Exception as e:
        print(f"Error placing bid: {e}")
        return {"success": False, "error": str(e)}

@app.get("/api/freelancer/project/{project_id}")
async def get_freelancer_project_details(
    project_id: int,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get full project details from Freelancer API"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import FreelancerCredentials
        user = get_user_by_email(email, db)
        
        # Get user's Freelancer credentials
        credentials = db.query(FreelancerCredentials).filter(FreelancerCredentials.user_id == user.id).first()
        if not credentials:
            raise HTTPException(status_code=400, detail="Freelancer credentials not found")
        
        # Use the stored access token to fetch project details
        headers = {
            "Authorization": f"Bearer {credentials.access_token}",
            "freelancer-oauth-v1": credentials.access_token,
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"https://www.freelancer.com/api/projects/0.1/projects/{project_id}",
                headers=headers,
                params={
                    "full_description": "true",
                    "project_details": "true"
                }
            )
            
            if response.status_code == 200:
                project_data = response.json()
                project = project_data.get("result", {})
                
                return {
                    "id": project.get("id"),
                    "title": project.get("title"),
                    "description": project.get("description", ""),
                    "preview_description": project.get("preview_description", ""),
                    "budget": project.get("budget", {}),
                    "time_submitted": project.get("time_submitted"),
                    "jobs": project.get("jobs", []),
                    "owner_id": project.get("owner_id")
                }
            else:
                print(f"Failed to fetch project details: {response.status_code} - {response.text}")
                raise HTTPException(status_code=response.status_code, detail="Failed to fetch project details")
                
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching project details: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch project details")

@app.post("/api/freelancer/generate-proposal")
async def generate_freelancer_proposal(
    project_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Generate a proposal for a Freelancer project using n8n webhook"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        user = get_user_by_email(email, db)
        
        webhook_url = os.getenv("FREELANCER_PROPOSAL_WEBHOOK_URL")
        if not webhook_url:
            raise HTTPException(status_code=500, detail="FREELANCER_PROPOSAL_WEBHOOK_URL not configured in environment")
        
        # Prepare payload with user context and project data
        payload = {
            "user_id": user.id,
            "user_email": user.email,
            "project": {
                "id": project_data.get("id"),
                "title": project_data.get("title"),
                "description": project_data.get("description"),
                "preview_description": project_data.get("preview_description", ""),
                "url": project_data.get("url"),
                "budget": project_data.get("budget", {}),
                "posted_time": project_data.get("posted_time"),
                "bid_count": project_data.get("bid_count", 0),
                "skills": project_data.get("skills", []),
                "client": project_data.get("client"),
                "delivery_time": project_data.get("delivery_time")
            }
        }
        
        # Prepare headers with authentication
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv("N8N_WEBHOOK_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key
        
        print(f"Generating proposal for project {project_data.get('id')} for user {user.email}")
        print(f"Description length: {len(project_data.get('description', ''))} characters")
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                webhook_url,
                json=payload,
                headers=headers
            )
            
            print(f"Proposal generation webhook response status: {response.status_code}")
            print(f"Proposal generation webhook response: {response.text[:500] if response.text else 'empty'}")
            
            if response.status_code != 200:
                error_detail = "Unable to generate proposal. Please try again later."
                try:
                    error_json = response.json()
                    if error_json.get("message"):
                        error_detail = "Service temporarily unavailable. Please try again."
                except:
                    pass
                raise HTTPException(status_code=response.status_code, detail=error_detail)
            
            # Parse response
            try:
                response_data = response.json()
                return {
                    "success": True,
                    "message": "Proposal generated successfully",
                    "data": response_data
                }
            except Exception as e:
                print(f"Error parsing proposal response: {e}")
                return {
                    "success": True,
                    "message": "Proposal generation initiated",
                    "data": {"response": response.text}
                }
                
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error generating proposal: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to generate proposal")

@app.post("/api/message/send")
async def send_message_with_cookies(
    request_data: dict
):
    """Send a message using extension credentials"""
    try:
        access_token = request_data.get("access_token")
        freelancer_cookies = request_data.get("freelancer_cookies")
        thread_id = request_data.get("thread_id")
        message = request_data.get("message")
        
        if not thread_id or not message:
            raise HTTPException(status_code=400, detail="thread_id and message are required")
        
        # If we have an OAuth token, use it directly
        if access_token and access_token != "using_cookies":
            headers = {
                "Authorization": f"Bearer {access_token}",
                "freelancer-oauth-v1": access_token,
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            form_data = {
                "message": message,
                "source": "chat_box"
            }
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"https://www.freelancer.com/api/messages/0.1/threads/{thread_id}/messages",
                    headers=headers,
                    data=form_data
                )
                
                if response.status_code == 200:
                    return {"success": True, "message": "Message sent successfully"}
                else:
                    error_text = response.text
                    return {"success": False, "error": f"API Error {response.status_code}: {error_text}"}
        
        # Cookie-based messaging would be implemented here
        return {
            "success": False,
            "error": "Cookie-based messaging not implemented in backend"
        }
        
    except Exception as e:
        print(f"Error sending message: {e}")
        return {"success": False, "error": str(e)}