import asyncio
import logging
import httpx
import os
import json
import time
import random
import re
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from database import SessionLocal
from models import User, GuruCredentials, GuruAutoBidSettings, BidHistory
from core.dependencies import get_user_by_email

logger = logging.getLogger("GuruAutoBidder")

class GuruAutoBidder:
    _instance = None
    _is_running = False
    _task = None
    _user_last_bid_time = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(GuruAutoBidder, cls).__new__(cls)
        return cls._instance

    def start(self):
        if self._is_running:
            logger.info("Guru AutoBidder Service already running")
            return
        self._is_running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Guru AutoBidder Service Started")

    def stop(self):
        if not self._is_running:
            return
        self._is_running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Guru AutoBidder Service Stopped")

    async def _loop(self):
        """Main bidding loop for Guru"""
        logger.info("Guru AutoBidder Loop Initiated")
        while self._is_running:
            try:
                await self.run_cycle_batch()
                await asyncio.sleep(600) # Run every 10 minutes
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Guru loop: {e}")
                await asyncio.sleep(60)

    async def run_cycle_batch(self):
        """Execute one parallel bidding cycle for all enabled Guru users"""
        logger.info("Guru AutoBidder Cycle Batch Initiated")
        
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
            enabled_settings = db.query(GuruAutoBidSettings).filter(
                GuruAutoBidSettings.enabled == True
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
                await asyncio.sleep(random.uniform(1.0, 2.0)) # Stagger starts
            
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
            logger.error(f"Error in Guru run_cycle_batch: {e}")
            return results_summary
        finally:
            db.close()

    async def _run_user_cycle(self, user_id, setting):
        """Run cycle for a single Guru user"""
        db = SessionLocal()
        try:
            creds = db.query(GuruCredentials).filter(GuruCredentials.user_id == user_id).first()
            if not creds or not creds.is_validated:
                return "Guru not connected"
            
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            bids_today = db.query(BidHistory).filter(
                BidHistory.user_id == user_id,
                BidHistory.platform == "guru",
                BidHistory.created_at >= today_start,
                BidHistory.status == "success"
            ).count()
            
            if bids_today >= setting.daily_bids:
                return f"Daily limit reached ({bids_today}/{setting.daily_bids})"

            # Build headers and cookies
            cookies_dict = creds.cookies if isinstance(creds.cookies, dict) else {}
            access_token = creds.access_token or cookies_dict.get("_accessToken", "")
            csrf_token = creds.csrf_token or cookies_dict.get("__RequestVerificationToken", "")

            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://www.guru.com/work/",
                "Origin": "https://www.guru.com",
            }
            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"
            if csrf_token:
                headers["RequestVerificationToken"] = csrf_token

            async with httpx.AsyncClient(timeout=30.0) as client:
                # Fetch Recommended Jobs
                search_url = f"https://www.guru.com/api/search/job/?query=L2Qvam9icy8%3D&pageNumber=1&pageSize=20"
                response = await client.get(search_url, headers=headers, cookies=cookies_dict)
                
                if response.status_code != 200:
                    return f"API Error: {response.status_code}"
                
                data = response.json()
                raw_jobs = (
                    data.get("Data", {}).get("Results") or
                    data.get("result") or
                    data.get("results") or
                    data.get("jobs") or
                    []
                )
                
                projects = [self._normalize_guru_job(j) for j in raw_jobs]
                
                if not projects:
                    return "No recommended jobs found"

                skip_counts = {"history": 0, "proposal_fail": 0, "bid_fail": 0, "too_old": 0, "too_many_quotes": 0}
                fail_limit = 5
                current_fails = 0
                
                for project in projects:
                    if current_fails >= fail_limit:
                        logger.warning(f"🛑 [GURU-AUTOBID] User {user_id}: Hit failure limit ({fail_limit}). Skipping remaining projects in this cycle.")
                        break
                        
                    project_id = project.get("id")
                    
                    # 1. History check
                    exists = db.query(BidHistory).filter(
                        BidHistory.user_id == user_id,
                        BidHistory.project_id == project_id,
                        BidHistory.platform == "guru"
                    ).first()
                    
                    if exists:
                        skip_counts["history"] += 1
                        continue

                    # 1.5 Quote check
                    max_quotes = getattr(settings, 'max_quotes', 50)
                    if project.get("total_proposals", 0) > max_quotes:
                        skip_counts["too_many_quotes"] = skip_counts.get("too_many_quotes", 0) + 1
                        continue

                    # 2. Age check (Respect dynamic settings)
                    max_hours = getattr(settings, 'max_project_age_hours', 48)
                    posted_at_str = project.get("posted_at")
                    if posted_at_str:
                        try:
                            # Our normalization now returns ISO strings
                            p_date = datetime.fromisoformat(posted_at_str)
                            if datetime.utcnow() - p_date > timedelta(hours=max_hours):
                                skip_counts["too_old"] += 1
                                continue
                        except:
                            pass
                    
                    # 3. Proposal generation
                    user_obj = db.query(User).filter(User.id == user_id).first()
                    logger.info(f"🧠 Guru User {user_id}: Generating proposal for '{project.get('title')}'")
                    proposal_text = await self._generate_proposal(user_obj, project)
                    
                    if not proposal_text:
                        skip_counts["proposal_fail"] += 1
                        continue
                    
                    # 4. Budget calculation
                    budget_val = project.get("budget", 100)
                    amount = budget_val # Guru usually has single values or ranges already handled in normalization
                    
                    # 5. Place bid
                    success = await self._place_bid(creds, project, proposal_text, amount)
                    if success:
                        history = BidHistory(
                            user_id=user_id,
                            project_id=project_id,
                            project_title=project.get("title", "Guru Project"),
                            project_url=project.get("url"),
                            bid_amount=float(amount),
                            proposal_text=proposal_text,
                            status="success",
                            platform="guru"
                        )
                        db.add(history)
                        db.commit()
                        logger.info(f"✅ [GURU-AUTOBID] User {user_id}: Successfully quoted on '{project.get('title')}' with ${amount}")
                        return True
                    else:
                        error_msg = getattr(self, '_last_error', 'Quote failed')
                        logger.warning(f"❌ [GURU-AUTOBID] User {user_id}: Quote failed for '{project.get('title')}'. Reason: {error_msg}")
                        current_fails += 1
                        skip_counts["bid_fail"] += 1
                
                reasons = []
                if skip_counts["history"] > 0: reasons.append(f"{skip_counts['history']} already quoted")
                if skip_counts["too_old"] > 0: reasons.append(f"{skip_counts['too_old']} too old")
                if skip_counts["proposal_fail"] > 0: reasons.append(f"{skip_counts['proposal_fail']} AI fail")
                if skip_counts["bid_fail"] > 0: reasons.append(f"{skip_counts['bid_fail']} Guru API fail")
                
                return f"No quotes placed. {', '.join(reasons)}"

        except Exception as e:
            logger.error(f"Error in Guru User {user_id} cycle: {e}")
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
                "url": project.get("url"),
                "budget": {"amount": project.get("budget"), "currency": "USD"},
                "skills": project.get("skills", []),
                "client": {"name": project.get("employer_name", "")}
            }
        }
        
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv("N8N_WEBHOOK_API_KEY")
        if api_key: headers["X-API-Key"] = api_key
            
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(webhook_url, json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        return data[0].get("proposal") or data[0].get("Proposal")
                    elif isinstance(data, dict):
                        return data.get("proposal") or data.get("Proposal") or data.get("data", {}).get("proposal")
        except Exception as e:
            logger.error(f"Guru Webhook error: {e}")
        return None

    async def _place_bid(self, creds, project, proposal, amount):
        project_id = project.get("id")
        cookies_dict = creds.cookies if isinstance(creds.cookies, dict) else {}
        access_token = creds.access_token or cookies_dict.get("_accessToken", "")
        csrf_token = creds.csrf_token or cookies_dict.get("__RequestVerificationToken", "")

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://www.guru.com/work/detail/{project_id}?apply=true",
            "Origin": "https://www.guru.com",
        }
        if access_token: headers["Authorization"] = f"Bearer {access_token}"
        if csrf_token: headers["RequestVerificationToken"] = csrf_token

        due_date = (datetime.utcnow() + timedelta(days=30)).strftime("%m/%d/%Y")
        quote_payload = {
            "Milestones": [{
                "MilestoneId": 0,
                "MilestoneName": "Project delivery",
                "Amount": amount,
                "DueDate": due_date
            }],
            "ScopeOfWork": proposal,
            "SafePayRequired": True,
            "AutopayDuration": 0,
            "StatusUpdateInterval": 0,
            "PrivateTransactions": True,
            "IsPremium": False,
            "ShareContactInformation": False,
            "DeletedMilestoneIds": [],
            "DeletedAttachmentIds": [],
            "Attachments": []
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"https://www.guru.com/api/v1/freelancer/jobs/{project_id}/quote/milestone",
                    headers=headers,
                    cookies=cookies_dict,
                    json=quote_payload
                )
                if response.status_code in (200, 201):
                    return True
                else:
                    self._last_error = response.text[:200]
                    return False
        except Exception as e:
            self._last_error = str(e)
            return False

    def _normalize_guru_job(self, job: dict) -> dict:
        proj = job.get("Project") or job
        emp = proj.get("Employer") or job.get("Employer") or {}
        job_id = proj.get("ProjectID") or proj.get("JobId") or job.get("id") or ""
        
        def extract_numeric_budget(b_val):
            if not b_val: return 100
            if isinstance(b_val, (int, float)): return b_val
            if isinstance(b_val, dict): return b_val.get("max") or b_val.get("Min") or 100
            s = str(b_val).lower().replace(",", "").replace("$", "").strip()
            nums = re.findall(r'(\d+\.?\d*)([kmb]?)', s)
            if not nums: return 100
            val, multiplier = nums[-1]
            val = float(val)
            if multiplier == 'k': val *= 1000
            return val

        raw_budget = proj.get("BudgetAmountShortDescription") or proj.get("Budget") or ""
        budget = extract_numeric_budget(raw_budget)
        
        slug = proj.get("Slug") or job.get("slug") or ""
        url = f"https://www.guru.com/d/jobs/id/{job_id}/" if job_id else f"https://www.guru.com/d/jobs/{slug}/"
        
        skills_raw = proj.get("Skills") or job.get("skills") or []
        if isinstance(skills_raw, list):
            skills = [s.get("name") or s.get("title") or str(s) if isinstance(s, dict) else str(s) for s in skills_raw]
        else:
            skills = []

        # Posted time - Robust conversion
        posted_at_raw = (
            proj.get("PostedDate") or 
            proj.get("DatePosted") or 
            job.get("postedDate") or 
            job.get("postDate") or 
            job.get("createdDate") or 
            job.get("datePosted") or 
            proj.get("DatePostedFormatted") or
            ""
        )
        posted_at = str(posted_at_raw)
        try:
            val = None
            if isinstance(posted_at_raw, (int, float)):
                val = float(posted_at_raw)
            elif isinstance(posted_at_raw, str):
                if posted_at_raw.isdigit():
                    val = float(posted_at_raw)
                elif "/Date(" in posted_at_raw:
                    match = re.search(r'\/Date\((\d+)\)\/', posted_at_raw)
                    if match:
                        val = float(match.group(1))
            if val:
                if val > 10**11: val /= 1000.0
                dt = datetime.fromtimestamp(val)
                posted_at = dt.isoformat()
        except:
            pass

        # Proposals count - deep search with TotalApplied priority
        def find_quotes(obj):
            if not isinstance(obj, dict): return 0
            known = ["TotalApplied", "QuoteCount", "QuotesReceived", "QuotesCount", "NumberOfQuotes", "Proposals", "Quotes"]
            for k in known:
                val = obj.get(k)
                if val is not None:
                    if isinstance(val, (int, float)): return int(val)
                    if isinstance(val, str) and val.isdigit(): return int(val)
                    if isinstance(val, dict):
                        res = val.get("count") or val.get("Count") or val.get("total") or 0
                        if res: return int(res)
            return 0

        proposals = find_quotes(proj) or find_quotes(job) or 0

        return {
            "id": str(job_id),
            "title": proj.get("Title") or "Untitled",
            "description": proj.get("Description") or "",
            "budget": budget,
            "url": url,
            "posted_at": posted_at,
            "employer_name": emp.get("Name") or "Private Client",
            "skills": skills,
            "total_proposals": proposals
        }

guru_bidder = GuruAutoBidder()
