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

@router.get("/")
async def root():
    """Fast root endpoint with minimal processing"""
    return {"status": "ok", "service": "akbpo-api", "version": "1.0"}

@router.get("/api/health")
async def health_check():
    """Fast health check with caching"""
    return {
        "status": "running",
        "database": _check_db_status(),
        "timestamp": int(time.time())
    }

