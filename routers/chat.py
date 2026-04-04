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

@router.post("/api/chat")
async def send_chat_message(
    chat_data: dict,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """
    Send chat message to N8N webhook and save to database
    Expected payload: {
        "message": "user message",
        "lead_id": 123,
        "proposal": "proposal text",
        "description": "job description"
    }
    """
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import Lead, ChatMessage
        user = get_user_by_email(email, db)
        
        # Get lead_id from chat_data
        lead_id = chat_data.get("lead_id")
        if not lead_id:
            raise HTTPException(status_code=400, detail="lead_id is required")
        
        # Verify lead ownership
        lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found or access denied")
        
        user_message = chat_data.get("message", "")
        
        # Save user message to database
        user_chat_message = ChatMessage(
            user_id=user.id,
            lead_id=lead_id,
            message=user_message,
            sender="user"
        )
        db.add(user_chat_message)
        db.commit()
        
        webhook_url = os.getenv("CHAT_WEBHOOK_URL")
        if not webhook_url:
            raise HTTPException(status_code=500, detail="CHAT_WEBHOOK_URL not configured")
        
        # Prepare payload for N8N
        payload = {
            "user_id": user.id,
            "user_email": user.email,
            "lead_id": lead_id,
            "message": user_message,
            "proposal": chat_data.get("proposal", lead.proposal),
            "description": chat_data.get("description", lead.description),
            "title": lead.title,
            "platform": lead.platform
        }
        
        # Prepare headers with authentication
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv("N8N_WEBHOOK_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key
        
        print(f"Sending chat message to N8N for lead {lead_id}")
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                webhook_url,
                json=payload,
                headers=headers
            )
            
            print(f"Chat webhook response status: {response.status_code}")
            print(f"Chat webhook response: {response.text}")
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to send message to AI assistant"
                )
            
            # Parse N8N response format: [{"output": "..."}]
            try:
                ai_response = response.json()
                output_text = ""
                
                # Handle array response from N8N
                if isinstance(ai_response, list) and len(ai_response) > 0:
                    output_text = ai_response[0].get("output", "")
                # Handle direct object response
                elif isinstance(ai_response, dict):
                    output_text = ai_response.get("output", ai_response.get("response", "Response received"))
                else:
                    output_text = str(ai_response)
                
                # Save AI response to database
                ai_chat_message = ChatMessage(
                    user_id=user.id,
                    lead_id=lead_id,
                    message=output_text,
                    sender="ai"
                )
                db.add(ai_chat_message)
                db.commit()
                
                return {
                    "success": True,
                    "response": output_text
                }
            except Exception as e:
                print(f"Error parsing AI response: {e}")
                return {
                    "success": True,
                    "response": response.text
                }
                
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error sending chat message: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to send message")

@router.get("/api/chat/history/{lead_id}")
async def get_chat_history(
    lead_id: int,
    email: str = Depends(verify_token),
    db: Session = Depends(get_db)
):
    """Get chat history for a specific lead"""
    try:
        if db is None:
            raise HTTPException(status_code=500, detail="Database connection failed")
        
        from models import Lead, ChatMessage
        user = get_user_by_email(email, db)
        
        # Verify lead ownership
        lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found or access denied")
        
        # Get chat messages for this lead
        messages = db.query(ChatMessage).filter(
            ChatMessage.lead_id == lead_id,
            ChatMessage.user_id == user.id
        ).order_by(ChatMessage.created_at.asc()).all()
        
        return {
            "success": True,
            "messages": [
                {
                    "id": msg.id,
                    "text": msg.message,
                    "sender": msg.sender,
                    "timestamp": msg.created_at.isoformat()
                }
                for msg in messages
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error retrieving chat history: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to retrieve chat history")

