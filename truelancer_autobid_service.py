import asyncio
import logging
import httpx
import os
import json
import time
import random
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from database import SessionLocal
from models import User, TruelancerCredentials, TruelancerAutoBidSettings, BidHistory

logger = logging.getLogger("TruelancerAutoBidder")

class TruelancerAutoBidder:
    _instance = None
    _is_running = False
    _task = None
    _user_last_bid_time = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(TruelancerAutoBidder, cls).__new__(cls)
        return cls._instance

    def start(self):
        if self._is_running:
            logger.info("Truelancer AutoBidder Service already running")
            return
        self._is_running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Truelancer AutoBidder Service Started")

    def stop(self):
        if not self._is_running:
            return
        self._is_running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Truelancer AutoBidder Service Stopped")

    async def _loop(self):
        """Main bidding loop for Truelancer"""
        logger.info("Truelancer AutoBidder Loop Initiated")
        while self._is_running:
            try:
                await self.run_cycle_batch()
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Truelancer loop: {e}")
                await asyncio.sleep(60)

    async def run_cycle_batch(self):
        """Execute one parallel bidding cycle for all enabled Truelancer users"""
        logger.info("Truelancer AutoBidder Cycle Batch Initiated")
        
        db = SessionLocal()
        results_summary = {
            "total_enabled_users": 0,
            "active_users": [],
            "successful_bids": 0,
            "failed_users": 0,
            "skipped_users": 0,
            "details": {},
            "timestamp": datetime.now().isoformat()
        }
        
        try:
            enabled_settings = db.query(TruelancerAutoBidSettings).filter(
                TruelancerAutoBidSettings.enabled == True
            ).all()
            
            results_summary["total_enabled_users"] = len(enabled_settings)
            
            if not enabled_settings:
                return results_summary
            
            tasks = []
            for setting in enabled_settings:
                user_id = setting.user_id
                
                # Check frequency limits
                last_bid = self._user_last_bid_time.get(user_id)
                if last_bid:
                    minutes_since = (datetime.now() - last_bid).total_seconds() / 60
                    if minutes_since < setting.frequency_minutes:
                        results_summary["skipped_users"] += 1
                        results_summary["details"][user_id] = f"Frequency limit: {minutes_since:.1f}m passed, need {setting.frequency_minutes}m"
                        continue
                
                # Create task for parallel execution
                task = asyncio.create_task(self._run_user_cycle(user_id, setting))
                tasks.append((user_id, task))
                await asyncio.sleep(random.uniform(0.5, 1.5))
            
            if tasks:
                results = await asyncio.gather(*[t for _, t in tasks], return_exceptions=True)
                
                for i, (user_id, _) in enumerate(tasks):
                    result = results[i]
                    if isinstance(result, Exception):
                        logger.error(f"❌ User {user_id}: Exception: {result}")
                        results_summary["failed_users"] += 1
                        results_summary["details"][user_id] = f"Error: {str(result)}"
                    elif result is True:
                        self._user_last_bid_time[user_id] = datetime.now()
                        results_summary["successful_bids"] += 1
                        results_summary["active_users"].append(user_id)
                        results_summary["details"][user_id] = "Success"
                    else:
                        results_summary["skipped_users"] += 1
                        results_summary["details"][user_id] = result or "Unknown skip"
            
            return results_summary
            
        except Exception as e:
            logger.error(f"Error in Truelancer run_cycle_batch: {e}")
            return results_summary
        finally:
            db.close()

    async def _run_user_cycle(self, user_id, setting):
        """Run cycle for a single user"""
        db = SessionLocal()
        try:
            creds = db.query(TruelancerCredentials).filter(TruelancerCredentials.user_id == user_id).first()
            if not creds or not creds.access_token:
                return "Truelancer not connected"
            
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            bids_today = db.query(BidHistory).filter(
                BidHistory.user_id == user_id,
                BidHistory.platform == "truelancer",
                BidHistory.created_at >= today_start,
                BidHistory.status == "success"
            ).count()
            
            if bids_today >= setting.daily_bids:
                return f"Daily limit reached ({bids_today}/{setting.daily_bids})"

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.truelancer.com/api/v1/projects",
                    headers={
                        "Authorization": f"Bearer {creds.access_token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json"
                    },
                    json={
                        "page": 1,
                        "per_page": 20,
                        "sort": "newest",
                        "skill_matching": True
                    }
                )
                
                if response.status_code != 200:
                    return f"API Error: {response.status_code}"
                
                data = response.json()
                projects = data.get("projects", {}).get("data", []) or data.get("data", [])
                
                if not projects:
                    return "No recommended jobs found"

                skip_counts = {"history": 0, "proposal_fail": 0, "bid_fail": 0, "too_old": 0}
                for project in projects:
                    project_id = str(project.get("id"))
                    
                    # 1. History check
                    exists = db.query(BidHistory).filter(
                        BidHistory.user_id == user_id,
                        BidHistory.project_id == project_id,
                        BidHistory.platform == "truelancer"
                    ).first()
                    
                    if exists:
                        skip_counts["history"] += 1
                        continue

                    # 2. Age check (No older than 48 hours)
                    created_at_str = project.get("created_at")
                    if created_at_str:
                        try:
                            p_date = None
                            # Truelancer usually returns "YYYY-MM-DD HH:MM:SS"
                            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S"):
                                try:
                                    p_date = datetime.strptime(created_at_str, fmt)
                                    break
                                except:
                                    continue
                            
                            if p_date:
                                # Compare with UTC now
                                if datetime.utcnow() - p_date > timedelta(hours=48):
                                    skip_counts["too_old"] += 1
                                    continue
                        except Exception as e:
                            logger.warning(f"Could not parse project date {created_at_str}: {e}")
                    
                    # 3. Proposal generation
                    user_obj = db.query(User).filter(User.id == user_id).first()
                    logger.info(f"🧠 User {user_id}: Generating proposal for '{project.get('title')}'")
                    proposal_text = await self._generate_proposal(user_obj, project)
                    
                    if not proposal_text:
                        skip_counts["proposal_fail"] += 1
                        continue
                    
                    # 4. Budget parsing
                    budget_val = project.get("budget", 0)
                    if isinstance(budget_val, dict):
                        min_amt = budget_val.get("min", 100)
                        max_amt = budget_val.get("max", min_amt)
                        amount = (min_amt + max_amt) / 2 if setting.smart_bidding else min_amt
                    else:
                        amount = float(budget_val) if budget_val else 100
                    
                    # 5. Place bid
                    success = await self._place_bid(creds, project, proposal_text, amount)
                    if success:
                        history = BidHistory(
                            user_id=user_id,
                            project_id=project_id,
                            project_title=project.get("title", "Truelancer Project"),
                            project_url=project.get("link") or f"https://www.truelancer.com/freelance-project/{project.get('slug')}",
                            bid_amount=float(amount),
                            proposal_text=proposal_text,
                            status="success",
                            platform="truelancer"
                        )
                        db.add(history)
                        db.commit()
                        logger.info(f"✅ User {user_id}: Successfully bid on '{project.get('title')}'")
                        return True
                    else:
                        skip_counts["bid_fail"] += 1
                
                # If we reach here, no bid was placed
                reasons = []
                if skip_counts["history"] > 0: reasons.append(f"{skip_counts['history']} bidded")
                if skip_counts["too_old"] > 0: reasons.append(f"{skip_counts['too_old']} too old (>48h)")
                if skip_counts["proposal_fail"] > 0: reasons.append(f"{skip_counts['proposal_fail']} AI fail")
                if skip_counts["bid_fail"] > 0: reasons.append(f"{skip_counts['bid_fail']} API fail")
                
                if skip_counts["too_old"] == len(projects):
                    return "Failed: No projects found within the last 48 hours."
                
                return f"No bids placed. Reasons: {', '.join(reasons)}"

        except Exception as e:
            logger.error(f"Error in User {user_id} cycle: {e}")
            return f"Error: {str(e)}"
        finally:
            db.close()

    async def _generate_proposal(self, user, project):
        webhook_url = os.getenv("FREELANCER_PROPOSAL_WEBHOOK_URL")
        if not webhook_url:
            return None
            
        # Prepare payload EXACTLY like the backend endpoint /api/truelancer/generate-proposal
        payload = {
            "user_id": user.id,
            "user_email": user.email,
            "project": {
                "id": project.get("id"),
                "title": project.get("title"),
                "description": project.get("description"),
                "preview_description": project.get("preview_description", ""),
                "url": project.get("link") or f"https://www.truelancer.com/freelance-project/{project.get('slug')}",
                "budget": {
                    "amount": project.get("budget"),
                    "currency": project.get("currency_symbol") or project.get("currency_code") or "USD"
                },
                "posted_time": project.get("created_at"),
                "bid_count": project.get("total_proposals", 0),
                "skills": project.get("skills", []),
                "client": {
                    "name": project.get("fname", ""),
                    "country": project.get("country_code", "")
                }
            }
        }
        
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv("N8N_WEBHOOK_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key
            
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(webhook_url, json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        return data[0].get("proposal") or data[0].get("Proposal")
                    elif isinstance(data, dict):
                        if data.get("data"):
                            return data["data"].get("proposal") or data["data"].get("Proposal")
                        return data.get("proposal") or data.get("Proposal")
        except Exception as e:
            logger.error(f"Webhook error: {e}")
        return None

    async def _place_bid(self, creds, project, proposal, amount):
        payload = {
            'proposal[job_id]': str(project.get("id")),
            'proposal[job_currency]': project.get("currency_code", creds.currency or "USD"),
            'proposal[item]': '13',
            'proposal[details]': proposal,
            'proposal[total_amount]': str(amount),
            'proposal[deposit_amount]': str(amount),
            'proposal[notify_freelancer]': '1'
        }
        headers = {
            "Authorization": f"Bearer {creds.access_token}",
            "Accept": "application/json"
        }
        cookies = creds.cookies if isinstance(creds.cookies, dict) else {}
        files_payload = {k: (None, str(v)) for k, v in payload.items()}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.truelancer.com/api/v1/proposal/save",
                    headers=headers,
                    cookies=cookies,
                    files=files_payload
                )
                if response.status_code == 200:
                    data = response.json()
                    return data.get("status") == 1
                else:
                    logger.error(f"Truelancer API Error: {response.status_code} {response.text}")
        except Exception as e:
            logger.error(f"Placement error: {e}")
        return False

truelancer_bidder = TruelancerAutoBidder()
