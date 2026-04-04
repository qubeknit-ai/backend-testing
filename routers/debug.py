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

@router.get("/api/debug/db")
async def debug_db():
    """Debug endpoint to check database connection"""
    try:
        from database import SessionLocal
        from models import User
        
        db = SessionLocal()
        
        # Test basic connection
        result = db.execute(text("SELECT 1 as test")).fetchone()
        print(f"🔍 [DEBUG_DB] Basic query result: {result}")
        
        # Test user table
        user_count = db.query(User).count()
        print(f"🔍 [DEBUG_DB] User count: {user_count}")
        
        db.close()
        
        return {
            "status": "ok", 
            "basic_query": result[0] if result else None,
            "user_count": user_count,
            "database_url": os.getenv("DATABASE_URL", "Not set")[:50] + "..." if os.getenv("DATABASE_URL") else "Not set"
        }
    except Exception as e:
        print(f"🔍 [DEBUG_DB] Database error: {e}")
        return {"status": "error", "error": str(e)}
        if not auth_header:
            return {
                "error": "No authorization header found",
                "headers": dict(request.headers)
            }
        
        if not auth_header.startswith("Bearer "):
            return {
                "error": "Invalid authorization header format",
                "auth_header": auth_header,
                "expected_format": "Bearer <token>"
            }
        
        token = auth_header.split(" ")[1]
        print(f"🔍 [DEBUG_AUTH] Extracted token: {token[:30]}...")
        
        # Try to decode without verification first
        try:
            import jwt
            unverified_payload = jwt.decode(token, options={"verify_signature": False})
            print(f"🔍 [DEBUG_AUTH] Unverified payload: {unverified_payload}")
            
            return {
                "success": True,
                "token_format": "valid_jwt",
                "token_length": len(token),
                "token_parts": len(token.split('.')),
                "unverified_payload": unverified_payload,
                "secret_key_prefix": SECRET_KEY[:10],
                "algorithm": ALGORITHM
            }
        except Exception as decode_error:
            print(f"🔍 [DEBUG_AUTH] Decode error: {decode_error}")
            return {
                "error": "Could not decode token",
                "token_length": len(token),
                "token_parts": len(token.split('.')),
                "decode_error": str(decode_error),
                "secret_key_prefix": SECRET_KEY[:10],
                "algorithm": ALGORITHM
            }
            
    except Exception as e:
        print(f"🔍 [DEBUG_AUTH] General error: {e}")
        return {
            "error": "Debug failed",
            "exception": str(e)
        }

