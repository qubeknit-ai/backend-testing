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
from .auth import get_password_hash, verify_password, create_access_token, verify_token, SECRET_KEY, ALGORITHM

router = APIRouter()

@router.post("/api/fetch-upwork")
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
        
        # Get user's settings or create default ones
        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
        if not settings:
            # Create default settings for the user
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

@router.post("/api/fetch-freelancer")
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
        
        # Get user's settings or create default ones
        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
        if not settings:
            # Create default settings for the user
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
        
        # OPTIMIZED: Reduced timeout from 600s to 30s for faster response
        async with httpx.AsyncClient(timeout=30.0) as client:
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

@router.post("/api/fetch-freelancer-plus")
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
        
        # Get user's settings or create default ones
        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
        if not settings:
            # Create default settings for the user
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
        
        # OPTIMIZED: Reduced timeout from 600s to 30s for faster response
        async with httpx.AsyncClient(timeout=30.0) as client:
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

@router.get("/api/fetch-limits")
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

