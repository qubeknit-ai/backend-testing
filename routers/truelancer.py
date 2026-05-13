"""
Truelancer router
Handles credential storage (sent by the Chrome extension) and status checks.
Mirrors the pattern used in routers/guru.py and routers/users.py (Freelancer section).
"""

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func, case
from typing import Optional
from datetime import datetime, timedelta
import os
import httpx
import json

from database import SessionLocal
from models import User, Lead, BidHistory, TruelancerCredentials, TruelancerAutoBidSettings
from schemas import TruelancerAutoBidSettings as TruelancerAutoBidSettingsSchema
from core.dependencies import get_db, get_user_by_email
from auth_utils import verify_token
from truelancer_autobid_service import truelancer_bidder

router = APIRouter()


# ── Credentials / Connection ──────────────────────────────────

@router.post("/api/truelancer/credentials")
async def save_truelancer_credentials(
    data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """
    Save or update Truelancer credentials.
    Called by the Chrome extension whenever it captures a fresh token / user data
    from Truelancer.com (via __NEXT_DATA__ or fetch-intercept).

    Expected payload fields (all optional – send whatever is available):
        access_token        : str   – Bearer JWT token
        truelancer_user_id  : str   – Truelancer internal user ID
        truelancer_email    : str   – Account email
        truelancer_fname    : str   – First name
        truelancer_lname    : str   – Last name
        truelancer_picture_url: str – Profile picture URL
        package_id          : int   – Subscription package ID
        currency            : str   – Default currency (e.g. "USD")
        cookies             : dict  – Full cookie dict captured from the browser
        validated_username  : str   – Display name / username for the UI
        validated_email     : str   – Confirmed email for the UI
    """
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        creds = db.query(TruelancerCredentials).filter(
            TruelancerCredentials.user_id == user.id
        ).first()

        now = datetime.utcnow()

        if creds:
            # Update only provided fields
            if data.get("access_token"):
                creds.access_token = data["access_token"]
            if data.get("truelancer_user_id") is not None:
                creds.truelancer_user_id = str(data["truelancer_user_id"])
            if data.get("truelancer_email"):
                creds.truelancer_email = data["truelancer_email"]
            if data.get("truelancer_fname"):
                creds.truelancer_fname = data["truelancer_fname"]
            if data.get("truelancer_lname"):
                creds.truelancer_lname = data["truelancer_lname"]
            if data.get("truelancer_picture_url"):
                creds.truelancer_picture_url = data["truelancer_picture_url"]
            if data.get("package_id") is not None:
                creds.package_id = data["package_id"]
            if data.get("currency"):
                creds.currency = data["currency"]
            if data.get("cookies") is not None:
                creds.cookies = data["cookies"]
            if data.get("validated_username"):
                creds.validated_username = data["validated_username"]
            if data.get("validated_email"):
                creds.validated_email = data["validated_email"]

            creds.is_validated = True
            creds.last_validated = now
            creds.updated_at = now
        else:
            creds = TruelancerCredentials(
                user_id=user.id,
                access_token=data.get("access_token"),
                truelancer_user_id=str(data["truelancer_user_id"]) if data.get("truelancer_user_id") else None,
                truelancer_email=data.get("truelancer_email"),
                truelancer_fname=data.get("truelancer_fname"),
                truelancer_lname=data.get("truelancer_lname"),
                truelancer_picture_url=data.get("truelancer_picture_url"),
                package_id=data.get("package_id"),
                currency=data.get("currency"),
                cookies=data.get("cookies"),
                validated_username=data.get("validated_username"),
                validated_email=data.get("validated_email"),
                is_validated=True,
                last_validated=now,
            )
            db.add(creds)

        db.commit()
        print(f"✅ [TRUELANCER] Credentials saved for user {user.email}")
        return {"success": True, "message": "Truelancer credentials saved"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [TRUELANCER] Error saving credentials: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to save Truelancer credentials")


@router.get("/api/truelancer/credentials")
async def get_truelancer_credentials(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Return stored Truelancer credentials for the current user (tokens redacted)."""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        creds = db.query(TruelancerCredentials).filter(
            TruelancerCredentials.user_id == user.id
        ).first()

        if not creds:
            raise HTTPException(status_code=404, detail="No Truelancer credentials found")

        return {
            "id": creds.id,
            "user_id": creds.user_id,
            "truelancer_user_id": creds.truelancer_user_id,
            "truelancer_email": creds.truelancer_email,
            "truelancer_fname": creds.truelancer_fname,
            "truelancer_lname": creds.truelancer_lname,
            "truelancer_picture_url": creds.truelancer_picture_url,
            "package_id": creds.package_id,
            "currency": creds.currency,
            "validated_username": creds.validated_username,
            "validated_email": creds.validated_email,
            "is_validated": creds.is_validated,
            "has_access_token": bool(creds.access_token),
            "has_cookies": bool(creds.cookies),
            "created_at": creds.created_at.isoformat() if creds.created_at else None,
            "updated_at": creds.updated_at.isoformat() if creds.updated_at else None,
            "last_validated": creds.last_validated.isoformat() if creds.last_validated else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [TRUELANCER] Error fetching credentials: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch Truelancer credentials")


@router.get("/api/truelancer/status")
async def get_truelancer_status(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Check if the user is connected to Truelancer.com."""
    if db is None:
        return {"connected": False, "error": "Database connection failed"}
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return {"connected": False}

        creds = db.query(TruelancerCredentials).filter(
            TruelancerCredentials.user_id == user.id
        ).first()

        if not creds or not creds.is_validated:
            return {
                "connected": False,
                "message": "No Truelancer credentials found. Use the browser extension to connect."
            }

        profile = None
        display_name = ""
        if creds.truelancer_fname or creds.truelancer_lname:
            display_name = f"{creds.truelancer_fname or ''} {creds.truelancer_lname or ''}".strip()
        elif creds.validated_username:
            display_name = creds.validated_username

        if display_name or creds.truelancer_email:
            profile = {
                "name": display_name or creds.validated_username,
                "username": creds.validated_username,
                "email": creds.truelancer_email or creds.validated_email,
                "user_id": creds.truelancer_user_id,
                "picture_url": creds.truelancer_picture_url,
                "truelancer_picture_url": creds.truelancer_picture_url,
                "package_id": creds.package_id,
                "currency": creds.currency,
            }

        return {
            "connected": True,
            "profile": profile,
            "last_validated": creds.last_validated.isoformat() if creds.last_validated else None,
        }

    except Exception as e:
        print(f"❌ [TRUELANCER] Error checking status: {e}")
        return {"connected": False, "error": str(e)}


@router.post("/api/truelancer/sync")
async def sync_truelancer_credentials(
    data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """
    Sync endpoint alias – identical to POST /api/truelancer/credentials.
    Kept separate so the extension can hit /sync without changing the existing
    /credentials endpoint semantics.
    """
    return await save_truelancer_credentials(data, email, db)


@router.post("/api/truelancer/disconnect")
async def disconnect_truelancer(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Remove Truelancer credentials for the current user."""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        creds = db.query(TruelancerCredentials).filter(
            TruelancerCredentials.user_id == user.id
        ).first()

        if creds:
            db.delete(creds)
            db.commit()

        return {"success": True, "message": "Truelancer disconnected"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [TRUELANCER] Error disconnecting: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to disconnect Truelancer")



# ── Settings ──────────────────────────────────────────────────

@router.get("/api/truelancer/settings")
async def get_truelancer_settings(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get current Truelancer AutoBidder settings from database"""
    user = get_user_by_email(email, db)
    
    # Get settings from database or create default
    db_settings = db.query(TruelancerAutoBidSettings).filter(TruelancerAutoBidSettings.user_id == user.id).first()
    if not db_settings:
        db_settings = TruelancerAutoBidSettings(user_id=user.id)
        db.add(db_settings)
        db.commit()
        db.refresh(db_settings)
    
    return {
        "enabled": db_settings.enabled,
        "daily_bids": db_settings.daily_bids,
        "frequency_minutes": db_settings.frequency_minutes,
        "smart_bidding": db_settings.smart_bidding,
        "skill_matching": db_settings.skill_matching,
        "proposal_type": db_settings.proposal_type
    }

@router.post("/api/truelancer/settings")
async def update_truelancer_settings(
    settings: TruelancerAutoBidSettingsSchema,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Update Truelancer AutoBidder settings in database"""
    user = get_user_by_email(email, db)
    
    # Get or create settings
    db_settings = db.query(TruelancerAutoBidSettings).filter(TruelancerAutoBidSettings.user_id == user.id).first()
    if not db_settings:
        db_settings = TruelancerAutoBidSettings(user_id=user.id)
        db.add(db_settings)
    
    # Update fields
    if settings.enabled is not None:
        db_settings.enabled = settings.enabled
    if settings.daily_bids is not None:
        db_settings.daily_bids = settings.daily_bids
    if settings.frequency_minutes is not None:
        db_settings.frequency_minutes = settings.frequency_minutes
    if settings.smart_bidding is not None:
        db_settings.smart_bidding = settings.smart_bidding
    if settings.skill_matching is not None:
        db_settings.skill_matching = settings.skill_matching
    if settings.proposal_type is not None:
        db_settings.proposal_type = settings.proposal_type
    
    db_settings.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(db_settings)
    
    return {
        "enabled": db_settings.enabled,
        "daily_bids": db_settings.daily_bids,
        "frequency_minutes": db_settings.frequency_minutes,
        "smart_bidding": db_settings.smart_bidding,
        "skill_matching": db_settings.skill_matching,
        "proposal_type": db_settings.proposal_type
    }

# ── Projects ──────────────────────────────────────────────────

@router.get("/api/truelancer/projects")
async def get_truelancer_projects(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Return Truelancer jobs stored in the leads table."""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        leads = db.query(Lead).filter(
            Lead.user_id == user.id,
            Lead.platform == "truelancer",
            Lead.visible == True
        ).order_by(Lead.created_at.desc()).limit(50).all()

        projects = [
            {
                "id": l.id,
                "title": l.title,
                "budget": l.budget,
                "description": l.description,
                "url": l.url,
                "skills": l.category,
                "posted_at": l.posted,
                "score": l.score,
                "status": l.status,
            }
            for l in leads
        ]
        return {"projects": projects, "total": len(projects)}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [TRUELANCER] Error fetching projects: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch Truelancer projects")


@router.get("/api/truelancer/recommended-jobs")
async def get_truelancer_recommended_jobs(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Fetch recommended jobs directly from Truelancer API using stored credentials."""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        creds = db.query(TruelancerCredentials).filter(
            TruelancerCredentials.user_id == user.id
        ).first()

        if not creds or not creds.access_token:
            raise HTTPException(status_code=400, detail="Truelancer not connected")

        # Fetch projects from Truelancer API
        # Extension uses POST https://api.truelancer.com/api/v1/projects with skill_matching: true
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.truelancer.com/api/v1/projects",
                headers={
                    "Authorization": f"Bearer {creds.access_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                },
                json={
                    "page": 1,
                    "per_page": 20,
                    "sort": "newest",
                    "skill_matching": True
                }
            )
            
            if response.status_code != 200:
                print(f"❌ [TRUELANCER] API error: {response.status_code} {response.text}")
                # If 401, maybe token expired
                if response.status_code == 401:
                    creds.is_validated = False
                    db.commit()
                    raise HTTPException(status_code=401, detail="Truelancer session expired. Please reconnect.")
                raise HTTPException(status_code=500, detail="Failed to fetch jobs from Truelancer")

            data = response.json()
            projects = data.get("projects", {}).get("data", []) or data.get("data", [])
            
            return {"projects": projects, "total": len(projects)}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [TRUELANCER] Error fetching recommended jobs: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/truelancer/generate-proposal")
async def generate_truelancer_proposal(
    project_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Generate a proposal for a Truelancer project using n8n webhook (same format as Freelancer)."""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        webhook_url = os.getenv("FREELANCER_PROPOSAL_WEBHOOK_URL")
        if not webhook_url:
            raise HTTPException(status_code=500, detail="FREELANCER_PROPOSAL_WEBHOOK_URL not configured")
        
        # Prepare payload identical to Freelancer
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
        
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv("N8N_WEBHOOK_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key
            
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(webhook_url, json=payload, headers=headers)
            
            if response.status_code != 200:
                raise HTTPException(status_code=500, detail="AI generation failed")
            
            return response.json()

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [TRUELANCER] Proposal generation error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ── Bids ──────────────────────────────────────────────────────

@router.get("/api/truelancer/bids")
async def get_truelancer_bids(
    status: str = "all",
    page: int = 1,
    per_page: int = 20,
    live: bool = False,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """
    Return Truelancer bid history.
    If live=True, fetches directly from Truelancer API.
    Otherwise, returns from local BidHistory table.
    """
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if live:
        try:
            creds = db.query(TruelancerCredentials).filter(TruelancerCredentials.user_id == user.id).first()
            if not creds or not creds.access_token:
                raise HTTPException(status_code=400, detail="Truelancer not connected")

            async with httpx.AsyncClient(timeout=30.0) as client:
                # Use stored cookies for authentication
                cookies = creds.cookies if isinstance(creds.cookies, dict) else {}
                
                # Try multiple candidate URLs
                candidates = [
                    {
                        "url": "https://api.truelancer.com/api/v1/user/proposalsent",
                        "method": "POST",
                        "json": {"filter": "proposals", "page": page}
                    },
                    {
                        "url": f"https://www.truelancer.com/api/v1/user/proposals-sent?filter=proposals&page={page}&per_page={per_page}",
                        "method": "GET"
                    },
                    {
                        "url": f"https://www.truelancer.com/api/v1/user/proposals-sent?page={page}&per_page={per_page}",
                        "method": "GET"
                    },
                    {
                        "url": f"https://api.truelancer.com/api/v1/proposals?page={page}&per_page={per_page}&status=sent",
                        "method": "GET"
                    }
                ]
                
                response = None
                last_error = None
                used_url = None
                
                for cand in candidates:
                    url = cand["url"]
                    method = cand["method"]
                    try:
                        print(f"📡 [TRUELANCER] Trying bids endpoint ({method}): {url}")
                        headers = {
                            "Authorization": f"Bearer {creds.access_token}",
                            "Accept": "application/json",
                            "X-Requested-With": "XMLHttpRequest"
                        }
                        
                        if method == "POST":
                            resp = await client.post(
                                url,
                                headers=headers,
                                json=cand.get("json"),
                                cookies=cookies,
                                timeout=10.0
                            )
                        else:
                            resp = await client.get(
                                url,
                                headers=headers,
                                cookies=cookies,
                                timeout=10.0
                            )

                        if resp.status_code == 200:
                            response = resp
                            used_url = url
                            break
                        else:
                            print(f"⚠️ [TRUELANCER] Endpoint {url} failed: {resp.status_code}")
                            last_error = f"Status {resp.status_code}"
                    except Exception as e:
                        print(f"⚠️ [TRUELANCER] Error trying {url}: {e}")
                        last_error = str(e)

                if not response:
                    print(f"❌ [TRUELANCER] All live bids endpoints failed. Last error: {last_error}")
                    raise HTTPException(status_code=500, detail=f"Truelancer API Error: All endpoints failed ({last_error})")

                print(f"✅ [TRUELANCER] Successfully fetched bids from: {used_url}")
                data = response.json()
                
                proposals_wrapper = data.get("proposals", {})
                if not proposals_wrapper and data.get("status") == 1:
                    # In some responses, 'proposals' might be missing if no data
                    proposals_wrapper = data
                
                live_bids = proposals_wrapper.get("data", data.get("data", []))
                total = proposals_wrapper.get("total", data.get("total", len(live_bids)))
                
                return {
                    "bids": [
                        {
                            "id": b.get("id"),
                            "project_title": (
                                b.get("job_title") or 
                                b.get("title") or 
                                b.get("job", {}).get("title") or 
                                b.get("type", {}).get("title") or 
                                "Untitled Project"
                            ),
                            "project_url": (
                                b.get("project_url") or 
                                (f"https://www.truelancer.com/freelance-project/{b.get('job_slug') or b.get('job', {}).get('slug') or b.get('type', {}).get('id')}")
                            ),
                            "bid_amount": b.get("total_amount") or b.get("amount") or b.get("price") or 0,
                            "budget": b.get("job_budget") or b.get("job", {}).get("budget") or "N/A",
                            "currency": b.get("job_currency") or b.get("currency") or "USD",
                            "status": (
                                b.get("status_text") or 
                                b.get("status") or 
                                b.get("proposalstatus", {}).get("displayvalue") or 
                                "Submitted"
                            ),
                            "submitted_at": b.get("created_at") or b.get("sentdate"),
                            "total_proposals": (
                                b.get("job_total_proposals") or 
                                b.get("job", {}).get("total_proposals") or 
                                b.get("proposalRank") or 
                                "N/A"
                            )
                        }
                        for b in live_bids
                    ],
                    "total": total,
                    "page": page,
                    "per_page": per_page
                }
        except Exception as e:
            print(f"❌ [TRUELANCER] Live bids fetch error: {e}")
            # Fallback to local bids if live fails
            pass

    try:
        query = db.query(BidHistory).filter(BidHistory.user_id == user.id)

        if status != "all":
            query = query.filter(func.lower(BidHistory.status) == status.lower())

        total = query.count()
        offset = (page - 1) * per_page
        bids = query.order_by(BidHistory.created_at.desc()).offset(offset).limit(per_page).all()
        
        return {
            "bids": [
                {
                    "id": b.id,
                    "project_title": b.project_title,
                    "project_url": b.project_url,
                    "bid_amount": b.bid_amount,
                    "proposal_text": b.proposal_text,
                    "status": b.status,
                    "submitted_at": b.created_at.isoformat() if b.created_at else None,
                }
                for b in bids
            ],
            "total": total,
            "page": page,
            "per_page": per_page
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [TRUELANCER] Error fetching bids: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch Truelancer bids")


@router.post("/api/truelancer/bid")
async def place_truelancer_bid(
    bid_request: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Place a bid on a Truelancer project."""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        creds = db.query(TruelancerCredentials).filter(
            TruelancerCredentials.user_id == user.id
        ).first()

        if not creds or not creds.access_token:
            raise HTTPException(status_code=400, detail="Truelancer not connected")

        project_id = bid_request.get("project_id")
        amount = bid_request.get("amount")
        description = bid_request.get("description")
        currency = bid_request.get("currency", creds.currency or "USD")

        if not project_id or not amount or not description:
            raise HTTPException(status_code=400, detail="Missing required bid data")

        # Prepare FormData payload for Truelancer
        payload = {
            'proposal[job_id]': str(project_id),
            'proposal[job_currency]': currency,
            'proposal[item]': '13',
            'proposal[details]': description,
            'proposal[total_amount]': str(amount),
            'proposal[deposit_amount]': str(bid_request.get("deposit_amount", amount)),
            'proposal[notify_freelancer]': '1'
        }

        # Build headers
        headers = {
            "Authorization": f"Bearer {creds.access_token}",
            "Accept": "application/json"
        }

        # Build cookies if available
        cookies = creds.cookies if isinstance(creds.cookies, dict) else {}

        # Submit to Truelancer
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Use 'files' parameter to force multipart/form-data which Truelancer requires
            files_payload = {k: (None, str(v)) for k, v in payload.items()}
            
            response = await client.post(
                "https://api.truelancer.com/api/v1/proposal/save",
                headers=headers,
                cookies=cookies,
                files=files_payload
            )
            
            if response.status_code != 200:
                print(f"❌ [TRUELANCER] Bid failed: {response.status_code} {response.text}")
                return {"success": False, "error": response.text}

            data = response.json()
            if data.get("status") != 1:
                return {"success": False, "error": data.get("message", "Truelancer error")}

            # Save to bid history
            history = BidHistory(
                user_id=user.id,
                project_id=str(project_id),
                project_title=bid_request.get("project_title", "Truelancer Project"),
                project_url=bid_request.get("project_url"),
                bid_amount=float(amount),
                proposal_text=description,
                status="success",
                platform="truelancer"
            )
            db.add(history)
            db.commit()

            return {"success": True, "message": "Bid placed successfully"}

    except Exception as e:
        print(f"❌ [TRUELANCER] Bid placement error: {e}")
        return {"success": False, "error": str(e)}


# ── Auto-bid stats & history ──────────────────────────────────

@router.get("/api/truelancer/autobid/run-cycle")
async def run_truelancer_autobid_cycle():
    """Trigger a single bidding cycle for all enabled Truelancer users (for Cron jobs)"""
    try:
        results = await truelancer_bidder.run_cycle_batch()
        return {
            "success": True,
            "results": results
        }
    except Exception as e:
        print(f"Error running Truelancer autobid cycle: {e}")
        return {
            "success": False,
            "error": str(e)
        }


@router.get("/api/truelancer/autobid/stats")
async def get_truelancer_autobid_stats(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    if db is None:
        return {"bids_today": 0, "bids_week": 0, "success_week": 0, "total_bids": 0, "success_rate": 0, "is_running": False}
    try:
        user = get_user_by_email(email, db)
        
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())

        # Get stats from BidHistory
        row = db.query(
            func.count(case((BidHistory.created_at >= today_start, 1), else_=None)).label('bids_today'),
            func.count(case((BidHistory.created_at >= week_start, 1), else_=None)).label('bids_week'),
            func.count(case(((BidHistory.created_at >= week_start) & func.lower(BidHistory.status).in_(['success', 'accepted', 'awarded']), 1), else_=None)).label('success_week'),
            func.count(case(((BidHistory.created_at >= week_start) & func.lower(BidHistory.status).in_(['failed', 'rejected', 'declined', 'error']), 1), else_=None)).label('failed_week'),
            func.coalesce(func.sum(case((BidHistory.created_at >= today_start, BidHistory.bid_amount), else_=None)), 0).label('amount_today'),
            func.coalesce(func.sum(case((BidHistory.created_at >= week_start, BidHistory.bid_amount), else_=None)), 0).label('amount_week'),
        ).filter(BidHistory.user_id == user.id, BidHistory.platform == "truelancer").first()

        # Get settings status
        settings = db.query(TruelancerAutoBidSettings).filter(TruelancerAutoBidSettings.user_id == user.id).first()
        is_running = settings.enabled if settings else False

        return {
            "bids_today": row.bids_today or 0,
            "bids_week": row.bids_week or 0,
            "success_week": row.success_week or 0,
            "failed_week": row.failed_week or 0,
            "bid_amount_today": float(row.amount_today or 0),
            "bid_amount_week": float(row.amount_week or 0),
            "is_running": is_running
        }
    except Exception as e:
        print(f"❌ [TRUELANCER] Error calculating stats: {e}")
        return {
            "bids_today": 0, "bids_week": 0, "success_week": 0, "failed_week": 0,
            "bid_amount_today": 0, "bid_amount_week": 0, "is_running": False
        }

@router.get("/api/truelancer/autobid/history")
async def get_truelancer_autobid_history(
    limit: int = 10,
    offset: int = 0,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get Truelancer bidding history with pagination"""
    user = get_user_by_email(email, db)
    
    total = db.query(BidHistory).filter(BidHistory.user_id == user.id, BidHistory.platform == "truelancer").count()
    history = db.query(BidHistory).filter(BidHistory.user_id == user.id, BidHistory.platform == "truelancer")\
        .order_by(BidHistory.created_at.desc())\
        .offset(offset)\
        .limit(limit)\
        .all()
    
    return {
        "total": total,
        "history": [
            {
                "id": h.id,
                "project_id": h.project_id,
                "project_title": h.project_title,
                "project_url": h.project_url,
                "bid_amount": h.bid_amount,
                "bid_time": h.created_at.isoformat() if h.created_at else None,
                "status": h.status,
                "error": h.error_message,
                "proposal_text": h.proposal_text
            }
            for h in history
        ]
    }


@router.post("/api/truelancer/autobid/start")
async def start_truelancer_autobid(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    user = get_user_by_email(email, db)
    settings = db.query(TruelancerAutoBidSettings).filter(TruelancerAutoBidSettings.user_id == user.id).first()
    if not settings:
        settings = TruelancerAutoBidSettings(user_id=user.id, enabled=True)
        db.add(settings)
    else:
        settings.enabled = True
    db.commit()
    return {"success": True, "message": "Truelancer auto-bid started"}


@router.post("/api/truelancer/autobid/stop")
async def stop_truelancer_autobid(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    user = get_user_by_email(email, db)
    settings = db.query(TruelancerAutoBidSettings).filter(TruelancerAutoBidSettings.user_id == user.id).first()
    if settings:
        settings.enabled = False
        db.commit()
    return {"success": True, "message": "Truelancer auto-bid stopped"}


# ── Debug ─────────────────────────────────────────────────────

@router.get("/api/truelancer/debug")
async def debug_truelancer_credentials(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Debug endpoint to inspect stored Truelancer credentials."""
    if db is None:
        return {"error": "Database connection failed"}
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return {"error": "User not found", "user_email": email}

        creds = db.query(TruelancerCredentials).filter(
            TruelancerCredentials.user_id == user.id
        ).first()

        if not creds:
            return {
                "user_email": email,
                "user_id": user.id,
                "has_credentials": False,
                "message": "No Truelancer credentials found",
            }

        return {
            "user_email": email,
            "user_id": user.id,
            "has_credentials": True,
            "credentials": {
                "id": creds.id,
                "truelancer_user_id": creds.truelancer_user_id,
                "truelancer_email": creds.truelancer_email,
                "validated_username": creds.validated_username,
                "is_validated": creds.is_validated,
                "has_access_token": bool(creds.access_token),
                "has_cookies": bool(creds.cookies),
                "package_id": creds.package_id,
                "currency": creds.currency,
                "created_at": creds.created_at.isoformat() if creds.created_at else None,
                "updated_at": creds.updated_at.isoformat() if creds.updated_at else None,
                "last_validated": creds.last_validated.isoformat() if creds.last_validated else None,
            },
        }

    except Exception as e:
        return {"error": str(e), "user_email": email}
