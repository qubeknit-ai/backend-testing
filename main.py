from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
from datetime import datetime
import httpx
import os
from dotenv import load_dotenv
from schemas import UserSignup, UserLogin, Token, UserResponse, SettingsUpdate, SettingsResponse
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/sync-receive")
async def sync_receive(db: Session = Depends(get_db)):
    try:
        print(f"Fetching from: {os.getenv('N8N_RECEIVE_WEBHOOK_URL')}")
        
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
            
            # Save leads to database if connection is available
            if db is not None:
                try:
                    from models import Lead
                    for lead_data in leads_data:
                        # Skip if lead doesn't have essential data
                        title = lead_data.get("title", lead_data.get("titlle"))
                        platform = lead_data.get("platform")
                        
                        if not title or not platform:
                            print(f"Skipping lead with missing title or platform: {lead_data}")
                            continue
                        
                        # Check if lead already exists by title and platform
                        existing_lead = db.query(Lead).filter(
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
        raise HTTPException(status_code=504, detail="N8N webhook timeout")
    except Exception as e:
        print(f"Error in sync_receive: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/fetch-upwork")
async def fetch_upwork(db: Session = Depends(get_db)):
    try:
        webhook_url = os.getenv("UPWORK_WEBHOOK_URL")
        print(f"Triggering Upwork webhook: {webhook_url}")
        
        if not webhook_url:
            raise HTTPException(status_code=500, detail="UPWORK_WEBHOOK_URL not configured in environment")
        
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
                headers=headers
            )
            print(f"Upwork webhook response status: {response.status_code}")
            print(f"Upwork webhook response: {response.text[:500] if response.text else 'empty'}")
            
            # Check if response contains N8N workflow error
            if response.status_code != 200:
                error_detail = response.text
                try:
                    error_json = response.json()
                    if "Unused Respond to Webhook" in str(error_json):
                        error_detail = "N8N Workflow Error: Please remove or properly connect the 'Respond to Webhook' node in your Upwork workflow"
                    elif "not registered" in str(error_json):
                        error_detail = f"N8N Webhook Not Found: The webhook at '{webhook_url}' is not registered or the workflow is not active. Please check: 1) Workflow is activated (toggle ON), 2) Webhook path matches, 3) Workflow is saved"
                    elif error_json.get("message"):
                        error_detail = f"N8N Error: {error_json.get('message')}"
                except:
                    pass
                raise HTTPException(status_code=response.status_code, detail=error_detail)
            
            return {"success": True, "message": "Upwork jobs fetch triggered successfully", "status": response.status_code}
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upwork webhook timeout - workflow may still be processing")
    except Exception as e:
        print(f"Error triggering Upwork webhook: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to trigger Upwork webhook: {str(e)}")

@app.post("/api/fetch-freelancer")
async def fetch_freelancer(db: Session = Depends(get_db)):
    try:
        webhook_url = os.getenv("FREELANCER_WEBHOOK_URL")
        print(f"Triggering Freelancer webhook: {webhook_url}")
        
        if not webhook_url:
            raise HTTPException(status_code=500, detail="FREELANCER_WEBHOOK_URL not configured in environment")
        
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
                headers=headers
            )
            print(f"Freelancer webhook response status: {response.status_code}")
            print(f"Freelancer webhook response: {response.text[:500] if response.text else 'empty'}")
            
            # Check if response contains N8N workflow error
            if response.status_code != 200:
                error_detail = response.text
                try:
                    error_json = response.json()
                    if "Unused Respond to Webhook" in str(error_json):
                        error_detail = "N8N Workflow Error: Please remove or properly connect the 'Respond to Webhook' node in your Freelancer workflow"
                    elif "not registered" in str(error_json):
                        error_detail = f"N8N Webhook Not Found: The webhook at '{webhook_url}' is not registered or the workflow is not active. Please check: 1) Workflow is activated (toggle ON), 2) Webhook path matches, 3) Workflow is saved"
                    elif error_json.get("message"):
                        error_detail = f"N8N Error: {error_json.get('message')}"
                except:
                    pass
                raise HTTPException(status_code=response.status_code, detail=error_detail)
            
            return {"success": True, "message": "Freelancer jobs fetch triggered successfully", "status": response.status_code}
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Freelancer webhook timeout - workflow may still be processing")
    except Exception as e:
        print(f"Error triggering Freelancer webhook: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to trigger Freelancer webhook: {str(e)}")

@app.get("/api/leads")
async def get_leads(db: Session = Depends(get_db)):
    try:
        if db is None:
            return {"leads": []}
        
        from models import Lead
        # Order by updated_at to show most recently updated leads first
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
                    "Proposal": lead.proposal,
                    "url": lead.url,
                    "created_at": lead.created_at.isoformat() if lead.created_at else None,
                    "updated_at": lead.updated_at.isoformat() if lead.updated_at else None
                }
                for lead in leads
            ]
        }
    except Exception as e:
        print(f"Error fetching leads: {e}")
        return {"leads": []}

@app.get("/api/dashboard/pipeline")
async def get_pipeline_stats(db: Session = Depends(get_db)):
    """
    Get pipeline breakdown with lead counts and values per stage
    """
    try:
        if db is None:
            return {"pipeline": []}
        
        from models import Lead
        
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
        
        # Get all leads
        all_leads = db.query(Lead).all()
        
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
async def get_dashboard_stats(db: Session = Depends(get_db)):
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
        
        # Get all leads
        all_leads = db.query(Lead).all()
        total_leads = len(all_leads)
        
        # Count by status
        ai_drafted = len([l for l in all_leads if l.status == "AI Drafted"])
        approved = len([l for l in all_leads if l.status == "Approved"])
        
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
        
        # Find the lead
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        
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
        raise HTTPException(status_code=500, detail=str(e))

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
        
        # Find the lead
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        
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
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/leads/clean")
async def clean_leads(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import Lead
        
        # Delete all leads
        deleted_count = db.query(Lead).delete()
        db.commit()
        
        print(f"Deleted {deleted_count} leads")
        return {"success": True, "message": f"Deleted {deleted_count} leads", "count": deleted_count}
    except Exception as e:
        print(f"Error cleaning leads: {e}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

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

@app.get("/api/n8n/settings")
async def get_settings_for_n8n(db: Session = Depends(get_db)):
    """
    Endpoint for n8n to fetch global settings
    Usage: GET /api/n8n/settings
    """
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    from models import GlobalSettings
    
    # Get global settings (there should only be one record)
    settings = db.query(GlobalSettings).first()
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
    
    from models import GlobalSettings
    
    # Get or create global settings (there should only be one record)
    settings = db.query(GlobalSettings).first()
    if not settings:
        settings = GlobalSettings(
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
    
    from models import GlobalSettings
    
    # Get or create global settings (there should only be one record)
    settings = db.query(GlobalSettings).first()
    if not settings:
        settings = GlobalSettings()
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
    
    settings.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(settings)
    
    return settings


# Notification endpoints
@app.post("/api/notifications/webhook")
async def receive_notification_webhook(payload: dict, db: Session = Depends(get_db)):
    """
    Receive notifications from n8n webhook
    Expected payload: {
        "type": "success|info|warning|error",
        "title": "Notification Title",
        "message": "Notification message"
    }
    """
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import Notification
        
        notification = Notification(
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
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save notification: {str(e)}")

@app.get("/api/notifications")
async def get_notifications(
    limit: int = 50,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get all notifications, ordered by newest first"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import Notification
        
        notifications = db.query(Notification)\
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
        raise HTTPException(status_code=500, detail=f"Failed to fetch notifications: {str(e)}")

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
        
        notification = db.query(Notification).filter(Notification.id == notification_id).first()
        if not notification:
            raise HTTPException(status_code=404, detail="Notification not found")
        
        notification.read = True
        notification.updated_at = datetime.utcnow()
        db.commit()
        
        return {"success": True, "message": "Notification marked as read"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update notification: {str(e)}")

@app.put("/api/notifications/mark-all-read")
async def mark_all_notifications_read(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Mark all notifications as read"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import Notification
        
        db.query(Notification).update({"read": True, "updated_at": datetime.utcnow()})
        db.commit()
        
        return {"success": True, "message": "All notifications marked as read"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update notifications: {str(e)}")

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
        
        notification = db.query(Notification).filter(Notification.id == notification_id).first()
        if not notification:
            raise HTTPException(status_code=404, detail="Notification not found")
        
        db.delete(notification)
        db.commit()
        
        return {"success": True, "message": "Notification deleted"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete notification: {str(e)}")
