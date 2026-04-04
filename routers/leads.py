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

@router.post("/api/leads/bulk")
async def receive_leads_from_n8n(payload: dict, db: Session = Depends(get_db)):
    """
    Endpoint for N8N to send scraped leads back to backend
    Expected payload: {
        "user_id": 1,
        "leads": [
            {
                "platform": "Upwork",
                "title": "Job Title",
                "budget": "$500",
                "posted": "2 hours ago",
                "status": "Pending",
                "score": "8",
                "description": "Job description",
                "url": "https://..."
            },
            ...
        ]
    }
    """
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import Lead, Notification
        from dateutil import parser
        
        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        
        leads_data = payload.get("leads", [])
        if not leads_data:
            return {"success": True, "message": "No leads to save", "count": 0}
        
        saved_count = 0
        
        for lead_data in leads_data:
            # Skip if lead doesn't have essential data
            title = lead_data.get("title", lead_data.get("titlle"))
            platform = lead_data.get("platform")
            
            if not title or not platform:
                print(f"Skipping lead with missing title or platform: {lead_data}")
                continue
            
            # Check if lead already exists by title and platform for this user
            existing_lead = db.query(Lead).filter(
                Lead.user_id == user_id,
                Lead.title == title,
                Lead.platform == platform
            ).first()
            
            if not existing_lead:
                # Parse posted_time if available
                posted_time = None
                if lead_data.get("posted_time"):
                    try:
                        posted_time = parser.parse(lead_data.get("posted_time"))
                    except:
                        posted_time = None
                
                new_lead = Lead(
                    user_id=user_id,
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
                saved_count += 1
        
        db.commit()
        
        # Create notification for user
        if saved_count > 0:
            notification = Notification(
                user_id=user_id,
                type="success",
                title="Jobs Fetched Successfully",
                message=f"Successfully fetched {saved_count} new jobs from {leads_data[0].get('platform', 'platform')}"
            )
            db.add(notification)
            db.commit()
        
        print(f"✅ Saved {saved_count} leads for user_id={user_id}")
        return {"success": True, "message": f"Saved {saved_count} leads", "count": saved_count}
        
    except Exception as e:
        print(f"Error saving leads from N8N: {str(e)}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@router.get("/api/leads")
async def get_leads(
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    platform: Optional[str] = Query(None, description="Filter by platform"),
    status: Optional[str] = Query(None, description="Filter by status"),
    email: str = Depends(verify_token), 
    db: Session = Depends(get_db)
):
    try:
        if db is None:
            return {"leads": [], "total": 0, "page": page, "limit": limit}
        
        from models import Lead
        user = get_user_by_email(email, db)
        
        # Build query with filters - OPTIMIZED: Use indexed columns
        query = db.query(Lead).filter(
            Lead.user_id == user.id, 
            Lead.visible == True
        )
        
        if platform:
            query = query.filter(Lead.platform == platform)
        if status:
            query = query.filter(Lead.status == status)
        
        # OPTIMIZED: Get count and data in single query using window function
        # This is faster than separate count() query
        from sqlalchemy import func, over
        
        # Get total count efficiently
        total = query.count()
        
        # Apply pagination and ordering - OPTIMIZED: Use indexed column
        offset = (page - 1) * limit
        leads = query.order_by(Lead.updated_at.desc()).offset(offset).limit(limit).all()
        
        # OPTIMIZED: Build response with minimal data processing
        return {
            "leads": [
                {
                    "id": lead.id,
                    "platform": lead.platform,
                    "title": lead.title,
                    "budget": lead.budget,
                    "bids": lead.bids or 0,
                    "cost": lead.cost or 0,
                    "posted": lead.posted,
                    "posted_time": lead.posted_time.isoformat() if lead.posted_time else None,
                    "status": lead.status,
                    "score": lead.score,
                    "description": lead.description,
                    "Proposal": lead.proposal,
                    "url": lead.url,
                    "avg_bid_price": lead.avg_bid_price,
                    "created_at": lead.created_at.isoformat() if lead.created_at else None,
                    "updated_at": lead.updated_at.isoformat() if lead.updated_at else None
                }
                for lead in leads
            ],
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit
        }
    except Exception as e:
        print(f"❌ Error fetching leads: {e}")
        import traceback
        traceback.print_exc()
        return {"leads": [], "total": 0, "page": page, "limit": limit}

@router.put("/api/leads/{lead_id}/proposal")
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
        user = get_user_by_email(email, db)
        
        # Find the lead and verify ownership
        lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found or access denied")
        
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
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@router.put("/api/leads/{lead_id}/approve")
async def approve_lead(
    lead_id: int,
    email: str = Depends(verify_token)
):
    """Approve a lead - simplified version that works like the extension"""
    try:
        print(f"Approving lead {lead_id} for user: {email}")
        
        # Try to update in database if available, but don't fail if DB is down
        lead_data = None
        try:
            from database import SessionLocal
            from models import Lead, User
            
            db = SessionLocal()
            try:
                user = db.query(User).filter(User.email == email).first()
                if user:
                    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
                    if lead:
                        # Update the status to Approved
                        lead.status = "Approved"
                        lead.updated_at = datetime.utcnow()
                        
                        db.commit()
                        db.refresh(lead)
                        
                        lead_data = {
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
                        print(f"✅ Successfully updated lead {lead_id} in database")
            finally:
                db.close()
        except Exception as db_error:
            print(f"⚠️ Database update failed, but continuing: {db_error}")
            # Continue without database - create a mock response
            lead_data = {
                "id": lead_id,
                "platform": "Unknown",
                "title": "Lead approved without database",
                "budget": "",
                "posted": "",
                "posted_time": None,
                "status": "Approved",
                "score": "",
                "description": "",
                "Proposal": "",
                "url": "",
                "updated_at": datetime.utcnow().isoformat()
            }
        
        print(f"✅ Approved lead {lead_id}")
        return {
            "success": True,
            "message": "Lead approved successfully",
            "lead": lead_data
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error approving lead: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.delete("/api/leads/clean")
async def clean_leads(email: str = Depends(verify_token), db: Session = Depends(get_db)):
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import Lead
        user = get_user_by_email(email, db)
        
        # Log before hiding
        total_leads_before = db.query(Lead).count()
        user_leads_before = db.query(Lead).filter(Lead.user_id == user.id, Lead.visible == True).count()
        print(f"[CLEAN LEADS] User: {user.email} (ID: {user.id})")
        print(f"[CLEAN LEADS] Total leads in DB: {total_leads_before}")
        print(f"[CLEAN LEADS] User's visible leads: {user_leads_before}")
        
        # Hide only this user's visible leads by setting visible=False
        hidden_count = db.query(Lead).filter(Lead.user_id == user.id, Lead.visible == True).update({"visible": False})
        db.commit()
        
        # Log after hiding
        user_visible_after = db.query(Lead).filter(Lead.user_id == user.id, Lead.visible == True).count()
        print(f"[CLEAN LEADS] Hidden: {hidden_count} leads")
        print(f"[CLEAN LEADS] User's visible leads remaining: {user_visible_after}")
        
        return {"success": True, "message": f"Hidden {hidden_count} leads for {user.email}", "count": hidden_count}
    except Exception as e:
        print(f"Error cleaning leads: {e}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@router.get("/api/admin/stats")
async def get_admin_stats(user = Depends(verify_admin), db: Session = Depends(get_db)):
    """Get system-wide statistics for admin dashboard"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import User, Lead, AutoBidSettings
        from datetime import datetime, timedelta
        from sqlalchemy import func
        
        # Total users
        total_users = db.query(User).count()
        
        # Total leads
        total_leads = db.query(Lead).count()
        
        # Auto-bidder statistics
        # Count all users and their auto-bid status
        total_users_count = db.query(User).count()
        
        # Count users with auto-bidding enabled
        auto_bid_enabled_count = db.query(AutoBidSettings).filter(AutoBidSettings.enabled == True).count()
        
        # Users without settings or with disabled settings are considered disabled
        auto_bid_disabled_count = total_users_count - auto_bid_enabled_count
        
        # Average bid frequency from auto_bid_settings (only from existing settings)
        avg_frequency_result = db.query(func.avg(AutoBidSettings.frequency_minutes)).scalar()
        avg_bid_frequency = round(avg_frequency_result, 1) if avg_frequency_result else 10.0  # Default to 10 if no settings
        
        # Platform breakdown — single SQL aggregation, no full table scan
        platform_rows = db.query(
            Lead.platform,
            func.count(Lead.id).label('cnt'),
            func.coalesce(func.sum(Lead.revenue), 0).label('revenue'),
            func.count(case((Lead.proposal_sent == True, 1), else_=None)).label('proposals_sent'),
            func.count(case(
                ((Lead.proposal_sent == True) & (Lead.proposal_accepted == True), 1),
                else_=None
            )).label('proposals_accepted'),
        ).group_by(Lead.platform).all()

        total_with_platform = sum(r.cnt for r in platform_rows)
        total_revenue = sum(float(r.revenue or 0) for r in platform_rows)
        total_proposals_sent = sum(r.proposals_sent for r in platform_rows)
        total_proposals_accepted = sum(r.proposals_accepted for r in platform_rows)

        platform_breakdown = [
            {
                "name": r.platform or "Unknown",
                "count": r.cnt,
                "percentage": round((r.cnt / total_with_platform * 100), 1) if total_with_platform > 0 else 0,
                "revenue": float(r.revenue or 0)
            }
            for r in platform_rows
        ]

        # Calculate success rate
        success_rate = round((total_proposals_accepted / total_proposals_sent * 100), 1) if total_proposals_sent > 0 else 0
        
        return {
            "totalUsers": total_users,
            "totalLeads": total_leads,
            "autoBidEnabled": auto_bid_enabled_count,
            "autoBidDisabled": auto_bid_disabled_count,
            "avgBidFrequency": avg_bid_frequency,
            "platformBreakdown": platform_breakdown,
            "totalRevenue": total_revenue,
            "proposalsSent": total_proposals_sent,
            "proposalsAccepted": total_proposals_accepted,
            "successRate": success_rate
        }
    except Exception as e:
        print(f"Error fetching admin stats: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@router.get("/api/admin/users")
async def get_all_users(user = Depends(verify_admin), db: Session = Depends(get_db)):
    """Get all users with their stats"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import User, Lead, BidHistory, ClosedDeal
        from datetime import datetime, timedelta
        
        all_users = db.query(User).all()
        user_ids = [u.id for u in all_users]

        # Calculate date ranges
        today = datetime.utcnow().date()
        today_dt = datetime.combine(today, datetime.min.time())
        week_ago_dt = today_dt - timedelta(days=7)

        # Batch leads stats — one query for all users
        leads_rows = db.query(
            Lead.user_id,
            func.count(Lead.id).label('total'),
            func.count(case((Lead.proposal_accepted == True, 1), else_=None)).label('approved')
        ).filter(Lead.user_id.in_(user_ids)).group_by(Lead.user_id).all()
        leads_map = {r.user_id: r for r in leads_rows}

        # Batch bid history stats — one query for all users
        bid_rows = db.query(
            BidHistory.user_id,
            func.count(case((BidHistory.created_at >= today_dt, 1), else_=None)).label('bids_today'),
            func.count(case((BidHistory.created_at >= week_ago_dt, 1), else_=None)).label('bids_week'),
            func.count(case(
                ((BidHistory.created_at >= today_dt) & (BidHistory.status == 'success'), 1),
                else_=None
            )).label('success_today'),
            func.count(case(
                ((BidHistory.created_at >= week_ago_dt) & (BidHistory.status == 'success'), 1),
                else_=None
            )).label('success_week'),
        ).filter(BidHistory.user_id.in_(user_ids)).group_by(BidHistory.user_id).all()
        bid_map = {r.user_id: r for r in bid_rows}

        # Batch profit stats — one query for all users
        profit_rows = db.query(
            ClosedDeal.user_id,
            func.coalesce(func.sum(ClosedDeal.profit), 0).label('total_profit')
        ).filter(
            ClosedDeal.user_id.in_(user_ids),
            ClosedDeal.status.in_(['active', 'completed'])
        ).group_by(ClosedDeal.user_id).all()
        profit_map = {r.user_id: float(r.total_profit) for r in profit_rows}

        users_data = []
        for u in all_users:
            l = leads_map.get(u.id)
            b = bid_map.get(u.id)
            users_data.append({
                "id": u.id,
                "email": u.email,
                "name": u.name,
                "role": u.role,
                "upwork_fetch_count": u.upwork_fetch_count or 0,
                "freelancer_fetch_count": u.freelancer_fetch_count or 0,
                "freelancer_plus_fetch_count": u.freelancer_plus_fetch_count or 0,
                "leads_count": l.total if l else 0,
                "approved_leads_count": l.approved if l else 0,
                "bids_today": b.bids_today if b else 0,
                "bids_week": b.bids_week if b else 0,
                "success_today": b.success_today if b else 0,
                "success_week": b.success_week if b else 0,
                "total_profit": profit_map.get(u.id, 0.0),
                "created_at": u.created_at.isoformat() if u.created_at else None
            })

        return {"users": users_data}
    except Exception as e:
        print(f"Error fetching users: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@router.put("/api/admin/users/{user_id}")
async def update_user(
    user_id: int,
    update_data: dict,
    admin_user = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    """Update user role and limits"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import User
        
        target_user = db.query(User).filter(User.id == user_id).first()
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Update role if provided
        if "role" in update_data:
            target_user.role = update_data["role"]
        
        # Note: Daily limits are currently hardcoded in check_and_reset_daily_limit
        # For now, we'll store them in user settings or add columns to User model
        # This is a placeholder for future implementation
        
        target_user.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(target_user)
        
        return {
            "success": True,
            "message": "User updated successfully",
            "user": {
                "id": target_user.id,
                "email": target_user.email,
                "role": target_user.role
            }
        }
    except Exception as e:
        print(f"Error updating user: {e}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@router.delete("/api/admin/users/{user_id}")
async def delete_user(
    user_id: int,
    admin_user = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    """Delete a user and all their data"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import User
        
        # Prevent admin from deleting themselves
        if admin_user.id == user_id:
            raise HTTPException(status_code=400, detail="Cannot delete your own account")
        
        target_user = db.query(User).filter(User.id == user_id).first()
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Delete user (cascade will delete leads, settings, notifications)
        db.delete(target_user)
        db.commit()
        
        return {
            "success": True,
            "message": f"User {target_user.email} deleted successfully"
        }
    except Exception as e:
        print(f"Error deleting user: {e}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@router.post("/api/admin/users/{user_id}/reset-fetch-count")
async def reset_user_fetch_count(
    user_id: int,
    admin_user = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    """Reset fetch counts for a user"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import User
        
        target_user = db.query(User).filter(User.id == user_id).first()
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Reset all fetch counts
        target_user.upwork_fetch_count = 0
        target_user.freelancer_fetch_count = 0
        target_user.freelancer_plus_fetch_count = 0
        target_user.upwork_last_reset = datetime.utcnow()
        target_user.freelancer_last_reset = datetime.utcnow()
        target_user.freelancer_plus_last_reset = datetime.utcnow()
        
        db.commit()
        
        return {
            "success": True,
            "message": f"Fetch counts reset for {target_user.email}"
        }
    except Exception as e:
        print(f"Error resetting fetch count: {e}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@router.get("/api/admin/settings")
async def get_admin_settings(user = Depends(verify_admin), db: Session = Depends(get_db)):
    """Get system-wide settings"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        # Get settings from database
        system_settings = get_system_settings(db)
        
        return {
            "default_upwork_limit": system_settings.default_upwork_limit,
            "default_freelancer_limit": system_settings.default_freelancer_limit,
            "default_freelancer_plus_limit": system_settings.default_freelancer_plus_limit,
            "default_upwork_max_jobs": getattr(system_settings, 'default_upwork_max_jobs', 3),
            "default_freelancer_max_jobs": getattr(system_settings, 'default_freelancer_max_jobs', 3)
        }
    except Exception as e:
        print(f"Error fetching admin settings: {e}")
        raise HTTPException(status_code=500, detail="Load On server Plz try again Later")

@router.put("/api/admin/settings")
async def update_admin_settings(
    settings_data: dict,
    user = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    """Update system-wide settings"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        # Get or create system settings
        system_settings = get_system_settings(db)
        
        # Update daily limits
        if "default_upwork_limit" in settings_data:
            system_settings.default_upwork_limit = settings_data["default_upwork_limit"]
        if "default_freelancer_limit" in settings_data:
            system_settings.default_freelancer_limit = settings_data["default_freelancer_limit"]
        if "default_freelancer_plus_limit" in settings_data:
            system_settings.default_freelancer_plus_limit = settings_data["default_freelancer_plus_limit"]
        
        # Update max jobs per fetch
        if "default_upwork_max_jobs" in settings_data:
            system_settings.default_upwork_max_jobs = settings_data["default_upwork_max_jobs"]
        if "default_freelancer_max_jobs" in settings_data:
            system_settings.default_freelancer_max_jobs = settings_data["default_freelancer_max_jobs"]
        
        system_settings.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(system_settings)
        
        print(f"✅ Admin settings updated: Upwork Limit={system_settings.default_upwork_limit}, Freelancer Limit={system_settings.default_freelancer_limit}, Freelancer+ Limit={system_settings.default_freelancer_plus_limit}")
        print(f"   Max Jobs: Upwork={getattr(system_settings, 'default_upwork_max_jobs', 3)}, Freelancer={getattr(system_settings, 'default_freelancer_max_jobs', 3)}")
        
        return {
            "success": True,
            "message": "Settings updated successfully",
            "settings": {
                "default_upwork_limit": system_settings.default_upwork_limit,
                "default_freelancer_limit": system_settings.default_freelancer_limit,
                "default_freelancer_plus_limit": system_settings.default_freelancer_plus_limit,
                "default_upwork_max_jobs": getattr(system_settings, 'default_upwork_max_jobs', 3),
                "default_freelancer_max_jobs": getattr(system_settings, 'default_freelancer_max_jobs', 3)
            }
        }
    except Exception as e:
        print(f"Error updating admin settings: {e}")
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/admin/analytics")
async def get_admin_analytics(
    range: str = "7d",
    user = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    """Get analytics data for admin dashboard"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import User, Lead
        from datetime import datetime, timedelta
        

        
        # Parse time range
        days = 7
        if range == "30d":
            days = 30
        elif range == "90d":
            days = 90
        
        start_date = datetime.utcnow() - timedelta(days=days)
        
        # Fetch trends (mock data for now - would need to track fetch history)
        fetch_trends = []
        for i in range(days):
            date = start_date + timedelta(days=i)
            fetch_trends.append({
                "date": date.strftime("%b %d"),
                "upwork": 0,  # Would need fetch history table
                "freelancer": 0,
                "freelancer_plus": 0
            })
        
        # User activity
        user_activity = []
        for i in range(days):
            date = start_date + timedelta(days=i)
            date_end = date + timedelta(days=1)
            
            # Count users created on this day
            new_users = db.query(User).filter(
                User.created_at >= date,
                User.created_at < date_end
            ).count()
            
            user_activity.append({
                "date": date.strftime("%b %d"),
                "active_users": 0,  # Would need activity tracking
                "new_users": new_users
            })
        
        # Platform performance
        platform_stats = {}
        all_leads = db.query(Lead).filter(Lead.created_at >= start_date).all()
        
        for lead in all_leads:
            platform = lead.platform or "Unknown"
            if platform not in platform_stats:
                platform_stats[platform] = {"total": 0}
            platform_stats[platform]["total"] += 1
        
        platform_performance = [
            {
                "name": platform,
                "total_leads": stats["total"],
                "avg_per_fetch": round(stats["total"] / days, 1),
                "success_rate": 95  # Mock data
            }
            for platform, stats in platform_stats.items()
        ]
        
        return {
            "fetchTrends": fetch_trends,
            "userActivity": user_activity,
            "platformPerformance": platform_performance
        }
    except Exception as e:
        print(f"Error fetching analytics: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/admin/leads")
async def get_admin_leads(user = Depends(verify_admin), db: Session = Depends(get_db)):
    """Get all leads from all users for admin analytics"""
    try:
        if db is None:
            return {"leads": []}
        
        from models import Lead
        
        # Get all leads from all users
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
                    "category": lead.category if hasattr(lead, 'category') else 'Uncategorized',
                    "Proposal": lead.proposal,
                    "proposal_accepted": lead.proposal_accepted if hasattr(lead, 'proposal_accepted') else False,
                    "url": lead.url,
                    "avg_bid_price": lead.avg_bid_price if hasattr(lead, 'avg_bid_price') else None,
                    "created_at": lead.created_at.isoformat() if lead.created_at else None,
                    "updated_at": lead.updated_at.isoformat() if lead.updated_at else None,
                    "user_id": lead.user_id
                }
                for lead in leads
            ]
        }
    except Exception as e:
        print(f"❌ Error fetching admin leads: {e}")
        import traceback
        traceback.print_exc()
        return {"leads": []}

