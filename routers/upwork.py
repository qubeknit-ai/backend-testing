from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional
from datetime import datetime, timedelta
import os

from database import SessionLocal
from models import User, Lead, BidHistory, UpworkCredentials
from core.dependencies import get_db, get_user_by_email
from auth_utils import get_password_hash, verify_password, create_access_token, verify_token, SECRET_KEY, ALGORITHM

router = APIRouter()


# ── Credentials / Connection ──────────────────────────────────

@router.post("/api/upwork/credentials")
async def save_upwork_credentials(
    data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Save or update Upwork OAuth token (sent by Chrome extension)"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        creds = db.query(UpworkCredentials).filter(UpworkCredentials.user_id == user.id).first()
        if creds:
            if data.get("access_token"):
                creds.access_token = data["access_token"]
            if data.get("oauth_token"):
                creds.oauth_token = data["oauth_token"]
            if data.get("upwork_user_id"):
                creds.upwork_user_id = str(data["upwork_user_id"])
            if data.get("validated_username"):
                creds.validated_username = data["validated_username"]
            if data.get("validated_email"):
                creds.validated_email = data["validated_email"]
            creds.is_validated = True
            creds.last_validated = datetime.utcnow()
            creds.updated_at = datetime.utcnow()
        else:
            creds = UpworkCredentials(
                user_id=user.id,
                access_token=data.get("access_token"),
                oauth_token=data.get("oauth_token"),
                upwork_user_id=str(data["upwork_user_id"]) if data.get("upwork_user_id") else None,
                validated_username=data.get("validated_username"),
                validated_email=data.get("validated_email"),
                is_validated=True,
                last_validated=datetime.utcnow()
            )
            db.add(creds)

        db.commit()
        return {"success": True, "message": "Upwork credentials saved"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error saving Upwork credentials: {e}")
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")


@router.get("/api/upwork/status")
async def get_upwork_status(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Check if user is connected to Upwork"""
    if db is None:
        return {"connected": False, "error": "Database connection failed"}
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return {"connected": False}

        creds = db.query(UpworkCredentials).filter(UpworkCredentials.user_id == user.id).first()
        if not creds or not creds.is_validated:
            return {"connected": False, "message": "No Upwork credentials found. Use the extension to connect."}

        profile = None
        if creds.validated_username:
            profile = {
                "name": creds.validated_username,
                "username": creds.validated_username,
                "email": creds.validated_email,
                "user_id": creds.upwork_user_id
            }

        return {
            "connected": True,
            "profile": profile,
            "last_validated": creds.last_validated.isoformat() if creds.last_validated else None
        }
    except Exception as e:
        print(f"Error checking Upwork status: {e}")
        return {"connected": False, "error": str(e)}


@router.post("/api/upwork/disconnect")
async def disconnect_upwork(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Remove Upwork credentials"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        creds = db.query(UpworkCredentials).filter(UpworkCredentials.user_id == user.id).first()
        if creds:
            db.delete(creds)
            db.commit()
        return {"success": True, "message": "Upwork disconnected"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")


# ── Projects (leads from DB filtered by platform) ─────────────

@router.get("/api/upwork/projects")
async def get_upwork_projects(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Return Upwork jobs stored in the leads table"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        leads = db.query(Lead).filter(
            Lead.user_id == user.id,
            Lead.platform == "upwork",
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
        print(f"Error fetching Upwork projects: {e}")
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")


# ── Bids ──────────────────────────────────────────────────────

@router.get("/api/upwork/bids")
async def get_upwork_bids(
    filter: str = "all",
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Return Upwork bid history"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        query = db.query(BidHistory).filter(
            BidHistory.user_id == user.id
        )

        # BidHistory doesn't have a platform column yet — filter by leads that are upwork
        # For now return all bids and let frontend sort; when platform col is added this query updates
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
                    "submitted_at": b.created_at.isoformat() if b.created_at else None
                }
                for b in bids
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")


# ── Auto-bid stats & history ──────────────────────────────────

@router.get("/api/upwork/autobid/stats")
async def get_upwork_autobid_stats(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    if db is None:
        return {"bids_today": 0, "bids_week": 0, "success_week": 0, "is_running": False, "connects_used": 0}
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
            "connects_used": 0
        }
    except Exception as e:
        return {"bids_today": 0, "bids_week": 0, "success_week": 0, "is_running": False}


@router.get("/api/upwork/autobid/history")
async def get_upwork_autobid_history(
    limit: int = 20,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    if db is None:
        return {"logs": []}
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return {"logs": []}

        logs = db.query(BidHistory).filter(
            BidHistory.user_id == user.id
        ).order_by(BidHistory.created_at.desc()).limit(limit).all()

        return {
            "logs": [
                {
                    "project_title": l.project_title,
                    "bid_amount": l.bid_amount,
                    "status": l.status,
                    "timestamp": l.created_at.isoformat() if l.created_at else None
                }
                for l in logs
            ]
        }
    except Exception:
        return {"logs": []}


@router.post("/api/upwork/autobid/start")
async def start_upwork_autobid(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    return {"success": True, "message": "Upwork auto-bid started"}


@router.post("/api/upwork/autobid/stop")
async def stop_upwork_autobid(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    return {"success": True, "message": "Upwork auto-bid stopped"}


# ── Settings ──────────────────────────────────────────────────

@router.get("/api/upwork/settings")
async def get_upwork_settings(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        from models import UserSettings
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
        if not settings:
            return {
                "enabled": False,
                "daily_bids": 10,
                "max_connects_per_day": 60,
                "frequency_minutes": 15,
                "min_skill_match": 1,
                "smart_bidding": True,
                "payment_verified_only": True,
                "job_categories": settings.upwork_job_categories if settings else ["Web Development"]
            }

        return {
            "enabled": False,
            "daily_bids": 10,
            "max_connects_per_day": getattr(settings, "ai_agent_max_connects_upwork", 60),
            "frequency_minutes": 15,
            "min_skill_match": getattr(settings, "ai_agent_min_score", 1),
            "smart_bidding": True,
            "payment_verified_only": settings.upwork_payment_verified,
            "job_categories": settings.upwork_job_categories or ["Web Development"]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")


@router.post("/api/upwork/settings")
async def save_upwork_settings(
    data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    try:
        from models import UserSettings
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
        if not settings:
            settings = UserSettings(user_id=user.id)
            db.add(settings)

        if data.get("job_categories") is not None:
            settings.upwork_job_categories = data["job_categories"]
        if data.get("max_connects_per_day") is not None:
            settings.ai_agent_max_connects_upwork = int(data["max_connects_per_day"])
        if data.get("payment_verified_only") is not None:
            settings.upwork_payment_verified = bool(data["payment_verified_only"])

        settings.updated_at = datetime.utcnow()
        db.commit()
        return {"success": True, "message": "Upwork settings saved"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")
