from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, Depends, status, Query, Request
from fastapi.responses import JSONResponse
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
from truelancer_autobid_service import truelancer_bidder
from guru_autobid_service import guru_bidder
from autobidder.upwork_bidder import upwork_bidder
from auth_utils import get_password_hash, verify_password, create_access_token, verify_token, SECRET_KEY, ALGORITHM

import json
from urllib.parse import unquote
import re

from core.utils import start_cache_cleanup

load_dotenv()

app = FastAPI()

# --- MIDDLEWARE (OUTERMOST FIRST) ---

# 1. CORS Middleware - MUST BE FIRST to catch preflight (OPTIONS) requests correctly
# When allow_credentials=True, allow_origins cannot be ["*"]
origins = [
    "https://akdropservicing.netlify.app",
    "https://akindustries.qubeknit.com",
    "http://localhost:5173",
    "http://localhost:3000",
    "http://localhost:8000",
    "chrome-extension://mejgjedpahjpkiangphnccdapnimhgne",
    "chrome-extension://bkjdkeipolippbigickoolgceahhgkke"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"chrome-extension://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

@app.get("/api/test-server")
async def test_server():
    return {"status": "ok", "message": "Backend is running and accessible"}

@app.get("/api/routes")
async def list_routes():
    url_list = [{"path": route.path, "name": route.name, "methods": list(route.methods) if hasattr(route, "methods") else []} for route in app.routes]
    return {"total": len(url_list), "routes": url_list}

# Global exception handler to capture and return more detailed errors for debugging
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    error_msg = str(exc)
    print(f"🔥 UNCAUGHT EXTERNAL EXCEPTION: {error_msg}")
    import traceback
    traceback.print_exc()
    
    # Manually add CORS headers to error response if middleware hasn't added them
    origin = request.headers.get("origin")
    headers = {}
    if origin in origins:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
    elif origin and (origin.startswith("chrome-extension://") or "netlify.app" in origin):
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"

    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal Server Error", 
            "error": error_msg,
            "type": type(exc).__name__,
            "path": request.url.path
        },
        headers=headers
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
from routers.truelancer import router as truelancer_router
app.include_router(truelancer_router)




# Start cache cleanup task


# Start services on startup
@app.on_event("startup")
async def startup_event():
    start_cache_cleanup()
    # Auto-bidder settings are now loaded lazily when the first bid fires,
    # avoiding a DB hit on every cold start (which happens every few minutes on Vercel).
    guru_bidder.start()
    print("✅ AK BPO backend started")

@app.on_event("shutdown")
async def shutdown_event():
    autobidder.stop()
    upwork_bidder.stop()
    truelancer_bidder.stop()
    guru_bidder.stop()




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




