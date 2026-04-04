from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional
from datetime import datetime, timedelta
import os

from database import SessionLocal
from models import User, Lead, BidHistory, GuruCredentials
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
        if creds:
            if data.get("access_token"):
                creds.access_token = data["access_token"]
            if data.get("csrf_token"):
                creds.csrf_token = data["csrf_token"]
            if data.get("guru_user_id"):
                creds.guru_user_id = str(data["guru_user_id"])
            if data.get("validated_username"):
                creds.validated_username = data["validated_username"]
            if data.get("validated_email"):
                creds.validated_email = data["validated_email"]
            creds.is_validated = True
            creds.last_validated = datetime.utcnow()
            creds.updated_at = datetime.utcnow()
        else:
            creds = GuruCredentials(
                user_id=user.id,
                access_token=data.get("access_token"),
                csrf_token=data.get("csrf_token"),
                guru_user_id=str(data["guru_user_id"]) if data.get("guru_user_id") else None,
                validated_username=data.get("validated_username"),
                validated_email=data.get("validated_email"),
                is_validated=True,
                last_validated=datetime.utcnow()
            )
            db.add(creds)

        db.commit()
        return {"success": True, "message": "Guru credentials saved"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error saving Guru credentials: {e}")
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")


@router.get("/api/guru/status")
async def get_guru_status(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Check if user is connected to Guru.com"""
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
        if creds.validated_username:
            profile = {
                "name": creds.validated_username,
                "username": creds.validated_username,
                "email": creds.validated_email,
                "user_id": creds.guru_user_id
            }

        return {
            "connected": True,
            "profile": profile,
            "last_validated": creds.last_validated.isoformat() if creds.last_validated else None
        }
    except Exception as e:
        print(f"Error checking Guru status: {e}")
        return {"connected": False, "error": str(e)}


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
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")


# ── Projects ──────────────────────────────────────────────────

@router.get("/api/guru/projects")
async def get_guru_projects(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Return Guru jobs stored in the leads table"""
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
        print(f"Error fetching Guru projects: {e}")
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")


# ── Bids (quotes) ─────────────────────────────────────────────

@router.get("/api/guru/bids")
async def get_guru_bids(
    filter: str = "all",
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Return Guru bid/quote history"""
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

@router.get("/api/guru/autobid/stats")
async def get_guru_autobid_stats(
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
            "is_running": False
        }
    except Exception:
        return {"bids_today": 0, "bids_week": 0, "success_week": 0, "is_running": False}


@router.get("/api/guru/autobid/history")
async def get_guru_autobid_history(
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


@router.post("/api/guru/autobid/start")
async def start_guru_autobid(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    return {"success": True, "message": "Guru auto-quote started"}


@router.post("/api/guru/autobid/stop")
async def stop_guru_autobid(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    return {"success": True, "message": "Guru auto-quote stopped"}


# ── Settings ──────────────────────────────────────────────────

@router.get("/api/guru/settings")
async def get_guru_settings(
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
        return {
            "enabled": False,
            "daily_bids": 10,
            "frequency_minutes": 15,
            "min_skill_match": getattr(settings, "ai_agent_min_score", 1) if settings else 1,
            "smart_bidding": True,
            "employer_verified_only": False,
            "job_categories": []
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")


@router.post("/api/guru/settings")
async def save_guru_settings(
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

        settings.updated_at = datetime.utcnow()
        db.commit()
        return {"success": True, "message": "Guru settings saved"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")


# ── Fetch trigger ─────────────────────────────────────────────

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

        webhook_result = await trigger_webhook_async(webhook_url, payload, headers)
        return {"success": True, "message": "Guru jobs fetch initiated"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error triggering Guru webhook: {e}")
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")
