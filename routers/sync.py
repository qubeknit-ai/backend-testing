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

@router.post("/api/sync-send")
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

@router.get("/api/sync-receive")
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
                            category=extract_category_from_text(title, lead_data.get("description", ""), platform),
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

@router.get("/api/n8n/settings/{user_id}")
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

