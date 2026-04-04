from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, Depends, status, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func, text, Float, case
from typing import List, Optional
from datetime import datetime, timedelta
import httpx
import os
from dotenv import load_dotenv
from functools import lru_cache
import asyncio
from concurrent.futures import ThreadPoolExecutor
from cache_utils import cached, cleanup_cache
import threading
import time
from schemas import UserSignup, UserLogin, Token, UserResponse, SettingsUpdate, SettingsResponse, UserProfileUpdate, TalentCreate, TalentUpdate, TalentResponse, FreelancerCredentialsCreate, FreelancerCredentialsResponse, FreelancerCredentialsUpdate, AutoBidSettings, ClosedDealCreate, ClosedDealUpdate, ClosedDealResponse
from autobid_service import bidder as autobidder
from auth_utils import get_password_hash, verify_password, create_access_token, verify_token, SECRET_KEY, ALGORITHM
import json
from urllib.parse import unquote
import re

from core.utils import start_cache_cleanup

load_dotenv()

app = FastAPI()

@app.get("/api/test-server")
async def test_server():
    return {"status": "ok", "message": "Backend is running and accessible"}

# Move CORS to the top to ensure it wraps all routes and handlers
# When allow_credentials=True, allow_origins cannot be ["*"]
origins = [
    "https://akdropservicing.netlify.app",
    "https://akindustries.qubeknit.com",
    "http://localhost:5173",
    "http://localhost:3000",
    "http://localhost:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Global exception handler to capture and return more detailed errors for debugging
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print(f"🔥 UNCAUGHT EXCEPTION: {str(exc)}")
    import traceback
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "error": str(exc)},
    )

from fastapi.responses import JSONResponse


from routers.leads import router as leads_router
app.include_router(leads_router)
from routers.sync import router as sync_router
app.include_router(sync_router)
from routers.autobid import router as autobid_router
app.include_router(autobid_router)
from routers.fetch import router as fetch_router
app.include_router(fetch_router)
from routers.users import router as users_router
app.include_router(users_router)
from routers.chat import router as chat_router
app.include_router(chat_router)
from routers.auth import router as auth_router
app.include_router(auth_router)
from routers.debug import router as debug_router
app.include_router(debug_router)
from routers.health import router as health_router
app.include_router(health_router)
from routers.upwork import router as upwork_router
app.include_router(upwork_router)
from routers.guru import router as guru_router
app.include_router(guru_router)




# Start cache cleanup task


# Start services on startup
@app.on_event("startup")
async def startup_event():
    start_cache_cleanup()
    # Auto-bidder settings are now loaded lazily when the first bid fires,
    # avoiding a DB hit on every cold start (which happens every few minutes on Vercel).
    print("✅ AK BPO backend started")

@app.on_event("shutdown")
async def shutdown_event():
    autobidder.stop()



# Lazy import database to avoid connection on startup





# CORSMiddleware moved to the top

# Add performance middleware
from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Add response time header middleware
@app.middleware("http")
async def add_process_time_header(request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response















@lru_cache(maxsize=100)
def get_dashboard_stats_cached(user_id: int, cache_key: str):
    """Cached dashboard stats - cache_key includes timestamp for cache invalidation"""
    from database import SessionLocal
    from models import Lead
    
    db = SessionLocal()
    try:
        # Use optimized queries with database aggregation
        base_query = db.query(Lead).filter(Lead.user_id == user_id, Lead.visible == True)
        
        # Get counts using database aggregation
        total_leads = base_query.count()
        ai_drafted = base_query.filter(Lead.status == "AI Drafted").count()
        approved = base_query.filter(Lead.proposal_accepted == True).count()
        
        # Get low score count with database query
        low_score = base_query.filter(
            Lead.score.notin_(['—', '', None]),
            func.cast(Lead.score, Float) < 7
        ).count()
        
        # Platform distribution using database aggregation
        platform_stats = db.query(
            Lead.platform,
            func.count(Lead.id).label('count')
        ).filter(
            Lead.user_id == user_id,
            Lead.visible == True
        ).group_by(Lead.platform).all()
        
        total_with_platform = sum(stat.count for stat in platform_stats)
        platform_distribution = [
            {
                "name": stat.platform or "Unknown",
                "value": round((stat.count / total_with_platform * 100), 1) if total_with_platform > 0 else 0,
                "count": stat.count
            }
            for stat in platform_stats
        ]
        
        # Timeline data (last 30 days) using database aggregation
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        timeline_stats = db.query(
            func.date(Lead.created_at).label('date'),
            func.count(Lead.id).label('total'),
            func.sum(case((Lead.status.in_(["AI Drafted", "Approved"]), 1), else_=0)).label('proposals')
        ).filter(
            Lead.user_id == user_id,
            Lead.visible == True,
            Lead.created_at >= thirty_days_ago
        ).group_by(func.date(Lead.created_at)).order_by(func.date(Lead.created_at)).all()
        
        timeline_data = [
            {
                "date": stat.date.strftime("%b %d") if stat.date else "Unknown",
                "total": int(stat.total),
                "proposals": int(stat.proposals or 0)
            }
            for stat in timeline_stats
        ]
        
        if not timeline_data:
            timeline_data = [{"date": datetime.utcnow().strftime("%b %d"), "total": 0, "proposals": 0}]
        
        return {
            "total_leads": total_leads,
            "ai_drafted": ai_drafted,
            "low_score": low_score,
            "approved": approved,
            "platform_distribution": platform_distribution,
            "timeline_data": timeline_data
        }
    finally:
        db.close()





















# User Profile endpoints



# Notification endpoints






# Admin endpoints









# AutoBidder Endpoints








# Talent endpoints





# Freelancer Extension API Endpoints
from pydantic import BaseModel
from typing import Optional













# Freelancer Credentials endpoints




# Freelancer API endpoints for frontend integration


















# Helper function to prepare headers and cookies for Freelancer API calls

# Extension integration endpoints







# CRM Endpoints




