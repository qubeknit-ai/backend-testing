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
                results = await self.run_cycle_batch()
                # Wait 5 minutes between cycles
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
            # Fetch ALL enabled Truelancer auto-bid settings
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
                
                # Small stagger
                await asyncio.sleep(random.uniform(0.5, 1.5) if 'random' in globals() else 0.5)
            
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

                for project in projects:
                    project_id = str(project.get("id"))
                    
                    exists = db.query(BidHistory).filter(
                        BidHistory.user_id == user_id,
                        BidHistory.project_id == project_id,
                        BidHistory.platform == "truelancer"
                    ).first()
                    
                    if exists:
                        continue
                    
                    bid_count = project.get("bid_count", 0)
                    if bid_count > setting.max_competition:
                        continue
                    
                    user = db.query(User).filter(User.id == user_id).first()
                    proposal_text = await self._generate_proposal(user, project)
                    if not proposal_text:
                        continue
                    
                    budget = project.get("budget", {})
                    min_amt = budget.get("min", 100)
                    max_amt = budget.get("max", min_amt)
                    amount = (min_amt + max_amt) / 2 if setting.smart_bidding else min_amt
                    
                    success = await self._place_bid(creds, project, proposal_text, amount)
                    if success:
                        history = BidHistory(
                            user_id=user_id,
                            project_id=project_id,
                            project_title=project.get("title", "Truelancer Project"),
                            project_url=f"https://www.truelancer.com/freelance-project/{project.get('id')}",
                            bid_amount=float(amount),
                            proposal_text=proposal_text,
                            status="success",
                            platform="truelancer"
                        )
                        db.add(history)
                        db.commit()
                        return True
                    
            return "No suitable new projects found (filtered by history or competition)"
        except Exception as e:
            logger.error(f"Error in User {user_id} cycle: {e}")
            return f"Error: {str(e)}"
        finally:
            db.close()

    async def _generate_proposal(self, user, project):
        webhook_url = os.getenv("FREELANCER_PROPOSAL_WEBHOOK_URL")
        if not webhook_url:
            return None
        payload = {
            "user_id": user.id,
            "user_email": user.email,
            "project": {
                "id": project.get("id"),
                "title": project.get("title"),
                "description": project.get("description"),
                "budget": project.get("budget", {}),
                "skills": project.get("skills", []),
            }
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(webhook_url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("proposal")
        except:
            pass
        return None

    async def _place_bid(self, creds, project, proposal, amount):
        payload = {
            'proposal[job_id]': str(project.get("id")),
            'proposal[job_currency]': project.get("budget", {}).get("currency", "USD"),
            'proposal[item]': '13',
            'proposal[details]': proposal,
            'proposal[total_amount]': str(amount),
            'proposal[deposit_amount]': str(amount),
            'proposal[notify_freelancer]': '1'
        }
        headers = {"Authorization": f"Bearer {creds.access_token}", "Accept": "application/json"}
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
                return response.status_code == 200 and response.json().get("status") == 1
        except:
            pass
        return False

truelancer_bidder = TruelancerAutoBidder()
