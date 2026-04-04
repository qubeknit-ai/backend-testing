from fastapi import APIRouter, HTTPException, Depends, status, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import func, text, Float, case
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
import httpx
import os
import re
import json
from urllib.parse import unquote
import time

from database import engine, SessionLocal
from models import *
from schemas import *
from core.dependencies import get_db, get_user_by_email, get_system_settings, check_and_reset_daily_limit, verify_admin, prepare_freelancer_request
from core.utils import extract_category_from_text, start_cache_cleanup, extract_category_from_url, init_db, trigger_webhook_async, _check_db_status
from auth_utils import get_password_hash, verify_password, create_access_token, verify_token, SECRET_KEY, ALGORITHM

router = APIRouter()

@router.get("/api/dashboard/pipeline")
async def get_pipeline_stats(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    """
    Get pipeline breakdown with lead counts and values per stage
    OPTIMIZED: Uses database aggregation instead of fetching all leads
    """
    try:
        if db is None:
            return {"pipeline": []}
        
        from models import Lead
        user = get_user_by_email(email, db)
        
        # Define pipeline stages in order
        stages = ["New", "Proposal Sent", "Approved", "Closed"]
        
        # OPTIMIZED: Use database aggregation with CASE statements
        # This is 10-50x faster than fetching all leads and processing in Python
        pipeline_query = db.query(
            case(
                (Lead.status.in_(["Pending"]), "New"),
                (Lead.status.in_(["AI Drafted", "Sent"]), "Proposal Sent"),
                (Lead.status == "Approved", "Approved"),
                (Lead.status.in_(["Closed", "Won", "Lost"]), "Closed"),
                else_="New"
            ).label("stage"),
            func.count(Lead.id).label("count")
        ).filter(
            Lead.user_id == user.id
        ).group_by("stage").all()
        
        # Convert to dictionary for easy lookup
        pipeline_data = {stage: {"count": 0, "value": 0} for stage in stages}
        for row in pipeline_query:
            if row.stage in pipeline_data:
                pipeline_data[row.stage]["count"] = row.count
        
        # Note: Budget value calculation removed for performance
        # If needed, can be added back with database-side calculation
        
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

@router.get("/api/dashboard/stats")
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

@router.post("/api/proposal/generate")
async def generate_proposal(
    proposal_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """
    Generate a proposal from job description using N8N webhook
    Expected payload: {
        "job_description": "job description text"
    }
    """
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        user = get_user_by_email(email, db)
        
        job_description = proposal_data.get("job_description", "")
        if not job_description.strip():
            raise HTTPException(status_code=400, detail="job_description is required")
        
        webhook_url = os.getenv("PROPOSAL_GENERATOR_WEBHOOK_URL")
        if not webhook_url:
            raise HTTPException(status_code=500, detail="PROPOSAL_GENERATOR_WEBHOOK_URL not configured")
        
        # Prepare payload for N8N with user_id
        payload = {
            "user_id": user.id,
            "user_email": user.email,
            "job_description": job_description.strip()
        }
        
        # Prepare headers with authentication
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv("N8N_WEBHOOK_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key
        
        print(f"Sending job description to N8N proposal generator")
        print(f"User ID: {user.id}, Email: {user.email}")
        print(f"Job description length: {len(job_description)} characters")
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                webhook_url,
                json=payload,
                headers=headers
            )
            
            print(f"Proposal generator webhook response status: {response.status_code}")
            print(f"Proposal generator webhook response: {response.text[:500]}")  # Log first 500 chars
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to generate proposal"
                )
            
            # Parse N8N response
            try:
                result = response.json()
                
                # Handle different response formats
                proposal_text = ""
                
                # Format 1: Array with output field [{"output": "..."}]
                if isinstance(result, list) and len(result) > 0:
                    first_item = result[0]
                    if isinstance(first_item, dict):
                        proposal_text = (
                            first_item.get("output") or 
                            first_item.get("proposal") or 
                            first_item.get("generated_proposal") or 
                            first_item.get("message") or 
                            ""
                        )
                    else:
                        proposal_text = str(first_item)
                
                # Format 2: Direct object {"output": "..."} or {"proposal": "..."}
                elif isinstance(result, dict):
                    proposal_text = (
                        result.get("output") or 
                        result.get("proposal") or 
                        result.get("generated_proposal") or 
                        result.get("message") or 
                        ""
                    )
                
                # Format 3: Plain string
                else:
                    proposal_text = str(result)
                
                if not proposal_text or not proposal_text.strip():
                    print(f"Warning: Empty proposal received. Raw response: {result}")
                    raise HTTPException(status_code=500, detail="No proposal received from generator")
                
                print(f"Successfully extracted proposal (length: {len(proposal_text)} chars)")
                
                return {
                    "success": True,
                    "proposal": proposal_text.strip()
                }
            except HTTPException:
                raise
            except Exception as e:
                print(f"Error parsing proposal response: {e}")
                print(f"Raw response: {response.text[:1000]}")  # Log first 1000 chars
                # Try to return raw response as fallback
                try:
                    return {
                        "success": True,
                        "proposal": response.text
                    }
                except:
                    raise HTTPException(status_code=500, detail=f"Failed to parse proposal response: {str(e)}")
                
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error generating proposal: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to generate proposal")

@router.get("/api/settings", response_model=SettingsResponse)
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

@router.put("/api/settings", response_model=SettingsResponse)
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

@router.get("/api/profile", response_model=UserResponse)
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

@router.put("/api/profile", response_model=UserResponse)
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

@router.post("/api/notifications/webhook")
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

@router.get("/api/notifications")
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

@router.put("/api/notifications/{notification_id}/read")
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

@router.put("/api/notifications/mark-all-read")
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

@router.delete("/api/notifications/{notification_id}")
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

@router.post("/api/talents", status_code=status.HTTP_201_CREATED)
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

@router.get("/api/talents")
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

@router.get("/api/talents/{talent_id}")
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

@router.put("/api/talents/{talent_id}")
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

@router.delete("/api/talents/{talent_id}")
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

@router.get("/api/freelancer/test")
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

@router.get("/api/freelancer/debug/skills")
async def debug_project_skills(
    email: str = Depends(verify_token),
    limit: int = 5
):
    """Debug endpoint to examine project skill extraction"""
    try:
        # Get user from database
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == email).first()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            
            # Use the debug function
            from autobid_service import bidder
            await bidder.debug_project_structure(user.id, limit)
            
            return {
                "status": "success",
                "message": f"Debug analysis complete for {limit} projects. Check server logs for detailed output.",
                "user_id": user.id
            }
            
        finally:
            db.close()
            
    except Exception as e:
        logger.error(f"Error in debug skills endpoint: {e}")
        return {"status": "error", "error": str(e)}

@router.get("/api/freelancer/test/skills")
async def test_skill_extraction(
    email: str = Depends(verify_token),
    skills: str = ""  # Empty by default to test no skill filter
):
    """Test skill extraction with specific skills or no skills"""
    try:
        # Get user from database
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == email).first()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            
            # Parse skills
            if skills.strip():
                selected_skills = [skill.strip() for skill in skills.split(",")]
            else:
                selected_skills = []  # Test with no skills
            
            # Use the test function
            from autobid_service import bidder
            await bidder.test_skill_extraction(user.id, selected_skills)
            
            return {
                "status": "success",
                "message": "Skill extraction test complete. Check server logs for detailed results.",
                "user_id": user.id,
                "tested_skills": selected_skills if selected_skills else "None (testing no skill filter)",
                "mode": "Specific skill matching" if selected_skills else "No skill filter (allow all projects)"
            }
            
        finally:
            db.close()
            
    except Exception as e:
        logger.error(f"Error in test skills endpoint: {e}")
        return {"status": "error", "error": str(e)}

@router.get("/api/freelancer/debug")
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

@router.post("/api/projects/list")
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

@router.post("/api/message/send")
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

@router.post("/api/freelancer/credentials", response_model=FreelancerCredentialsResponse)
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

@router.get("/api/freelancer/credentials", response_model=FreelancerCredentialsResponse)
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

@router.put("/api/freelancer/credentials", response_model=FreelancerCredentialsResponse)
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

@router.delete("/api/freelancer/credentials")
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

@router.get("/api/freelancer/status")
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

@router.get("/api/freelancer/projects")
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
                    
                    # Get selected skills from database instead of user profile
                    selected_skill_names = credentials.selected_skills or []
                    print(f"🎯 User selected skills from database: {selected_skill_names}")
                    
                    # If user has selected skills, find their IDs from the profile
                    if selected_skill_names and user_profile.get("jobs"):
                        profile_jobs = user_profile["jobs"]
                        # Match selected skill names with profile job IDs
                        for job in profile_jobs:
                            if job.get("name") in selected_skill_names:
                                user_skills.append(job["id"])
                        
                        print(f"✓ Matched skill IDs from profile: {user_skills}")
                        
                        # If no matches found in profile, we'll use all profile skills as fallback
                        if not user_skills and profile_jobs:
                            print("⚠️ No selected skills matched profile, using all profile skills as fallback")
                            user_skills = [job["id"] for job in profile_jobs]
                    elif user_profile.get("jobs"):
                        # No skills selected, use all profile skills
                        user_skills = [job["id"] for job in user_profile["jobs"]]
                        print(f"✓ Using all profile skills (no selection): {user_skills}")
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
            # Add explicit sorting to get NEWEST projects first (same as autobid service)
            search_url = f"https://www.freelancer.com/api/projects/0.1/projects/active/?compact=true&limit=20&user_details=true&jobs=true&sort_field=time_submitted&sort_order=desc&{skills_params}&languages[]=en"
            print(f"🎯 Searching projects with user skills: {user_skills}")
            print(f"🔗 Search URL: {search_url}")
        else:
            # Fallback to recommended projects if no skills found
            # Add explicit sorting to get NEWEST projects first (same as autobid service)
            search_url = "https://www.freelancer.com/api/projects/0.1/projects/active/?compact=true&limit=20&user_details=true&jobs=true&sort_field=time_submitted&sort_order=desc&user_recommended=true"
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
        
        # No additional sorting needed - already added sort_field=time_submitted&sort_order=desc above
        
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
                        users_url = f"https://www.freelancer.com/api/users/0.1/users/?{users_ids_param}&avatar=true&country_details=true&reputation=true&display_info=true&status=true&verification=true&qualification_details=true&membership_details=true"
                        
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

@router.get("/api/freelancer/projects/count")
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

@router.get("/api/freelancer/messages/threads")
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

@router.get("/api/freelancer/messages/count")
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

@router.get("/api/freelancer/messages/{thread_id}")
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

@router.post("/api/freelancer/messages/send")
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

@router.get("/api/freelancer/bids")
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

@router.get("/api/freelancer/bids/count")
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

@router.post("/api/freelancer/bid")
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

@router.delete("/api/freelancer/bids/{bid_id}/retract")
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

@router.get("/api/freelancer/settings")
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

@router.put("/api/freelancer/settings")
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

@router.get("/api/freelancer/skills")
async def get_freelancer_skills(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get user's selected freelancer skills"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, FreelancerCredentials
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        credentials = db.query(FreelancerCredentials).filter(FreelancerCredentials.user_id == user.id).first()
        
        if not credentials:
            return {"selected_skills": []}
        
        return {"selected_skills": credentials.selected_skills or []}
        
    except Exception as e:
        print(f"Error getting freelancer skills: {e}")
        raise HTTPException(status_code=500, detail="Failed to get skills")

@router.put("/api/freelancer/skills")
async def update_freelancer_skills(
    skills_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Update user's selected freelancer skills"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, FreelancerCredentials
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        credentials = db.query(FreelancerCredentials).filter(FreelancerCredentials.user_id == user.id).first()
        
        if not credentials:
            # Create new credentials record if it doesn't exist
            credentials = FreelancerCredentials(
                user_id=user.id,
                selected_skills=skills_data.get("selected_skills", [])
            )
            db.add(credentials)
        else:
            # Update existing credentials
            credentials.selected_skills = skills_data.get("selected_skills", [])
            credentials.updated_at = datetime.utcnow()
        
        db.commit()
        
        return {
            "success": True, 
            "message": "Skills updated successfully",
            "selected_skills": credentials.selected_skills
        }
        
    except Exception as e:
        print(f"Error updating freelancer skills: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update skills")

@router.get("/api/freelancer/available-skills")
async def get_available_freelancer_skills(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get list of available skills from user's Freelancer profile"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        from models import User, FreelancerCredentials
        import json
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        credentials = db.query(FreelancerCredentials).filter(FreelancerCredentials.user_id == user.id).first()
        
        if not credentials or not credentials.is_validated:
            # Return common freelancer skills as fallback
            common_skills = [
                "PHP", "JavaScript", "HTML", "CSS", "WordPress", "Python", "React", "Node.js",
                "MySQL", "Laravel", "Vue.js", "Angular", "Bootstrap", "jQuery", "AJAX",
                "Web Development", "Mobile App Development", "Android", "iOS", "Flutter",
                "Graphic Design", "Logo Design", "Photoshop", "Illustrator", "UI/UX Design",
                "Content Writing", "Copywriting", "SEO", "Digital Marketing", "Social Media Marketing",
                "Data Entry", "Excel", "Virtual Assistant", "Customer Service", "Translation",
                "Video Editing", "Animation", "3D Modeling", "Game Development", "Unity",
                "Machine Learning", "Data Science", "Artificial Intelligence", "Blockchain",
                "DevOps", "AWS", "Docker", "Linux", "System Administration"
            ]
            return {"available_skills": sorted(common_skills)}
        
        # Fetch user's actual skills from Freelancer profile (same as extension)
        try:
            # Prepare headers and cookies for Freelancer API
            headers = {"Content-Type": "application/json"}
            cookies = {}
            
            # Use cookies if available (same logic as projects endpoint)
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
                    
                    print(f"🍪 Using cookies for skills API call: {list(cookies.keys())}")
                except Exception as e:
                    print(f"⚠️ Error parsing cookies: {e}")
            
            # Fallback to OAuth token if no cookies or as backup
            if credentials.access_token and credentials.access_token != "using_cookies":
                headers["Authorization"] = f"Bearer {credentials.access_token}"
                headers["freelancer-oauth-v1"] = credentials.access_token
                print("🔑 Using OAuth token for skills API call")
            
            # Use the exact same API call as the extension
            profile_url = "https://www.freelancer.com/api/users/0.1/self?limit=1&jobs=true&webapp=1&compact=true&new_errors=true&new_pools=true"
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(profile_url, headers=headers, cookies=cookies)
                
                if response.status_code == 200:
                    data = response.json()
                    user_profile = data.get("result", {})
                    
                    # Extract skills from jobs array (same as extension)
                    if user_profile.get("jobs") and len(user_profile["jobs"]) > 0:
                        skills = [job["name"] for job in user_profile["jobs"]]
                        print(f"✅ Fetched {len(skills)} skills from user profile: {skills}")
                        return {"available_skills": sorted(skills)}
                    else:
                        print("ℹ️ No skills found in user profile")
                        # Return common skills as fallback
                        common_skills = [
                            "PHP", "JavaScript", "HTML", "CSS", "WordPress", "Python", "React", "Node.js",
                            "MySQL", "Laravel", "Vue.js", "Angular", "Bootstrap", "jQuery", "AJAX",
                            "Web Development", "Mobile App Development", "Android", "iOS", "Flutter",
                            "Graphic Design", "Logo Design", "Photoshop", "Illustrator", "UI/UX Design",
                            "Content Writing", "Copywriting", "SEO", "Digital Marketing", "Social Media Marketing",
                            "Data Entry", "Excel", "Virtual Assistant", "Customer Service", "Translation",
                            "Video Editing", "Animation", "3D Modeling", "Game Development", "Unity",
                            "Machine Learning", "Data Science", "Artificial Intelligence", "Blockchain",
                            "DevOps", "AWS", "Docker", "Linux", "System Administration"
                        ]
                        return {"available_skills": sorted(common_skills)}
                elif response.status_code == 401:
                    raise HTTPException(status_code=401, detail="Freelancer credentials expired. Please reconnect.")
                else:
                    print(f"⚠️ Could not get user profile: {response.status_code}")
                    # Return common skills as fallback
                    common_skills = [
                        "PHP", "JavaScript", "HTML", "CSS", "WordPress", "Python", "React", "Node.js",
                        "MySQL", "Laravel", "Vue.js", "Angular", "Bootstrap", "jQuery", "AJAX",
                        "Web Development", "Mobile App Development", "Android", "iOS", "Flutter",
                        "Graphic Design", "Logo Design", "Photoshop", "Illustrator", "UI/UX Design",
                        "Content Writing", "Copywriting", "SEO", "Digital Marketing", "Social Media Marketing",
                        "Data Entry", "Excel", "Virtual Assistant", "Customer Service", "Translation",
                        "Video Editing", "Animation", "3D Modeling", "Game Development", "Unity",
                        "Machine Learning", "Data Science", "Artificial Intelligence", "Blockchain",
                        "DevOps", "AWS", "Docker", "Linux", "System Administration"
                    ]
                    return {"available_skills": sorted(common_skills)}
                    
        except Exception as e:
            print(f"⚠️ Error fetching skills from Freelancer profile: {e}")
            # Return common skills as fallback
            common_skills = [
                "PHP", "JavaScript", "HTML", "CSS", "WordPress", "Python", "React", "Node.js",
                "MySQL", "Laravel", "Vue.js", "Angular", "Bootstrap", "jQuery", "AJAX",
                "Web Development", "Mobile App Development", "Android", "iOS", "Flutter",
                "Graphic Design", "Logo Design", "Photoshop", "Illustrator", "UI/UX Design",
                "Content Writing", "Copywriting", "SEO", "Digital Marketing", "Social Media Marketing",
                "Data Entry", "Excel", "Virtual Assistant", "Customer Service", "Translation",
                "Video Editing", "Animation", "3D Modeling", "Game Development", "Unity",
                "Machine Learning", "Data Science", "Artificial Intelligence", "Blockchain",
                "DevOps", "AWS", "Docker", "Linux", "System Administration"
            ]
            return {"available_skills": sorted(common_skills)}
        
    except Exception as e:
        print(f"Error getting available freelancer skills: {e}")
        raise HTTPException(status_code=500, detail="Failed to get available skills")

@router.delete("/api/freelancer/disconnect")
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

@router.post("/api/freelancer/refresh-cache")
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

@router.post("/api/freelancer/sync")
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

@router.get("/api/freelancer/profile")
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

@router.get("/api/freelancer/project/{project_id}")
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

@router.post("/api/freelancer/generate-proposal")
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

@router.post("/api/message/send")
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

@router.get("/api/crm/deals")
async def get_closed_deals(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0)
):
    """Get all closed deals for current user"""
    from models import ClosedDeal
    user = get_user_by_email(email, db)
    
    # Get total count
    total = db.query(ClosedDeal).filter(ClosedDeal.user_id == user.id).count()
    
    # Get paginated deals
    deals = db.query(ClosedDeal).filter(
        ClosedDeal.user_id == user.id
    ).order_by(
        ClosedDeal.closed_date.desc()
    ).offset(offset).limit(limit).all()
    
    return {
        "deals": [
            {
                "id": d.id,
                "bid_history_id": d.bid_history_id,
                "project_title": d.project_title,
                "project_url": d.project_url,
                "platform": d.platform,
                "client_payment": d.client_payment,
                "outsource_cost": d.outsource_cost,
                "platform_fee": d.platform_fee,
                "profit": d.profit,
                "status": d.status,
                "closed_date": d.closed_date.isoformat(),
                "completion_date": d.completion_date.isoformat() if d.completion_date else None,
                "created_at": d.created_at.isoformat(),
                "updated_at": d.updated_at.isoformat()
            }
            for d in deals
        ],
        "total": total,
        "limit": limit,
        "offset": offset
    }

@router.post("/api/crm/deals")
async def create_closed_deal(
    deal_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Create a new closed deal"""
    from models import ClosedDeal
    user = get_user_by_email(email, db)
    
    # Calculate profit
    client_payment = float(deal_data.get("client_payment", 0))
    outsource_cost = float(deal_data.get("outsource_cost", 0))
    platform_fee = float(deal_data.get("platform_fee", 0))
    profit = client_payment - outsource_cost - platform_fee
    
    new_deal = ClosedDeal(
        user_id=user.id,
        bid_history_id=deal_data.get("bid_history_id"),
        project_title=deal_data.get("project_title"),
        project_url=deal_data.get("project_url"),
        platform=deal_data.get("platform", "Unknown"),
        client_payment=client_payment,
        outsource_cost=outsource_cost,
        platform_fee=platform_fee,
        profit=profit,
        status=deal_data.get("status", "active")
    )
    
    db.add(new_deal)
    db.commit()
    db.refresh(new_deal)
    
    return {
        "success": True,
        "deal": {
            "id": new_deal.id,
            "bid_history_id": new_deal.bid_history_id,
            "project_title": new_deal.project_title,
            "project_url": new_deal.project_url,
            "platform": new_deal.platform,
            "client_payment": new_deal.client_payment,
            "outsource_cost": new_deal.outsource_cost,
            "platform_fee": new_deal.platform_fee,
            "profit": new_deal.profit,
            "status": new_deal.status,
            "closed_date": new_deal.closed_date.isoformat(),
            "completion_date": new_deal.completion_date.isoformat() if new_deal.completion_date else None
        }
    }

@router.put("/api/crm/deals/{deal_id}")
async def update_closed_deal(
    deal_id: int,
    deal_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Update an existing closed deal"""
    from models import ClosedDeal
    user = get_user_by_email(email, db)
    
    deal = db.query(ClosedDeal).filter(
        ClosedDeal.id == deal_id,
        ClosedDeal.user_id == user.id
    ).first()
    
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    
    # Update fields
    if "project_title" in deal_data:
        deal.project_title = deal_data["project_title"]
    if "project_url" in deal_data:
        deal.project_url = deal_data["project_url"]
    if "client_payment" in deal_data:
        deal.client_payment = float(deal_data["client_payment"])
    if "outsource_cost" in deal_data:
        deal.outsource_cost = float(deal_data["outsource_cost"])
    if "platform_fee" in deal_data:
        deal.platform_fee = float(deal_data["platform_fee"])
    if "status" in deal_data:
        deal.status = deal_data["status"]
    if "completion_date" in deal_data:
        if deal_data["completion_date"]:
            deal.completion_date = datetime.fromisoformat(deal_data["completion_date"].replace('Z', '+00:00'))
        else:
            deal.completion_date = None
    
    # Recalculate profit
    deal.profit = deal.client_payment - deal.outsource_cost - deal.platform_fee
    deal.updated_at = datetime.utcnow()
    
    db.commit()
    db.refresh(deal)
    
    return {
        "success": True,
        "deal": {
            "id": deal.id,
            "bid_history_id": deal.bid_history_id,
            "project_title": deal.project_title,
            "project_url": deal.project_url,
            "platform": deal.platform,
            "client_payment": deal.client_payment,
            "outsource_cost": deal.outsource_cost,
            "platform_fee": deal.platform_fee,
            "profit": deal.profit,
            "status": deal.status,
            "closed_date": deal.closed_date.isoformat(),
            "completion_date": deal.completion_date.isoformat() if deal.completion_date else None
        }
    }

@router.delete("/api/crm/deals/{deal_id}")
async def delete_closed_deal(
    deal_id: int,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Delete a closed deal"""
    from models import ClosedDeal
    user = get_user_by_email(email, db)
    
    deal = db.query(ClosedDeal).filter(
        ClosedDeal.id == deal_id,
        ClosedDeal.user_id == user.id
    ).first()
    
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    
    db.delete(deal)
    db.commit()
    
    return {"success": True, "message": "Deal deleted successfully"}

@router.get("/api/crm/stats")
async def get_crm_stats(
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get CRM statistics for dashboard"""
    from models import ClosedDeal
    user = get_user_by_email(email, db)
    
    # Total revenue (all closed deals)
    total_revenue = db.query(func.sum(ClosedDeal.client_payment)).filter(
        ClosedDeal.user_id == user.id,
        ClosedDeal.status.in_(['active', 'completed'])
    ).scalar() or 0
    
    # Total profit
    total_profit = db.query(func.sum(ClosedDeal.profit)).filter(
        ClosedDeal.user_id == user.id,
        ClosedDeal.status.in_(['active', 'completed'])
    ).scalar() or 0
    
    # Count of deals
    total_deals = db.query(ClosedDeal).filter(
        ClosedDeal.user_id == user.id
    ).count()
    
    # Active deals
    active_deals = db.query(ClosedDeal).filter(
        ClosedDeal.user_id == user.id,
        ClosedDeal.status == 'active'
    ).count()
    
    return {
        "total_revenue": float(total_revenue),
        "total_profit": float(total_profit),
        "total_deals": total_deals,
        "active_deals": active_deals
    }

