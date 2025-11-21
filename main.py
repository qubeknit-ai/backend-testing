from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
from datetime import datetime
import httpx
import os
from dotenv import load_dotenv
from schemas import UserSignup, UserLogin, Token, UserResponse, SettingsUpdate, SettingsResponse, UserProfileUpdate, TalentCreate, TalentUpdate, TalentResponse
from auth import get_password_hash, verify_password, create_access_token, verify_token

load_dotenv()

app = FastAPI()

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
    try:
        from database import SessionLocal
        db = SessionLocal()
    except Exception as e:
        print(f"Database connection failed: {e}")
        db = None
    
    try:
        yield db
    finally:
        if db is not None:
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        print(f"Triggering Upwork webhook for user {user.email}: {webhook_url}")
        
        if not webhook_url:
            raise HTTPException(status_code=500, detail="UPWORK_WEBHOOK_URL not configured in environment")
        
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
        
        # Add API key if configured
        api_key = os.getenv("N8N_WEBHOOK_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key
            print(f"Using API key authentication: {api_key[:10]}...")
        else:
            print("WARNING: N8N_WEBHOOK_API_KEY not set - webhook may fail authentication")
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                webhook_url,
                json=payload,
                headers=headers
            )
            print(f"Upwork webhook response status: {response.status_code}")
            print(f"Upwork webhook response: {response.text[:500] if response.text else 'empty'}")
            
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
            user.upwork_fetch_count += 1
            db.commit()
            
            remaining = daily_limit - user.upwork_fetch_count
            print(f"User {user.email} fetched Upwork. Count: {user.upwork_fetch_count}/{daily_limit}, Remaining: {remaining}")
            
            return {
                "success": True,
                "message": "Upwork jobs fetch triggered successfully",
                "status": response.status_code,
                "fetch_count": user.upwork_fetch_count,
                "daily_limit": daily_limit,
                "remaining": remaining
            }
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Load On server Plz try again Later")
    except Exception as e:
        print(f"Error triggering Upwork webhook: {str(e)}")
        import traceback
        traceback.print_exc()
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
        
        async with httpx.AsyncClient(timeout=60.0) as client:
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
        
        async with httpx.AsyncClient(timeout=180.0) as client:
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
async def get_leads(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    try:
        if db is None:
            return {"leads": []}
        
        from models import Lead
        user = get_user_by_email(email, db)
        
        # Debug: Print user info
        print(f"🔍 DEBUG - Current user: id={user.id}, email={user.email}")
        
        # Debug: Check all leads in database
        all_leads = db.query(Lead).all()
        print(f"🔍 DEBUG - Total leads in DB: {len(all_leads)}")
        for lead in all_leads[:5]:  # Print first 5
            print(f"   Lead id={lead.id}, user_id={lead.user_id}, title={lead.title[:50]}")
        
        # Get only this user's leads
        leads = db.query(Lead).filter(Lead.user_id == user.id).order_by(Lead.updated_at.desc()).all()
        print(f"🔍 DEBUG - Leads for user {user.id}: {len(leads)}")
        
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
            ]
        }
    except Exception as e:
        print(f"❌ Error fetching leads: {e}")
        import traceback
        traceback.print_exc()
        return {"leads": []}

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
        
        from models import Lead
        from datetime import datetime, timedelta
        user = get_user_by_email(email, db)
        
        # Get only this user's leads
        all_leads = db.query(Lead).filter(Lead.user_id == user.id).all()
        total_leads = len(all_leads)
        
        # Count by status
        ai_drafted = len([l for l in all_leads if l.status == "AI Drafted"])
        # Count approved using the new proposal_accepted field
        approved = len([l for l in all_leads if getattr(l, 'proposal_accepted', False) == True])
        
        # Count Unqualified leads (score < 7 or score is "—")
        low_score = 0
        for lead in all_leads:
            try:
                score_val = lead.score.strip() if lead.score else "—"
                if score_val == "—" or score_val == "":
                    continue
                if float(score_val) < 7:
                    low_score += 1
            except (ValueError, AttributeError):
                continue
        
        # Platform distribution
        platform_counts = {}
        for lead in all_leads:
            platform = lead.platform or "Unknown"
            platform_counts[platform] = platform_counts.get(platform, 0) + 1
        
        total_with_platform = sum(platform_counts.values())
        platform_distribution = [
            {
                "name": platform,
                "value": round((count / total_with_platform * 100), 1) if total_with_platform > 0 else 0,
                "count": count
            }
            for platform, count in platform_counts.items()
        ]
        
        # Timeline data (last 30 days)
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        recent_leads = [l for l in all_leads if l.created_at and l.created_at >= thirty_days_ago]
        
        # Group by date
        date_groups = {}
        for lead in recent_leads:
            date_key = lead.created_at.strftime("%b %d")
            if date_key not in date_groups:
                date_groups[date_key] = {"total": 0, "proposals": 0}
            date_groups[date_key]["total"] += 1
            # Count proposals sent (AI Drafted or Approved status)
            if lead.status in ["AI Drafted", "Approved"]:
                date_groups[date_key]["proposals"] += 1
        
        # Convert to list and sort
        timeline_data = [
            {"date": date, "total": data["total"], "proposals": data["proposals"]}
            for date, data in sorted(date_groups.items(), key=lambda x: datetime.strptime(x[0], "%b %d"))
        ]
        
        # If no data, provide sample structure
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
        
        # Update the status to Approved
        lead.status = "Approved"
        lead.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(lead)
        
        print(f"Approved lead {lead_id}")
        return {
            "success": True,
            "message": "Lead approved successfully",
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
        print(f"Error approving lead: {e}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@app.delete("/api/leads/clean")
async def clean_leads(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import Lead
        user = get_user_by_email(email, db)
        
        # Log before deletion
        total_leads_before = db.query(Lead).count()
        user_leads_before = db.query(Lead).filter(Lead.user_id == user.id).count()
        print(f"[CLEAN LEADS] User: {user.email} (ID: {user.id})")
        print(f"[CLEAN LEADS] Total leads in DB: {total_leads_before}")
        print(f"[CLEAN LEADS] User's leads: {user_leads_before}")
        
        # Delete only this user's leads
        deleted_count = db.query(Lead).filter(Lead.user_id == user.id).delete()
        db.commit()
        
        # Log after deletion
        total_leads_after = db.query(Lead).count()
        print(f"[CLEAN LEADS] Deleted: {deleted_count} leads")
        print(f"[CLEAN LEADS] Total leads remaining: {total_leads_after}")
        
        return {"success": True, "message": f"Deleted {deleted_count} leads for {user.email}", "count": deleted_count}
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
    return {"message": "FastAPI Proxy Server with PostgreSQL"}


@app.get("/api/health")
async def health_check():
    db_status = "disconnected"
    try:
        from database import engine
        with engine.connect() as conn:
            db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)[:100]}"
    
    return {
        "status": "running",
        "database": db_status
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
            upwork_auto_fetch=False,
            upwork_auto_fetch_interval=2,
            freelancer_job_category="Web Development",
            freelancer_max_jobs=3,
            freelancer_auto_fetch=False,
            freelancer_auto_fetch_interval=3
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
    if settings_data.upwork_auto_fetch is not None:
        settings.upwork_auto_fetch = settings_data.upwork_auto_fetch
    if settings_data.upwork_auto_fetch_interval is not None:
        settings.upwork_auto_fetch_interval = settings_data.upwork_auto_fetch_interval
    if settings_data.freelancer_job_category is not None:
        settings.freelancer_job_category = settings_data.freelancer_job_category
    if settings_data.freelancer_max_jobs is not None:
        settings.freelancer_max_jobs = settings_data.freelancer_max_jobs
    if settings_data.freelancer_auto_fetch is not None:
        settings.freelancer_auto_fetch = settings_data.freelancer_auto_fetch
    if settings_data.freelancer_auto_fetch_interval is not None:
        settings.freelancer_auto_fetch_interval = settings_data.freelancer_auto_fetch_interval
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
    
    user = get_user_by_email(email, db)
    return user

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
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get user's notifications, ordered by newest first"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import Notification
        user = get_user_by_email(email, db)
        
        notifications = db.query(Notification)\
            .filter(Notification.user_id == user.id)\
            .order_by(Notification.created_at.desc())\
            .limit(limit)\
            .all()
        
        return {
            "notifications": [
                {
                    "id": n.id,
                    "type": n.type,
                    "title": n.title,
                    "message": n.message,
                    "read": n.read,
                    "created_at": n.created_at.isoformat() if n.created_at else None
                }
                for n in notifications
            ]
        }
    except Exception as e:
        print(f"Error fetching notifications: {str(e)}")
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

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
