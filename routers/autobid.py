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
from autobid_service import bidder as autobidder

router = APIRouter()

@router.get("/api/autobid/heartbeat")
async def autobid_heartbeat():
    """Heartbeat endpoint to keep AutoBidder service alive"""
    return {
        "success": True,
        "timestamp": datetime.now().isoformat(),
        "is_running": autobidder._is_running,
        "message": "AutoBidder service heartbeat"
    }

@router.get("/api/autobid/stats")
async def get_autobid_stats(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    try:
        from models import BidHistory, AutoBidSettings

        user = get_user_by_email(email, db)

        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())

        # Single aggregation query — replaces 6 separate DB round-trips
        row = db.query(
            func.count(
                case((BidHistory.created_at >= today_start, 1), else_=None)
            ).label('bids_today'),
            func.count(
                case((BidHistory.created_at >= week_start, 1), else_=None)
            ).label('bids_week'),
            func.count(
                case((
                    (BidHistory.created_at >= week_start) &
                    func.lower(BidHistory.status).in_(['success', 'accepted', 'awarded']),
                    1
                ), else_=None)
            ).label('success_week'),
            func.count(
                case((
                    (BidHistory.created_at >= week_start) &
                    func.lower(BidHistory.status).in_(['failed', 'rejected', 'declined', 'error']),
                    1
                ), else_=None)
            ).label('failed_week'),
            func.coalesce(
                func.sum(case((BidHistory.created_at >= today_start, BidHistory.bid_amount), else_=None)),
                0
            ).label('amount_today'),
            func.coalesce(
                func.sum(case((BidHistory.created_at >= week_start, BidHistory.bid_amount), else_=None)),
                0
            ).label('amount_week'),
        ).filter(BidHistory.user_id == user.id).first()

        # Separate small query for settings (different table, unavoidable)
        settings = db.query(AutoBidSettings.enabled).filter(AutoBidSettings.user_id == user.id).first()
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
        print(f"Error fetching autobid stats: {e}")
        return {
            "bids_today": 0,
            "bids_week": 0,
            "success_week": 0,
            "failed_week": 0,
            "bid_amount_today": 0.0,
            "bid_amount_week": 0.0,
            "is_running": False
        }

@router.get("/api/autobid/settings")
async def get_autobid_settings(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    """Get current AutoBidder settings from database"""
    from models import AutoBidSettings as DBAutoBidSettings
    user = get_user_by_email(email, db)
    
    # Get settings from database or create default
    db_settings = db.query(DBAutoBidSettings).filter(DBAutoBidSettings.user_id == user.id).first()
    if not db_settings:
        db_settings = DBAutoBidSettings(user_id=user.id)
        db.add(db_settings)
        db.commit()
        db.refresh(db_settings)
    
    return {
        "enabled": db_settings.enabled,
        "daily_bids": db_settings.daily_bids,
        "currencies": db_settings.currencies,
        "frequency_minutes": db_settings.frequency_minutes,
        "max_project_bids": db_settings.max_project_bids,
        "smart_bidding": db_settings.smart_bidding,
        "min_skill_match": getattr(db_settings, 'min_skill_match', 1),
        "proposal_type": getattr(db_settings, 'proposal_type', 1)
    }

@router.post("/api/autobid/settings")
async def update_autobid_settings(
    settings: AutoBidSettings,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Update AutoBidder settings in database"""
    from models import AutoBidSettings as DBAutoBidSettings
    user = get_user_by_email(email, db)
    
    # Get or create settings
    db_settings = db.query(DBAutoBidSettings).filter(DBAutoBidSettings.user_id == user.id).first()
    if not db_settings:
        db_settings = DBAutoBidSettings(user_id=user.id)
        db.add(db_settings)
    
    # Update fields
    if settings.enabled is not None:
        db_settings.enabled = settings.enabled
    if settings.daily_bids is not None:
        db_settings.daily_bids = settings.daily_bids
    if settings.currencies is not None:
        db_settings.currencies = settings.currencies
    if settings.frequency_minutes is not None:
        db_settings.frequency_minutes = settings.frequency_minutes
    if settings.max_project_bids is not None:
        db_settings.max_project_bids = settings.max_project_bids
    if settings.smart_bidding is not None:
        db_settings.smart_bidding = settings.smart_bidding
    if settings.min_skill_match is not None:
        db_settings.min_skill_match = settings.min_skill_match
    if settings.proposal_type is not None:
        db_settings.proposal_type = settings.proposal_type
    if settings.commission_projects is not None:
        db_settings.commission_projects = settings.commission_projects
    
    db.commit()
    db.refresh(db_settings)
    
    # Update in-memory settings
    settings_dict = {
        "enabled": db_settings.enabled,
        "daily_bids": db_settings.daily_bids,
        "currencies": db_settings.currencies,
        "frequency_minutes": db_settings.frequency_minutes,
        "max_project_bids": db_settings.max_project_bids,
        "smart_bidding": db_settings.smart_bidding,
        "min_skill_match": db_settings.min_skill_match,
        "proposal_type": db_settings.proposal_type,
        "commission_projects": db_settings.commission_projects
    }
    autobidder.update_settings(settings_dict)
    
    return settings_dict

@router.post("/api/autobid/start")
async def start_autobidder(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    """Manually start the AutoBidder service"""
    try:
        from models import AutoBidSettings as DBAutoBidSettings
        
        user = get_user_by_email(email, db)
        
        # Update database to mark as enabled
        db_settings = db.query(DBAutoBidSettings).filter(DBAutoBidSettings.user_id == user.id).first()
        if db_settings:
            db_settings.enabled = True
            db.commit()
        
        # Start the service
        autobidder.start()
        
        return {
            "success": True,
            "status": "started", 
            "message": "Auto-bidder started successfully",
            "settings": autobidder.get_settings()
        }
    except Exception as e:
        print(f"Error starting auto-bidder: {e}")
        return {
            "success": False,
            "status": "error",
            "message": f"Failed to start auto-bidder: {str(e)}"
        }

@router.post("/api/autobid/stop")
async def stop_autobidder(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    """Manually stop the AutoBidder service"""
    try:
        from models import AutoBidSettings as DBAutoBidSettings
        
        user = get_user_by_email(email, db)
        
        # Update database to mark as disabled
        db_settings = db.query(DBAutoBidSettings).filter(DBAutoBidSettings.user_id == user.id).first()
        if db_settings:
            db_settings.enabled = False
            db.commit()
        
        # Stop the service
        autobidder.stop()
        
        return {
            "success": True,
            "status": "stopped",
            "message": "Auto-bidder stopped successfully"
        }
    except Exception as e:
        print(f"Error stopping auto-bidder: {e}")
        return {
            "success": False,
            "status": "error", 
            "message": f"Failed to stop auto-bidder: {str(e)}"
        }

@router.post("/api/autobid/history")
async def save_bid_history(bid_data: dict, db: Session = Depends(get_db)):
    """Save bid history (called by autobidder)"""
    from models import BidHistory
    
    # For now, use a default user_id (in production, extract from token)
    user_id = 1  # TODO: Get from authenticated user
    
    history = BidHistory(
        user_id=user_id,
        project_id=bid_data.get("project_id"),
        project_title=bid_data.get("project_title"),
        project_url=bid_data.get("project_url"),
        bid_amount=bid_data.get("bid_amount", 0),
        proposal_text=bid_data.get("proposal_text"),
        status=bid_data.get("status", "pending"),
        error_message=bid_data.get("error_message")
    )
    
    db.add(history)
    db.commit()
    return {"success": True}

@router.get("/api/autobid/history")
async def get_bid_history(
    email: str = Depends(verify_token), 
    db: Session = Depends(get_db),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0)
):
    """Get bid history for current user with pagination"""
    from models import BidHistory
    user = get_user_by_email(email, db)
    
    # Get total count
    total = db.query(BidHistory).filter(BidHistory.user_id == user.id).count()
    
    # Get paginated history
    history = db.query(BidHistory).filter(
        BidHistory.user_id == user.id
    ).order_by(
        BidHistory.created_at.desc()
    ).offset(offset).limit(limit).all()
    
    return {
        "history": [
            {
                "id": h.id,
                "project_title": h.project_title,
                "project_url": h.project_url,
                "amount": h.bid_amount,
                "bid_time": h.created_at.isoformat(),
                "status": h.status,
                "error": h.error_message,
                "url": h.project_url
            }
            for h in history
        ],
        "total": total,
        "limit": limit,
        "offset": offset
    }

@router.post("/api/bid/place")
async def place_bid(bid: BidRequest):
    """Place a bid on a Freelancer project"""
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            
            print(f"Attempting bid on project {bid.project_id}...")
            
            # Prepare headers - mimic browser request exactly
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Origin": "https://www.freelancer.com",
                "Referer": f"https://www.freelancer.com/projects/{bid.project_id}",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            }
            
            # Use OAuth token if available
            if bid.access_token and bid.access_token != 'using_cookies':
                print("Using OAuth token for authentication")
                headers["Authorization"] = f"Bearer {bid.access_token}"
                headers["freelancer-oauth-v1"] = bid.access_token
            else:
                print("Using cookie-based authentication (No OAuth token provided)")
            
            # Parse ALL cookies from the JSON cookie string
            cookies_dict = {}
            csrf_token = None
            user_id = None
            auth_hash = None
            
            if bid.freelancer_cookies:
                try:
                    # Parse JSON cookie object
                    cookie_data = json.loads(bid.freelancer_cookies)
                    
                    user_id = cookie_data.get('GETAFREE_USER_ID')
                    auth_hash = cookie_data.get('GETAFREE_AUTH_HASH_V2')
                    csrf_token = cookie_data.get('XSRF_TOKEN')
                    session2 = cookie_data.get('session2', '')
                    qfence = cookie_data.get('qfence', '')
                    cookieconsent_status = cookie_data.get('cookieconsent_status', '')
                    
                    # MANDATORY: Check all required cookies exist
                    if not user_id or not auth_hash:
                        return {
                            "success": False,
                            "error": "Missing required cookies (GETAFREE_USER_ID or GETAFREE_AUTH_HASH_V2). Please extract credentials again.",
                            "status_code": 400
                        }
                    
                    if not session2:
                        return {
                            "success": False,
                            "error": "session2 cookie missing - this is required for bidding. Please log out and log back into Freelancer.com, then extract credentials again.",
                            "status_code": 400
                        }
                    
                    # Make CSRF token optional - try bidding without it if missing
                    if not csrf_token:
                        print("Warning: XSRF-TOKEN cookie missing - will attempt bidding without CSRF protection")
                        csrf_token = ""  # Set empty string to avoid None errors
                    
                    # Set ALL required cookies
                    cookies_dict['GETAFREE_USER_ID'] = user_id
                    cookies_dict['GETAFREE_AUTH_HASH_V2'] = auth_hash
                    if csrf_token:
                        cookies_dict['XSRF-TOKEN'] = csrf_token
                    cookies_dict['session2'] = session2
                    
                    if qfence:
                        cookies_dict['qfence'] = qfence
                    
                    if cookieconsent_status:
                        cookies_dict['cookieconsent_status'] = cookieconsent_status
                    
                    # Add CSRF headers only if token is available
                    if csrf_token:
                        headers["X-CSRF-Token"] = csrf_token
                        headers["X-XSRF-TOKEN"] = csrf_token
                        headers["x-csrf-token"] = csrf_token
                        headers["x-xsrf-token"] = csrf_token
                        print(f"  All CSRF headers set with token: {csrf_token[:20]}...")
                    else:
                        print("  No CSRF token available - proceeding without CSRF headers")
                    headers["x-requested-with"] = "XMLHttpRequest"
                    headers["freelancer-auth-v2"] = f"{user_id};{auth_hash}"
                    headers["sec-ch-ua-platform"] = '"Windows"'
                    
                    print(f"Using Freelancer session cookies:")
                    print(f"  GETAFREE_USER_ID: {user_id}")
                    print(f"  GETAFREE_AUTH_HASH_V2: {auth_hash[:30]}...")
                    print(f"  XSRF-TOKEN: {csrf_token[:20] if csrf_token else 'Not provided'}...")
                    print(f"  session2: {'Present' if session2 else 'MISSING (REQUIRED)'}")
                    print(f"  qfence: {'Present' if qfence else 'Missing'}")
                    print(f"  cookieconsent_status: {'Present' if cookieconsent_status else 'Missing'}")
                    print(f"  freelancer-auth-v2: {user_id};{auth_hash[:20]}...")
                    print(f"  x-requested-with: XMLHttpRequest")
                    print(f"  All CSRF headers: X-CSRF-Token, X-XSRF-TOKEN, x-csrf-token, x-xsrf-token")
                        
                except json.JSONDecodeError as e:
                    print(f"Error parsing cookie JSON: {e}")
                    return {
                        "success": False,
                        "error": "Invalid cookie format. Please extract credentials again.",
                        "status_code": 400
                    }
            else:
                if not headers.get("Authorization"):
                    print("Error: No Freelancer cookies provided and no Access Token")
                    return {
                        "success": False,
                        "error": "No authentication credentials. Please extract credentials from Token tab.",
                        "status_code": 401
                    }
                else:
                    print("Warning: No Freelancer cookies provided, relying on Access Token")
            
            # Verify we have credentials (either cookies or token)
            if not cookies_dict and not headers.get("Authorization"):
                return {
                    "success": False,
                    "error": "No authentication credentials provided. Please extract credentials first.",
                    "status_code": 401
                }
            
            # CRITICAL: Ensure bidder_id matches the authenticated user
            if user_id and str(bid.bidder_id) != str(user_id):
                return {
                    "success": False,
                    "error": f"Bidder ID mismatch: bidder_id ({bid.bidder_id}) must match GETAFREE_USER_ID ({user_id}). Please use the correct user ID.",
                    "status_code": 400
                }
            
            # Use the exact endpoint that Freelancer's frontend uses with query parameters
            api_url = "https://www.freelancer.com/api/projects/0.1/bids/?compact=true&new_errors=true&new_pools=true"
            
            # Prepare JSON payload exactly as Freelancer frontend does
            payload = {
                "project_id": bid.project_id,
                "bidder_id": bid.bidder_id,
                "amount": bid.amount,
                "period": bid.period,
                "milestone_percentage": bid.milestone_percentage,
                "highlighted": False,
                "sponsored": False,
                "ip_contract": False,
                "anonymous": False,
                "description": bid.description
            }
            
            # Use JSON content type as Freelancer frontend does
            headers["Content-Type"] = "application/json"
            
            print(f"Attempting API endpoint: {api_url}")
            print(f"JSON payload: {payload}")
            print(f"Cookies: {list(cookies_dict.keys())}")
            print(f"CSRF headers: X-CSRF-TOKEN = {headers.get('X-CSRF-TOKEN', 'Not set')[:20]}...")
            
            response = await client.post(
                api_url,
                headers=headers,
                cookies=cookies_dict,
                json=payload,
                timeout=30.0
            )
            
            print(f"Response status: {response.status_code}")
            print(f"Response headers: {dict(response.headers)}")
            response_text = response.text
            print(f"Response body: {response_text[:1000]}")
            
            # Log full response for debugging
            if response.status_code != 200 and response.status_code != 201:
                print(f"FULL ERROR RESPONSE: {response_text}")
            
            if response.status_code == 200 or response.status_code == 201:
                try:
                    response_data = response.json()
                    print(f"Success! Bid placed: {response_data}")
                    return {
                        "success": True,
                        "data": response_data,
                        "message": f"Bid placed successfully: ${bid.amount}"
                    }
                except Exception as e:
                    print(f"Error parsing response: {e}")
                    return {
                        "success": True,
                        "message": f"Bid placed (status {response.status_code})"
                    }
            else:
                error_text = response.text
                print(f"Bid failed with status {response.status_code}")
                print(f"Error details: {error_text}")
                
                # Parse error message
                try:
                    error_json = response.json()
                    error_msg = error_json.get('message', error_text)
                except:
                    error_msg = error_text
                
                return {
                    "success": False,
                    "error": error_msg,
                    "status_code": response.status_code,
                    "full_response": error_text[:500]
                }
                
    except Exception as e:
        print(f"Exception: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/bid/place")
async def place_bid_with_cookies(
    request_data: dict
):
    """Place a bid using extension credentials"""
    try:
        access_token = request_data.get("access_token")
        freelancer_cookies = request_data.get("freelancer_cookies")
        project_id = request_data.get("project_id")
        bidder_id = request_data.get("bidder_id")
        amount = request_data.get("amount")
        period = request_data.get("period", 7)
        description = request_data.get("description", "")
        
        if not project_id or not bidder_id or not amount:
            raise HTTPException(status_code=400, detail="project_id, bidder_id, and amount are required")
        
        # If we have an OAuth token, use it directly
        if access_token and access_token != "using_cookies":
            headers = {
                "Authorization": f"Bearer {access_token}",
                "freelancer-oauth-v1": access_token,
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            form_data = {
                "bidder_id": str(bidder_id),
                "amount": str(amount),
                "period": str(period),
                "description": description,
                "milestone_percentage": "100"
            }
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"https://www.freelancer.com/api/projects/0.1/projects/{project_id}/bids",
                    headers=headers,
                    data=form_data
                )
                
                if response.status_code == 200:
                    return {"success": True, "message": "Bid placed successfully"}
                else:
                    error_text = response.text
                    print(f"Freelancer API Error {response.status_code}: {error_text}")
                    
                    # Try to parse JSON error response to get the actual error message
                    try:
                        error_data = response.json()
                        if isinstance(error_data, dict):
                            # Look for common error message fields
                            actual_error = (
                                error_data.get('message') or 
                                error_data.get('error') or 
                                error_data.get('detail') or
                                error_data.get('error_description') or
                                str(error_data)
                            )
                            return {"success": False, "error": actual_error}
                    except:
                        pass
                    
                    # Fallback to raw error text
                    return {"success": False, "error": error_text or f"API Error {response.status_code}"}
        
        # Cookie-based bidding would be implemented here
        return {
            "success": False,
            "error": "Cookie-based bidding not implemented in backend"
        }
        
    except Exception as e:
        print(f"Error placing bid: {e}")
        return {"success": False, "error": str(e)}

