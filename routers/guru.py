"""
Guru router - Full feature parity with Truelancer.
Handles credential storage, live job fetching via stored cookies,
AI proposal generation (same webhook as Truelancer), and milestone-based bid submission.
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
from models import User, Lead, BidHistory, GuruCredentials, GuruAutoBidSettings
from schemas import GuruAutoBidSettings as GuruAutoBidSettingsSchema
from guru_autobid_service import guru_bidder
from core.dependencies import get_db, get_user_by_email
from auth_utils import verify_token

router = APIRouter()


# ── Credentials / Connection ──────────────────────────────────

@router.post("/api/guru/credentials")
async def save_guru_credentials(
    data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Save or update Guru credentials (sent by Chrome extension)"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        creds = db.query(GuruCredentials).filter(GuruCredentials.user_id == user.id).first()
        now = datetime.utcnow()
        if creds:
            if "access_token" in data: creds.access_token = data["access_token"]
            if "aspx_auth" in data: creds.aspx_auth = data["aspx_auth"]
            if "csrf_token" in data: creds.csrf_token = data["csrf_token"]
            if "cookies" in data: creds.cookies = data["cookies"]
            if "guru_user_id" in data: creds.guru_user_id = str(data["guru_user_id"])
            if "validated_username" in data: creds.validated_username = data["validated_username"]
            if "validated_email" in data: creds.validated_email = data["validated_email"]
            if "validated_picture_url" in data: creds.validated_picture_url = data["validated_picture_url"]
            creds.is_validated = True
            creds.last_validated = now
            creds.updated_at = now
        else:
            creds = GuruCredentials(
                user_id=user.id,
                access_token=data.get("access_token"),
                aspx_auth=data.get("aspx_auth"),
                csrf_token=data.get("csrf_token"),
                cookies=data.get("cookies"),
                guru_user_id=str(data["guru_user_id"]) if data.get("guru_user_id") else None,
                validated_username=data.get("validated_username"),
                validated_email=data.get("validated_email"),
                validated_picture_url=data.get("validated_picture_url"),
                is_validated=True,
                last_validated=now
            )
            db.add(creds)

        db.commit()
        print(f"✅ [GURU] Credentials saved for user {user.email}")
        return {"success": True, "message": "Guru credentials saved"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [GURU] Error saving credentials: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to save Guru credentials")


@router.get("/api/guru/status")
async def get_guru_status(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Check if user is connected to Guru.com — returns full profile like Truelancer"""
    if db is None:
        return {"connected": False, "error": "Database connection failed"}
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return {"connected": False}

        creds = db.query(GuruCredentials).filter(GuruCredentials.user_id == user.id).first()
        if not creds or not creds.is_validated:
            return {"connected": False, "message": "No Guru credentials found. Use the extension to connect."}

        profile = None
        if creds.validated_username or creds.validated_email:
            profile = {
                "name": creds.validated_username,
                "username": creds.validated_username,
                "email": creds.validated_email,
                "user_id": creds.guru_user_id,
                "picture_url": creds.validated_picture_url,
                "updated_at": creds.updated_at.isoformat() if creds.updated_at else None,
            }

        return {
            "connected": True,
            "profile": profile,
            "last_validated": creds.last_validated.isoformat() if creds.last_validated else None
        }
    except Exception as e:
        print(f"❌ [GURU] Error checking status: {e}")
        return {"connected": False, "error": str(e)}


@router.post("/api/guru/token-refresh")
async def refresh_all_guru_tokens(db: Session = Depends(get_db)):
    """
    Background refresh for Guru access tokens using refresh tokens.
    Can be called via a cron job or manual trigger to keep sessions alive.
    """
    creds_list = db.query(GuruCredentials).filter(GuruCredentials.is_validated == True).all()
    results = {"total": len(creds_list), "success": 0, "failed": 0, "details": []}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for creds in creds_list:
            try:
                # 1. Extract refresh token and client_id
                # Handle both string and dict formats for cookies
                cookies = creds.cookies
                if isinstance(cookies, str):
                    try:
                        cookies = json.loads(cookies)
                    except:
                        cookies = {}
                elif not isinstance(cookies, dict):
                    cookies = {}
                
                refresh_token = cookies.get("_refreshToken")
                # Default to known web client ID if missing
                client_id = cookies.get("_clientID") or "2324802" 
                
                if not refresh_token:
                    results["failed"] += 1
                    results["details"].append({"user_id": creds.user_id, "status": "skipped", "error": "No refresh token found"})
                    continue

                # 2. Call Guru token endpoint
                token_url = "https://www.guru.com/api/v1/oauth/token/access"
                payload = {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id
                }
                
                resp = await client.post(token_url, data=payload)
                
                if resp.status_code == 200:
                    data = resp.json()
                    new_access_token = data.get("access_token")
                    new_refresh_token = data.get("refresh_token")
                    
                    if new_access_token:
                        # 3. Update database fields
                        creds.access_token = new_access_token
                        
                        # Update the cookies JSON structure too
                        cookies["_accessToken"] = new_access_token
                        if new_refresh_token:
                            cookies["_refreshToken"] = new_refresh_token
                        
                        creds.cookies = cookies
                        creds.updated_at = datetime.utcnow()
                        creds.last_validated = datetime.utcnow()
                        
                        results["success"] += 1
                        results["details"].append({"user_id": creds.user_id, "status": "refreshed"})
                    else:
                        results["failed"] += 1
                        results["details"].append({"user_id": creds.user_id, "status": "invalid_response", "data": data})
                else:
                    results["failed"] += 1
                    results["details"].append({
                        "user_id": creds.user_id, 
                        "status": f"failed_{resp.status_code}", 
                        "error": resp.text[:200]
                    })
            
            except Exception as e:
                results["failed"] += 1
                results["details"].append({"user_id": creds.user_id, "status": "error", "error": str(e)})

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"❌ [GURU] Database commit error during token refresh: {e}")
        raise HTTPException(status_code=500, detail="Failed to save refreshed tokens")

    return results


@router.post("/api/guru/disconnect")
async def disconnect_guru(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Remove Guru credentials"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        creds = db.query(GuruCredentials).filter(GuruCredentials.user_id == user.id).first()
        if creds:
            db.delete(creds)
            db.commit()
        return {"success": True, "message": "Guru disconnected"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to disconnect Guru")


# ── Settings ──────────────────────────────────────────────────

@router.get("/api/guru/settings")
async def get_guru_settings(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get Guru auto-bid settings from database"""
    user = get_user_by_email(email, db)
    db_settings = db.query(GuruAutoBidSettings).filter(GuruAutoBidSettings.user_id == user.id).first()
    if not db_settings:
        db_settings = GuruAutoBidSettings(user_id=user.id)
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


@router.post("/api/guru/settings")
async def save_guru_settings(
    data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Save Guru auto-bid settings"""
    user = get_user_by_email(email, db)
    db_settings = db.query(GuruAutoBidSettings).filter(GuruAutoBidSettings.user_id == user.id).first()
    if not db_settings:
        db_settings = GuruAutoBidSettings(user_id=user.id)
        db.add(db_settings)

    if data.get("enabled") is not None: db_settings.enabled = data["enabled"]
    if data.get("daily_bids") is not None: db_settings.daily_bids = data["daily_bids"]
    if data.get("frequency_minutes") is not None: db_settings.frequency_minutes = data["frequency_minutes"]
    if data.get("smart_bidding") is not None: db_settings.smart_bidding = data["smart_bidding"]
    if data.get("skill_matching") is not None: db_settings.skill_matching = data["skill_matching"]
    if data.get("proposal_type") is not None: db_settings.proposal_type = data["proposal_type"]

    db_settings.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(db_settings)
    return {"success": True, "enabled": db_settings.enabled, "daily_bids": db_settings.daily_bids,
            "frequency_minutes": db_settings.frequency_minutes, "smart_bidding": db_settings.smart_bidding,
            "skill_matching": db_settings.skill_matching, "proposal_type": db_settings.proposal_type}


# ── Live Job Fetching ─────────────────────────────────────────

def _build_guru_cookie_header(cookies_dict: dict) -> str:
    """Convert cookies dict to Cookie header string."""
    if not cookies_dict:
        return ""
    return "; ".join(f"{k}={v}" for k, v in cookies_dict.items())


def _normalize_guru_job(job: dict) -> dict:
    """Normalize a Guru API job object to our standard format."""
    # Deep Nesting Fallbacks (matching popup.js logic)
    proj = job.get("Project") or job
    emp = proj.get("Employer") or job.get("Employer") or {}
    
    job_id = proj.get("ProjectID") or proj.get("JobId") or job.get("id") or job.get("jobId") or job.get("JobId") or ""
    title = proj.get("Title") or proj.get("JobTitle") or job.get("title") or job.get("Title") or "Untitled"
    description = proj.get("Description") or job.get("description") or job.get("Description") or job.get("snippet") or job.get("Snippet") or ""
    
    # Budget Parsing Helper
    def extract_numeric_budget(b_val):
        if not b_val: return 0
        if isinstance(b_val, (int, float)): return b_val
        if isinstance(b_val, dict):
            return b_val.get("max") or b_val.get("Max") or b_val.get("min") or b_val.get("Min") or 0
        
        # String parsing (e.g. "$10k-$25k", "500", "Not Sure")
        s = str(b_val).lower().replace(",", "").replace("$", "").strip()
        if "not sure" in s or "n/a" in s: return 0
        
        # Try to find numbers
        import re
        nums = re.findall(r'(\d+\.?\d*)([kmb]?)', s)
        if not nums: return 0
        
        # Take the last number (usually the max or the single value)
        val, multiplier = nums[-1]
        val = float(val)
        if multiplier == 'k': val *= 1000
        elif multiplier == 'm': val *= 1000000
        return val

    # Budget — Guru returns budget as object or string
    raw_budget = proj.get("BudgetAmountShortDescription") or proj.get("Budget") or job.get("budget") or job.get("Budget") or ""
    is_hourly = proj.get("IsHourly") is True or job.get("IsHourly") is True or \
                proj.get("PaymentType") == "Hourly" or job.get("PaymentType") == "Hourly" or \
                (isinstance(raw_budget, str) and "hourly" in raw_budget.lower())

    budget = extract_numeric_budget(raw_budget)
    
    if isinstance(raw_budget, dict):
        budget_min = raw_budget.get("min") or raw_budget.get("Min") or 0
        budget_max = raw_budget.get("max") or raw_budget.get("Max") or 0
        budget_str = f"${budget_min}-${budget_max}" if budget_min and budget_max else f"${budget_max or budget_min}"
    else:
        budget_str = str(raw_budget)
        if budget_str and not budget_str.startswith("$") and any(c.isdigit() for c in budget_str):
            budget_str = f"${budget_str}"
    
    if not budget_str or budget_str == "N/A" or budget_str == "Budget N/A" or "not sure" in budget_str.lower():
        b_min = proj.get("MinBudget") or job.get("MinBudget") or proj.get("BudgetFrom") or 0
        b_max = proj.get("MaxBudget") or job.get("MaxBudget") or proj.get("BudgetTo") or b_min
        if b_min > 0:
            budget_str = f"${b_min} - ${b_max}" if b_max > b_min else f"${b_min}"
            if is_hourly: budget_str += "/hr"
            budget = b_max or b_min
        else:
            budget_str = "Not Sure"
            budget = 0

    # Skills
    skills_raw = proj.get("Skills") or job.get("skills") or job.get("Skills") or job.get("categories") or []
    if isinstance(skills_raw, list):
        skills = [s.get("name") or s.get("title") or s.get("Name") or str(s) if isinstance(s, dict) else str(s) for s in skills_raw]
    else:
        skills = []

    # URL
    slug = proj.get("Slug") or job.get("slug") or job.get("Slug") or ""
    url = proj.get("url") or proj.get("URL") or job.get("url") or job.get("URL") or ""
    if not url and (job_id or slug):
        url = f"https://www.guru.com/d/jobs/id/{job_id}/" if job_id else f"https://www.guru.com/d/jobs/{slug}/"

    # Employer info
    employer_name = proj.get("EmployerName") or job.get("EmployerName") or emp.get("Name") or emp.get("displayName") or emp.get("fullName") or "Private Client"

    # Posted time - Robust conversion
    posted_at_raw = (
        proj.get("PostedDate") or 
        proj.get("DatePosted") or 
        job.get("postedDate") or 
        job.get("postDate") or 
        job.get("createdDate") or 
        job.get("datePosted") or 
        proj.get("DatePostedFormatted") or
        ""
    )
    posted_at = str(posted_at_raw)
    try:
        # Handle numeric timestamps (int, float, or string digits)
        val = None
        if isinstance(posted_at_raw, (int, float)):
            val = float(posted_at_raw)
        elif isinstance(posted_at_raw, str):
            if posted_at_raw.isdigit():
                val = float(posted_at_raw)
            elif "/Date(" in posted_at_raw:
                import re
                match = re.search(r'\/Date\((\d+)\)\/', posted_at_raw)
                if match:
                    val = float(match.group(1))
        
        if val:
            # If timestamp is very large, assume milliseconds
            if val > 10**11: val /= 1000.0
            dt = datetime.fromtimestamp(val)
            posted_at = dt.isoformat()
    except Exception as e:
        print(f"DEBUG DATE ERROR: {e} for {posted_at_raw}")
        pass

    # Proposals count - deep search with TotalApplied priority
    def find_quotes(obj):
        if not isinstance(obj, dict): return 0
        known = ["TotalApplied", "QuoteCount", "QuotesReceived", "QuotesCount", "NumberOfQuotes", "Proposals", "Quotes"]
        for k in known:
            val = obj.get(k)
            if val is not None:
                if isinstance(val, (int, float)): return int(val)
                if isinstance(val, str) and val.isdigit(): return int(val)
                if isinstance(val, dict):
                    res = val.get("count") or val.get("Count") or val.get("total") or 0
                    if res: return int(res)
        return 0

    proposals = find_quotes(proj) or find_quotes(job) or 0

    # Job type
    job_type = proj.get("JobType") or job.get("jobType") or job.get("type") or ("Hourly" if is_hourly else "Fixed")
    if isinstance(job_type, dict):
        job_type = job_type.get("name") or "Fixed"

    return {
        "id": str(job_id),
        "title": title,
        "description": description,
        "budget": budget,
        "budget_str": budget_str,
        "currency": "USD",
        "skills": skills,
        "url": url,
        "slug": slug,
        "posted_at": posted_at,
        "employer_name": employer_name,
        "total_proposals": proposals,
        "job_type": job_type,
        "status": proj.get("Status") or job.get("status") or "open",
    }


@router.get("/api/guru/recommended-jobs")
async def get_guru_recommended_jobs(
    page: int = 1,
    page_size: int = 20,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Fetch live jobs from Guru.com using stored cookies — mirrors Truelancer's recommended-jobs."""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    creds = db.query(GuruCredentials).filter(GuruCredentials.user_id == user.id).first()
    if not creds or not creds.is_validated:
        raise HTTPException(status_code=400, detail="Guru not connected")

    # Build cookie header from stored cookies dict
    cookies_dict = creds.cookies if isinstance(creds.cookies, dict) else {}
    if not cookies_dict or len(cookies_dict) < 2:
        raise HTTPException(
            status_code=400, 
            detail="Guru session cookies missing. Please open the AB BPO extension popup to sync your connection."
        )

    access_token = creds.access_token or cookies_dict.get("_accessToken", "")
    csrf_token = creds.csrf_token or cookies_dict.get("__RequestVerificationToken", "")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.guru.com/work/",
        "Origin": "https://www.guru.com",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if csrf_token:
        headers["RequestVerificationToken"] = csrf_token

    # Guru search endpoint — same one used in background.js fetchGuruJobs
    # query param is base64 of "/d/jobs/"
    search_url = f"https://www.guru.com/api/search/job/?query=L2Qvam9icy8%3D&pageNumber={page}&pageSize={page_size}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                search_url,
                headers=headers,
                cookies=cookies_dict
            )

            print(f"📡 [GURU] Jobs API status: {response.status_code}")

            if response.status_code == 401:
                creds.is_validated = False
                db.commit()
                raise HTTPException(status_code=401, detail="Guru session expired. Please reconnect via the extension.")

            if response.status_code != 200:
                print(f"❌ [GURU] API error body: {response.text[:300]}")
                raise HTTPException(status_code=502, detail=f"Guru API returned {response.status_code}")

            data = response.json()

            # Guru API response shape — try multiple known structures
            raw_jobs = (
                data.get("Data", {}).get("Results") or
                data.get("result") or
                data.get("results") or
                data.get("jobs") or
                data.get("data") or
                data.get("items") or
                []
            )
            
            if not raw_jobs and isinstance(data, dict):
                # Fallback: search for any list that might be jobs
                for key, value in data.items():
                    if isinstance(value, list) and len(value) > 0:
                        raw_jobs = value
                        print(f"🔍 [GURU] Found potential jobs list in key: {key}")
                        break

            if isinstance(raw_jobs, dict):
                raw_jobs = raw_jobs.get("data") or raw_jobs.get("items") or list(raw_jobs.values())

            projects = [_normalize_guru_job(j) for j in raw_jobs]
            print(f"✅ [GURU] Fetched {len(projects)} jobs from Guru API. Data keys: {list(data.keys())}")
            return {"projects": projects, "total": len(projects)}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [GURU] Error fetching recommended jobs: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch Guru jobs: {str(e)}")


# ── Generate Proposal ─────────────────────────────────────────

@router.post("/api/guru/generate-proposal")
async def generate_guru_proposal(
    project_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Generate AI proposal for a Guru project — same webhook format as Truelancer."""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        webhook_url = os.getenv("FREELANCER_PROPOSAL_WEBHOOK_URL")
        if not webhook_url:
            raise HTTPException(status_code=500, detail="FREELANCER_PROPOSAL_WEBHOOK_URL not configured")

        # Identical payload format to Truelancer generate-proposal
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
                "delivery_time": project_data.get("delivery_time"),
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
        print(f"❌ [GURU] Proposal generation error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ── Bid Submission ────────────────────────────────────────────

@router.post("/api/guru/bid")
async def place_guru_bid(
    bid_request: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Submit a milestone-based quote on Guru.com using stored cookies."""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    creds = db.query(GuruCredentials).filter(GuruCredentials.user_id == user.id).first()
    if not creds or not creds.is_validated:
        raise HTTPException(status_code=400, detail="Guru not connected")

    project_id = bid_request.get("project_id")
    amount = float(bid_request.get("amount", 0))
    description = bid_request.get("description", "")
    project_title = bid_request.get("project_title", "Guru Project")
    project_url = bid_request.get("project_url", f"https://www.guru.com/d/jobs/id/{project_id}/")

    if not project_id or not amount or not description:
        raise HTTPException(status_code=400, detail="Missing project_id, amount, or description")

    cookies_dict = creds.cookies if isinstance(creds.cookies, dict) else {}
    access_token = creds.access_token or cookies_dict.get("_accessToken", "")
    csrf_token = creds.csrf_token or cookies_dict.get("__RequestVerificationToken", "")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.guru.com/work/detail/{project_id}?apply=true",
        "Origin": "https://www.guru.com",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if csrf_token:
        headers["RequestVerificationToken"] = csrf_token

    # Milestone due date: 30 days from now (as confirmed by user)
    due_date = (datetime.utcnow() + timedelta(days=30)).strftime("%m/%d/%Y")

    quote_payload = {
        "Milestones": [{
            "MilestoneId": 0,
            "MilestoneName": "Project delivery",
            "Amount": amount,
            "DueDate": due_date
        }],
        "ScopeOfWork": description,
        "SafePayRequired": True,
        "AutopayDuration": 0,
        "StatusUpdateInterval": 0,
        "PrivateTransactions": True,
        "IsPremium": False,
        "ShareContactInformation": False,
        "DeletedMilestoneIds": [],
        "DeletedAttachmentIds": [],
        "Attachments": []
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://www.guru.com/api/v1/freelancer/jobs/{project_id}/quote/milestone",
                headers=headers,
                cookies=cookies_dict,
                json=quote_payload
            )

            print(f"📡 [GURU] Bid response: {response.status_code}")

            success = response.status_code in (200, 201)
            error_text = None
            if not success:
                error_text = response.text[:300]
                print(f"❌ [GURU] Bid failed: {error_text}")

            # Save to bid history regardless (track attempts)
            history = BidHistory(
                user_id=user.id,
                project_id=str(project_id),
                project_title=project_title,
                project_url=project_url,
                bid_amount=amount,
                proposal_text=description[:500],
                status="submitted" if success else "failed",
                error_message=error_text,
                platform="guru"
            )
            db.add(history)
            db.commit()

            if not success:
                return {"success": False, "error": error_text}

            return {"success": True, "message": "Quote submitted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [GURU] Bid error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to submit Guru quote: {str(e)}")


# ── Bids History ──────────────────────────────────────────────

@router.get("/api/guru/bids")
async def get_guru_bids(
    filter: str = "all",
    page: int = 1,
    per_page: int = 20,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Return Guru bid/quote history from BidHistory table"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        query = db.query(BidHistory).filter(
            BidHistory.user_id == user.id,
            BidHistory.platform == "guru"
        )

        if filter != "all":
            query = query.filter(func.lower(BidHistory.status) == filter.lower())

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
        print(f"❌ [GURU] Error fetching bids: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch Guru bids")


# ── Stats ─────────────────────────────────────────────────────

@router.get("/api/guru/autobid/stats")
async def get_guru_autobid_stats(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    if db is None:
        return {"bids_today": 0, "bids_week": 0, "success_week": 0, "failed_week": 0, "bid_amount_today": 0, "bid_amount_week": 0, "is_running": False}
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
        ).filter(BidHistory.user_id == user.id, BidHistory.platform == "guru").first()

        # Get settings status
        settings = db.query(GuruAutoBidSettings).filter(GuruAutoBidSettings.user_id == user.id).first()
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
        print(f"❌ [GURU] Error calculating stats: {e}")
        return {
            "bids_today": 0, "bids_week": 0, "success_week": 0, "failed_week": 0,
            "bid_amount_today": 0, "bid_amount_week": 0, "is_running": False
        }

@router.get("/api/guru/autobid/history")
async def get_guru_autobid_history(
    limit: int = 10,
    offset: int = 0,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get Guru bidding history with pagination"""
    user = get_user_by_email(email, db)
    
    total = db.query(BidHistory).filter(BidHistory.user_id == user.id, BidHistory.platform == "guru").count()
    history = db.query(BidHistory).filter(BidHistory.user_id == user.id, BidHistory.platform == "guru")\
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


@router.get("/api/guru/autobid/run-cycle")
async def run_guru_autobid_cycle():
    """Trigger a single bidding cycle for all enabled Guru users (for Cron jobs)"""
    try:
        results = await guru_bidder.run_cycle_batch()
        return {
            "success": True,
            "results": results
        }
    except Exception as e:
        print(f"Error running Guru autobid cycle: {e}")
        return {
            "success": False,
            "error": str(e)
        }


@router.get("/api/guru/autobid/settings")
async def get_guru_autobid_settings(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get Guru AutoBid settings"""
    user = get_user_by_email(email, db)
    settings = db.query(GuruAutoBidSettings).filter(GuruAutoBidSettings.user_id == user.id).first()
    if not settings:
        settings = GuruAutoBidSettings(user_id=user.id)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    
    return {
        "enabled": settings.enabled,
        "daily_bids": settings.daily_bids,
        "frequency_minutes": settings.frequency_minutes,
        "smart_bidding": settings.smart_bidding,
        "skill_matching": settings.skill_matching,
        "proposal_type": settings.proposal_type,
        "max_quotes": settings.max_quotes,
        "max_project_age_hours": settings.max_project_age_hours
    }


@router.post("/api/guru/autobid/settings")
async def update_guru_autobid_settings(
    new_settings: GuruAutoBidSettingsSchema,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Update Guru AutoBid settings"""
    user = get_user_by_email(email, db)
    settings = db.query(GuruAutoBidSettings).filter(GuruAutoBidSettings.user_id == user.id).first()
    if not settings:
        settings = GuruAutoBidSettings(user_id=user.id)
        db.add(settings)
    
    # Update fields
    if new_settings.enabled is not None: settings.enabled = new_settings.enabled
    if new_settings.daily_bids is not None: settings.daily_bids = new_settings.daily_bids
    if new_settings.frequency_minutes is not None: settings.frequency_minutes = new_settings.frequency_minutes
    if new_settings.smart_bidding is not None: settings.smart_bidding = new_settings.smart_bidding
    if new_settings.skill_matching is not None: settings.skill_matching = new_settings.skill_matching
    if new_settings.proposal_type is not None: settings.proposal_type = new_settings.proposal_type
    if new_settings.max_quotes is not None: settings.max_quotes = new_settings.max_quotes
    if new_settings.max_project_age_hours is not None: settings.max_project_age_hours = new_settings.max_project_age_hours
    
    db.commit()
    return {"success": True, "message": "Settings updated"}


@router.post("/api/guru/autobid/start")
async def start_guru_autobid(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    user = get_user_by_email(email, db)
    settings = db.query(GuruAutoBidSettings).filter(GuruAutoBidSettings.user_id == user.id).first()
    if not settings:
        settings = GuruAutoBidSettings(user_id=user.id, enabled=True)
        db.add(settings)
    else:
        settings.enabled = True
    db.commit()
    return {"success": True, "message": "Guru auto-bid started"}


@router.post("/api/guru/autobid/stop")
async def stop_guru_autobid(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    user = get_user_by_email(email, db)
    settings = db.query(GuruAutoBidSettings).filter(GuruAutoBidSettings.user_id == user.id).first()
    if settings:
        settings.enabled = False
        db.commit()
    return {"success": True, "message": "Guru auto-bid stopped"}


# ── Stored Projects (leads table) ─────────────────────────────

@router.get("/api/guru/projects")
async def get_guru_projects(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Return Guru jobs stored in the leads table (legacy)"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        leads = db.query(Lead).filter(
            Lead.user_id == user.id,
            Lead.platform == "guru",
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
        print(f"❌ [GURU] Error fetching projects: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch Guru projects")


# ── Fetch trigger (n8n webhook) ───────────────────────────────

@router.post("/api/fetch-guru")
async def fetch_guru(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Trigger Guru job scraping via n8n webhook"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        import httpx
        from core.utils import trigger_webhook_async

        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        webhook_url = os.getenv("GURU_WEBHOOK_URL")
        if not webhook_url:
            raise HTTPException(status_code=500, detail="GURU_WEBHOOK_URL not configured")

        payload = {"user_id": user.id, "user_email": user.email}
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv("N8N_WEBHOOK_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key

        await trigger_webhook_async(webhook_url, payload, headers)
        return {"success": True, "message": "Guru jobs fetch initiated"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [GURU] Error triggering webhook: {e}")
        raise HTTPException(status_code=500, detail="Failed to trigger Guru sync")
