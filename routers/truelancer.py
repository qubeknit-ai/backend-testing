"""
Truelancer router
Handles credential storage (sent by the Chrome extension) and status checks.
Mirrors the pattern used in routers/guru.py and routers/users.py (Freelancer section).
"""

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional
from datetime import datetime, timedelta
import os

from database import SessionLocal
from models import User, Lead, BidHistory, TruelancerCredentials
from core.dependencies import get_db, get_user_by_email
from auth_utils import verify_token

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


# ── Bids ──────────────────────────────────────────────────────

@router.get("/api/truelancer/bids")
async def get_truelancer_bids(
    filter: str = "all",
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Return Truelancer bid history for the current user."""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        query = db.query(BidHistory).filter(BidHistory.user_id == user.id)

        if filter != "all":
            query = query.filter(func.lower(BidHistory.status) == filter.lower())

        bids = query.order_by(BidHistory.created_at.desc()).limit(100).all()
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
            ]
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to fetch Truelancer bids")


# ── Auto-bid stats & history ──────────────────────────────────

@router.get("/api/truelancer/autobid/stats")
async def get_truelancer_autobid_stats(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    if db is None:
        return {"bids_today": 0, "bids_week": 0, "success_week": 0, "is_running": False}
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return {"bids_today": 0, "bids_week": 0, "success_week": 0, "is_running": False}

        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())

        bids_today = db.query(BidHistory).filter(
            BidHistory.user_id == user.id,
            BidHistory.created_at >= today_start
        ).count()

        bids_week = db.query(BidHistory).filter(
            BidHistory.user_id == user.id,
            BidHistory.created_at >= week_start
        ).count()

        success_week = db.query(BidHistory).filter(
            BidHistory.user_id == user.id,
            BidHistory.created_at >= week_start,
            func.lower(BidHistory.status).in_(["success", "accepted", "awarded"])
        ).count()

        return {
            "bids_today": bids_today,
            "bids_week": bids_week,
            "success_week": success_week,
            "is_running": False,
        }
    except Exception:
        return {"bids_today": 0, "bids_week": 0, "success_week": 0, "is_running": False}


@router.post("/api/truelancer/autobid/start")
async def start_truelancer_autobid(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    return {"success": True, "message": "Truelancer auto-bid started"}


@router.post("/api/truelancer/autobid/stop")
async def stop_truelancer_autobid(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
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
