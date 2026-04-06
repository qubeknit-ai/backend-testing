import asyncio
import logging
from datetime import datetime, timedelta
import random
import json
import time
from typing import Optional, Dict, Any, List
import httpx

logger = logging.getLogger("AutoBidder")

class AutoBidderAIBidMixin:
    async def _generate_proposal(self, user_id: int, project: Dict, bid_amount: float) -> str:
        """Generate AI proposal for a project"""
        logger.info(f"🤖 User {user_id}: Generating proposal...")
        
        import os
        webhook_url = os.getenv("FREELANCER_PROPOSAL_WEBHOOK_URL")
        
        if not webhook_url:
            logger.warning("⚠️  Proposal webhook not configured")
            return f"I can help you with this project. My bid is ${bid_amount}."
        
        try:
            title = project.get("title", "Unknown")
            project_id = project.get("id")
            budget = project.get("budget", {})
            min_budget = budget.get("minimum", 50)
            max_budget = budget.get("maximum", min_budget)
            
            project_data = {
                "id": project_id,
                "title": title,
                "description": project.get("preview_description", project.get("description", "No description available")),
                "preview_description": project.get("preview_description", ""),
                "budget": {
                    "minimum": min_budget,
                    "maximum": max_budget,
                    "currency": "USD"
                },
                "bid_count": project.get("bid_stats", {}).get("bid_count", 0),
                "skills": [job.get("name") for job in project.get("jobs", [])] if project.get("jobs") else []
            }
            
            payload = {
                "user_id": user_id,
                "user_email": "autobidder@system",
                "project": project_data
            }
            
            headers = {"Content-Type": "application/json"}
            api_key = os.getenv("N8N_WEBHOOK_API_KEY")
            if api_key:
                headers["X-API-Key"] = api_key
            
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(webhook_url, json=payload, headers=headers)
                
                if response.status_code == 200:
                    data = response.json()
                    
                    # Try multiple possible response formats
                    proposal = None
                    if isinstance(data, dict):
                        proposal = (data.get("data", {}).get("proposal") or 
                                  data.get("proposal") or 
                                  data.get("result", {}).get("proposal") or
                                  data.get("output") or
                                  data.get("message") or
                                  data.get("text"))
                    elif isinstance(data, str):
                        proposal = data
                    
                    if not proposal or proposal.strip() == "":
                        logger.error(f"❌ User {user_id}: Empty proposal response")
                        raise Exception("Empty proposal")
                        
                    logger.info(f"✅ User {user_id}: Proposal generated ({len(proposal)} chars)")
                    return proposal
                else:
                    raise Exception(f"AI failed: {response.status_code}")
                    
        except Exception as e:
            logger.error(f"❌ User {user_id}: Error generating AI proposal: {e}")
            raise Exception(f"Failed to generate proposal: {e}")

