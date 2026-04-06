import asyncio
import logging
from datetime import datetime, timedelta
import random
import json
import time
from typing import Optional, Dict, Any, List
import httpx

logger = logging.getLogger("AutoBidder")

class AutoBidderCoreMixin:
    def update_settings(self, new_settings: Dict[str, Any]):
        """Update settings - now handled per user in database"""
        logger.info(f"AutoBidder settings update requested: {new_settings}")
        # Settings are now stored per-user in database, not globally
        
        # Don't automatically start here - let manual start handle it
        logger.info("Settings updated. Use start() method to begin auto-bidding.")

    def get_settings(self):
        """Get settings - now returns empty as settings are per-user in database"""
        return {}

    def set_user_context(self, user_id: int):
        """Set the current user context for the auto-bidder (Legacy support)"""
        # Kept for compatibility but mostly unused in multi-user mode
        pass

    def start(self):
        if self._is_running:
            logger.info("AutoBidder Service already running, ignoring start request")
            return
        
        self._is_running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("AutoBidder Service Started")

    def stop(self):
        if not self._is_running:
            logger.info("AutoBidder Service already stopped, ignoring stop request")
            return
            
        self._is_running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("AutoBidder Service Stopped")

    async def _bid_on_project(self, user_id: int, project: Dict, settings: Dict):
        """Place a REAL bid on Freelancer.com with AI-generated proposal and skill validation"""
        logger.info(f"\n💼 User {user_id}: BIDDING ON PROJECT")
        
        try:
            title = project.get("title", "Unknown")
            project_id = project.get("id")
            
            # Step 0: Skill validation is now handled in _filter_projects() 
            # This redundant ID-based validation was too strict and caused false rejections
            # The name-based skill matching in _filter_projects is more accurate
            # logger.info(f"🔍 User {user_id}: Validating skills for project...")
            # if not await self._validate_user_skills_for_project(user_id, project):
            #     logger.error(f"❌ User {user_id}: SKILL VALIDATION FAILED - Minimum skill match not met.")
            #     
            #     # Save to bid history to prevent retry
            #     await self._save_bid_history({
            #         "user_id": user_id,
            #         "project_id": str(project_id),
            #         "project_title": title,
            #         "project_url": f"https://www.freelancer.com/projects/{project.get('seo_url', project_id)}",
            #         "bid_amount": 0,
            #         "proposal_text": "Skill validation failed",
            #         "status": "insufficient_skill_match",
            #         "error_message": "Minimum skill match not met."
            #     })
            #     return "INSUFFICIENT_SKILL_MATCH"
            # 
            # logger.info(f"✅ User {user_id}: Skill validation passed")
            
            # Calculate bid amount FIRST
            budget = project.get("budget", {})
            min_budget = budget.get("minimum", 50)
            max_budget = budget.get("maximum", min_budget)
            
            if settings.get("smart_bidding"):
                bid_amount = (min_budget + max_budget) / 2
            else:
                bid_amount = min_budget

            bid_amount = round(bid_amount, 2)
            
            logger.info(f"💰 User {user_id}: Calculated bid amount: ${bid_amount}")

            # Step 1: Generate AI proposal
            try:
                proposal = await self._generate_proposal(user_id, project, bid_amount)
            except Exception as e:
                logger.info("⏭️  Skipping this project")
                return False
            
            # Step 2: Get Freelancer credentials
            from database import SessionLocal
            from models import FreelancerCredentials
            import json
            
            db = SessionLocal()
            try:
                credentials = db.query(FreelancerCredentials).filter(
                    FreelancerCredentials.user_id == user_id
                ).first()
                
                if not credentials:
                    raise Exception("No credentials found")
                
                cookies_dict = {}
                csrf_token = None
                
                if credentials.cookies:
                    cookie_data = credentials.cookies if isinstance(credentials.cookies, dict) else json.loads(credentials.cookies)
                    
                    user_id_cookie = cookie_data.get('GETAFREE_USER_ID')
                    auth_hash = cookie_data.get('GETAFREE_AUTH_HASH_V2')
                    csrf_token = cookie_data.get('XSRF_TOKEN')
                    session2 = cookie_data.get('session2')
                    
                    if not user_id_cookie or not auth_hash or not session2:
                        raise Exception("Missing required cookies")
                        
                    cookies_dict = {
                        "GETAFREE_USER_ID": user_id_cookie,
                        "GETAFREE_AUTH_HASH_V2": auth_hash,
                        "session2": session2
                    }
                    if cookie_data.get('qfence'):
                         cookies_dict['qfence'] = cookie_data['qfence']
                
                # Step 3: Build headers and payload
                headers = self._get_freelancer_headers(
                    project, user_id_cookie, auth_hash, csrf_token, 
                    credentials.access_token or ""
                )
                
                bid_payload = self._build_bid_payload(
                    user_id, project, bid_amount, proposal, user_id_cookie
                )
                
                # Step 4: Submit bid
                logger.info(f"📤 User {user_id}: Sending bid request...")
                response = await self._submit_bid(headers, cookies_dict, bid_payload)
                
                # Step 5: Handle response
                result = await self._handle_bid_response(user_id, project, bid_amount, proposal, response)
                
                # Return appropriate values based on result
                if result == "SUCCESS":
                    return True
                elif result in ["CREDENTIALS_EXPIRED", "BID_LIMIT_REACHED"]:
                    return result
                else:
                    return False
                    
            finally:
                db.close()

        except Exception as e:
            logger.error(f"❌ ERROR BIDDING ON PROJECT for User {user_id}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    async def _handle_bid_response(self, user_id: int, project: Dict, bid_amount: float, proposal: str, response: httpx.Response) -> str:
        """Handle the response from bid submission"""
        project_id = project.get("id")
        title = project.get("title", "Unknown")
        
        logger.info(f"📨 Response: {response.status_code}")
        
        if response.status_code == 200 or response.status_code == 201:
            try:
                response_data = response.json()
                if response_data.get("status") == "error":
                    error_message = response_data.get("message", "Unknown error")
                    logger.error(f"❌ User {user_id}: Freelancer returned error: {error_message}")
                    
                    # Check if it's "already bid" error - DON'T save to history, just return
                    if "already bid" in error_message.lower() or "you have already bid" in error_message.lower():
                        logger.info(f"⏭️  User {user_id}: Already bid error detected, skipping to next project (not saving to history)")
                        return "ALREADY_BID"
                    
                    # Save other errors to history to avoid retry
                    await self._save_bid_history({
                        "user_id": user_id,
                        "project_id": str(project_id),
                        "project_title": title,
                        "project_url": f"https://www.freelancer.com/projects/{project.get('seo_url', project_id)}",
                        "bid_amount": bid_amount,
                        "proposal_text": proposal,
                        "status": "failed",
                        "error_message": error_message
                    })
                    return "ERROR"
                
                logger.info(f"✅ User {user_id}: BID PLACED SUCCESSFULLY!")
                logger.info(f"   Bid ID: {response_data.get('result', {}).get('id', 'N/A')}")
                
            except Exception as e:
                logger.warning(f"⚠️  Could not parse response: {e}")
            
            # Save successful bid to history
            await self._save_bid_history({
                "user_id": user_id,
                "project_id": str(project_id),
                "project_title": title,
                "project_url": f"https://www.freelancer.com/projects/{project.get('seo_url', project_id)}",
                "bid_amount": bid_amount,
                "proposal_text": proposal,
                "status": "success"
            })
            return "SUCCESS"
        
        else:
            error_text = response.text
            logger.error(f"❌ User {user_id}: Bid failed: {response.status_code}")
            
            # Handle 401 Unauthorized (expired credentials)
            if response.status_code == 401:
                logger.error(f"🔐 User {user_id}: Freelancer credentials EXPIRED (401) during bidding")
                await self._mark_credentials_expired(user_id)
                return "CREDENTIALS_EXPIRED"
            
            try:
                error_data = response.json()
                error_message = error_data.get('message') or error_data.get('error') or str(error_data)
            except:
                error_message = error_text or f"HTTP {response.status_code}"
            
            # Check for specific error types
            if "already bid" in error_message.lower() or "you have already bid" in error_message.lower():
                logger.info(f"⏭️  User {user_id}: Already bid error detected, skipping to next project (not saving to history)")
                return "ALREADY_BID"
            
            if "used all of your bids" in error_message.lower() or "you have used all your bids" in error_message.lower() or "daily limit" in error_message.lower():
                logger.error(f"🚫 User {user_id}: Used all available bids or reached daily limit! Temporarily disabling auto-bidding...")
                await self._disable_user_autobidding(user_id)
                return "BID_LIMIT_REACHED"
            
            if "restricted from bidding" in error_message.lower() or "is restricted" in error_message.lower():
                logger.error(f"⛔ User {user_id}: Account is RESTRICTED from bidding by Freelancer.com! Disabling auto-bidding...")
                await self._disable_user_autobidding(user_id)
                return "ACCOUNT_RESTRICTED"
            
            # Save ALL other errors to history to prevent retry (but NOT already_bid errors)
            await self._save_bid_history({
                "user_id": user_id,
                "project_id": str(project_id),
                "project_title": title,
                "project_url": f"https://www.freelancer.com/projects/{project.get('seo_url', project_id)}",
                "bid_amount": bid_amount,
                "proposal_text": proposal,
                "status": "failed",
                "error_message": error_message
            })
            return "ERROR"

