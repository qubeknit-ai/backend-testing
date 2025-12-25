import asyncio
import logging
from datetime import datetime, timedelta
import random
import json
from playwright.async_api import async_playwright
from typing import Optional, Dict, Any, List
import httpx

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutoBidder")

class AutoBidder:
    _instance = None
    _is_running = False
    _task = None
    _user_last_bid_time = {}  # Track last bid time per user
    
    # Removed global settings - now using per-user settings from database

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AutoBidder, cls).__new__(cls)
        return cls._instance

    def set_user_context(self, user_id: int):
        """Set the current user context for the auto-bidder (Legacy support)"""
        # Kept for compatibility but mostly unused in multi-user mode
        pass

    def update_settings(self, new_settings: Dict[str, Any]):
        """Update settings - now handled per user in database"""
        logger.info(f"AutoBidder settings update requested: {new_settings}")
        # Settings are now stored per-user in database, not globally
        
        # Don't automatically start here - let manual start handle it
        logger.info("Settings updated. Use start() method to begin auto-bidding.")

    def get_settings(self):
        """Get settings - now returns empty as settings are per-user in database"""
        return {}

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

    async def _loop(self):
        """Main bidding loop that handles multiple users"""
        logger.info("AutoBidder Loop Initiated (Multi-User Mode)")
        
        from database import SessionLocal
        from models import AutoBidSettings as DBAutoBidSettings
        
        while self._is_running:
            try:
                db = SessionLocal()
                active_users = []
                
                try:
                    # Fetch ALL enabled auto-bid settings
                    enabled_settings = db.query(DBAutoBidSettings).filter(
                        DBAutoBidSettings.enabled == True
                    ).all()
                    
                    if not enabled_settings:
                        logger.info("😴 No users have auto-bidding enabled. Sleeping...")
                    else:
                        logger.info(f"👥 Found {len(enabled_settings)} users with auto-bidding enabled")
                        
                        for db_setting in enabled_settings:
                            user_id = db_setting.user_id
                            
                            # Convert DB model to dict settings
                            settings = {
                                "enabled": db_setting.enabled,
                                "min_budget": db_setting.min_budget,
                                "max_budget": db_setting.max_budget,
                                "frequency_minutes": db_setting.frequency_minutes,
                                "max_project_bids": db_setting.max_project_bids,
                                "smart_bidding": db_setting.smart_bidding
                            }
                            
                            # Check if enough time has passed since last bid for this user
                            current_time = datetime.now()
                            last_bid_time = self._user_last_bid_time.get(user_id)
                            
                            if last_bid_time:
                                time_since_last_bid = (current_time - last_bid_time).total_seconds() / 60
                                if time_since_last_bid < settings["frequency_minutes"]:
                                    remaining_minutes = settings["frequency_minutes"] - time_since_last_bid
                                    logger.info(f"⏰ User {user_id}: Skipping - {time_since_last_bid:.1f}m since last bid, need {settings['frequency_minutes']}m (wait {remaining_minutes:.1f}m more)")
                                    continue
                                else:
                                    logger.info(f"✅ User {user_id}: Ready to bid - {time_since_last_bid:.1f}m since last bid (frequency: {settings['frequency_minutes']}m)")
                            else:
                                logger.info(f"🆕 User {user_id}: First bid attempt (frequency: {settings['frequency_minutes']}m)")
                            
                            logger.info(f"🔄 Processing cycle for User ID: {user_id}")
                            active_users.append(user_id)
                            
                            # Run bid cycle for this SPECIFIC user
                            bid_placed = await self._run_bid_cycle(user_id, settings)
                            
                            # Update last bid time if bid was placed
                            if bid_placed:
                                self._user_last_bid_time[user_id] = current_time
                            
                            # Small delay between users to not hammer API
                            await asyncio.sleep(5)
                            
                finally:
                    db.close()
                
                # Smart wait time: Check frequently enough for the most active user
                # but don't check too often to waste resources
                if enabled_settings:
                    min_frequency_minutes = min(setting.frequency_minutes for setting in enabled_settings)
                    # Wait for 1/4 of the minimum frequency, but at least 30 seconds, max 5 minutes
                    smart_wait_minutes = max(0.5, min(5, min_frequency_minutes / 4))
                    wait_seconds = int(smart_wait_minutes * 60)
                    logger.info(f"✅ Cycle complete for users {active_users}. Next check in {smart_wait_minutes} minutes ({wait_seconds}s) - min user frequency: {min_frequency_minutes}m")
                else:
                    wait_seconds = 300  # Default 5 minutes if no users
                    logger.info(f"✅ No enabled users. Waiting {wait_seconds} seconds...")
                
                await asyncio.sleep(wait_seconds)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in AutoBidder loop: {e}")
                import traceback
                logger.error(traceback.format_exc())
                await asyncio.sleep(60)

    async def _run_bid_cycle(self, user_id: int, settings: Dict):
        """Execute one complete bidding cycle for a specific user"""
        try:
            # 1. Fetch projects using existing API for THIS user
            projects = await self._fetch_projects(user_id)
            if not projects:
                logger.info(f"📭 User {user_id}: No projects found")
                return False

            logger.info(f"📥 User {user_id}: Found {len(projects)} total projects")

            # 2. Filter projects by criteria for THIS user
            filtered_projects = self._filter_projects(projects, settings)
            if not filtered_projects:
                logger.info(f"🔍 User {user_id}: No projects match criteria")
                return False

            logger.info(f"✅ User {user_id}: {len(filtered_projects)} projects match criteria")

            # 3. Sort by NEWEST first (time_submitted), then by HIGHEST budget - FIXED!
            filtered_projects.sort(key=lambda p: (
                -(p.get("time_submitted", 0)),  # Negative for descending (newest first)
                -(p.get("budget", {}).get("maximum", 0))  # Then by highest budget
            ))
            
            # 4. Check database for already-bid projects
            from database import SessionLocal
            from models import BidHistory
            
            db = SessionLocal()
            try:
                # Get all project IDs THIS user has already bid on
                already_bid_ids = db.query(BidHistory.project_id).filter(
                    BidHistory.user_id == user_id
                ).all()
                already_bid_set = set(str(row.project_id) for row in already_bid_ids)
                
                logger.info(f"📋 User {user_id}: Already bid on {len(already_bid_set)} projects")
                
                # Try to bid on projects in order (best first) until one succeeds
                for project in filtered_projects:
                    project_id = str(project.get("id"))
                    
                    if project_id in already_bid_set:
                        logger.debug(f"⏭️  Skipping {project.get('title')} - already bid")
                        continue
                    
                    logger.info(f"🎯 User {user_id}: Attempting bid on '{project.get('title')}'")
                    
                    try:
                        bid_result = await self._bid_on_project(user_id, project, settings)
                        if bid_result:
                            logger.info(f"✅ User {user_id}: Successfully placed bid!")
                            return True
                        else:
                            logger.info(f"❌ User {user_id}: Bid failed, trying next project...")
                            continue
                    except Exception as e:
                        logger.error(f"Failed to bid on {project.get('title')} for User {user_id}: {e}")
                        continue
                
            finally:
                db.close()
            
            logger.info(f"ℹ️  User {user_id}: No successful bids placed this cycle")
            return False

        except Exception as e:
            logger.error(f"Error in bid cycle for User {user_id}: {e}")
            return False

    async def _fetch_projects(self, user_id: int) -> List[Dict]:
        """Fetch projects from the Freelancer API using database credentials for SPECIFIC USER"""
        logger.info("-" * 40)
        logger.info(f"🔍 FETCHING PROJECTS FOR USER {user_id}")
        logger.info("-" * 40)
        
        try:
            # Get freelancer credentials directly from database
            from database import SessionLocal
            from models import FreelancerCredentials
            
            db = SessionLocal()
            try:
                # Get credentials for THIS SPECIFIC USER
                credentials = db.query(FreelancerCredentials).filter(
                    FreelancerCredentials.user_id == user_id,
                    FreelancerCredentials.is_validated == True
                ).first()
                
                if not credentials:
                    logger.warning(f"⚠️  User {user_id}: No validated Freelancer credentials found in database.")
                    return []
                
                logger.info(f"✅ Found credentials for user_id: {credentials.user_id}")
                
                # Prepare headers with Freelancer authentication
                headers = {
                    "Content-Type": "application/json"
                }
                
                # Add cookies if available
                cookies_dict = {}
                if credentials.cookies:
                    cookies_dict = credentials.cookies
                    logger.info(f"🍪 Using {len(cookies_dict)} stored cookies")
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    # Step 1: Get user profile to fetch skills
                    logger.info(f"👤 User {user_id}: Fetching profile skills...")
                    user_profile_url = "https://www.freelancer.com/api/users/0.1/self?limit=1&jobs=true&webapp=1&compact=true"
                    
                    user_response = await client.get(user_profile_url, headers=headers, cookies=cookies_dict)
                    
                    logger.info(f"👤 User {user_id}: Profile API response: {user_response.status_code}")
                    
                    user_skills = []
                    if user_response.status_code == 200:
                        user_data = user_response.json()
                        user_profile = user_data.get("result", {})
                        
                        logger.info(f"👤 User {user_id}: Profile keys: {list(user_profile.keys()) if isinstance(user_profile, dict) else type(user_profile)}")
                        
                        if user_profile.get("jobs") and len(user_profile["jobs"]) > 0:
                            user_skills = [job["id"] for job in user_profile["jobs"]]
                            logger.info(f"✅ User {user_id}: Found {len(user_skills)} skills: {user_skills[:5]}...")
                        else:
                            logger.info(f"ℹ️  User {user_id}: No skills found in user profile")
                    else:
                        logger.warning(f"⚠️  User {user_id}: Could not get user profile: {user_response.status_code}")
                        logger.warning(f"⚠️  User {user_id}: Profile response: {user_response.text[:300]}...")
                    
                    # Step 2: Build URL with user skills - FIXED to get LATEST projects
                    if user_skills:
                        skills_params = "&".join([f"jobs[]={skill_id}" for skill_id in user_skills])
                        # Added sort_field=time_submitted&sort_order=desc to get NEWEST projects first
                        url = f"https://www.freelancer.com/api/projects/0.1/projects/active/?compact=true&limit=20&user_details=true&jobs=true&sort_field=time_submitted&sort_order=desc&{skills_params}&languages[]=en"
                    else:
                        # Added sort_field=time_submitted&sort_order=desc to get NEWEST projects first
                        url = "https://www.freelancer.com/api/projects/0.1/projects/active/?compact=true&limit=20&user_details=true&jobs=true&sort_field=time_submitted&sort_order=desc&user_recommended=true"
                    
                    logger.info(f"🌐 User {user_id}: Using URL: {url[:100]}...")
                    
                    # Step 3: Fetch projects
                    logger.info(f"📡 User {user_id}: Fetching projects...")
                    
                    response = await client.get(
                        url,
                        headers=headers,
                        cookies=cookies_dict
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        logger.info(f"🔍 User {user_id}: API Response structure: {list(data.keys()) if isinstance(data, dict) else type(data)}")
                        
                        result = data.get("result", {})
                        if isinstance(result, dict):
                            projects = result.get("projects", [])
                            logger.info(f"📊 User {user_id}: Result keys: {list(result.keys())}")
                        else:
                            projects = []
                            logger.warning(f"⚠️  User {user_id}: Unexpected result format: {type(result)}")
                        
                        logger.info(f"✅ User {user_id}: Successfully fetched {len(projects)} projects")
                        
                        # Log first few projects with their posting times for debugging
                        if projects and len(projects) > 0:
                            first_project = projects[0]
                            logger.info(f"📝 User {user_id}: Sample project keys: {list(first_project.keys()) if isinstance(first_project, dict) else type(first_project)}")
                            
                            # Log posting times of first 3 projects to verify we're getting latest
                            for i, project in enumerate(projects[:3]):
                                time_submitted = project.get("time_submitted", 0)
                                if time_submitted:
                                    posted_time = self._format_time_ago(time_submitted)
                                    logger.info(f"📅 User {user_id}: Project {i+1} '{project.get('title', 'Unknown')[:50]}...' posted {posted_time}")
                                else:
                                    logger.info(f"📅 User {user_id}: Project {i+1} '{project.get('title', 'Unknown')[:50]}...' - no timestamp")
                        
                        # If no projects found with skills, try without skills filter - FIXED to get LATEST
                        if len(projects) == 0 and user_skills:
                            logger.info(f"🔄 User {user_id}: No projects with skills filter, trying general search...")
                            # Added sort_field=time_submitted&sort_order=desc to get NEWEST projects first
                            fallback_url = "https://www.freelancer.com/api/projects/0.1/projects/active/?compact=true&limit=20&user_details=true&jobs=true&sort_field=time_submitted&sort_order=desc"
                            
                            fallback_response = await client.get(
                                fallback_url,
                                headers=headers,
                                cookies=cookies_dict
                            )
                            
                            if fallback_response.status_code == 200:
                                fallback_data = fallback_response.json()
                                fallback_result = fallback_data.get("result", {})
                                if isinstance(fallback_result, dict):
                                    projects = fallback_result.get("projects", [])
                                    logger.info(f"🔄 User {user_id}: Fallback search found {len(projects)} projects")
                        
                        return projects
                    else:
                        logger.error(f"❌ User {user_id}: Failed to fetch projects: HTTP {response.status_code}")
                        logger.error(f"❌ User {user_id}: Response body: {response.text[:500]}...")
                        return []
                        
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error fetching projects for User {user_id}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return []

    def _filter_projects(self, projects: List[Dict], settings: Dict) -> List[Dict]:
        """Filter projects based on settings"""
        
        min_budget = settings.get("min_budget", 10)
        max_budget = settings.get("max_budget", 1000)
        max_bids = settings.get("max_project_bids", 50)
        
        filtered = []

        for project in projects:
            # Check budget
            budget = project.get("budget", {})
            project_min = budget.get("minimum", 0)
            project_max = budget.get("maximum", project_min)
            
            if project_min < min_budget or project_max > max_budget:
                continue

            # Check bid count
            bid_count = project.get("bid_stats", {}).get("bid_count", 0)
            if bid_count > max_bids:
                continue

            filtered.append(project)
        
        return filtered

    async def _bid_on_project(self, user_id: int, project: Dict, settings: Dict):
        """Place a REAL bid on Freelancer.com with AI-generated proposal"""
        logger.info(f"\n💼 User {user_id}: BIDDING ON PROJECT")
        
        try:
            title = project.get("title", "Unknown")
            project_id = project.get("id")
            
            # Calculate bid amount
            budget = project.get("budget", {})
            min_budget = budget.get("minimum", 50)
            max_budget = budget.get("maximum", min_budget)
            
            if settings.get("smart_bidding"):
                bid_amount = (min_budget + max_budget) / 2
            else:
                bid_amount = min_budget

            bid_amount = round(bid_amount, 2)

            # Step 1: Generate AI proposal using webhook
            logger.info(f"🤖 User {user_id}: Generating AI proposal...")
            
            import os
            webhook_url = os.getenv("FREELANCER_PROPOSAL_WEBHOOK_URL")
            
            if not webhook_url:
                logger.warning("⚠️  FREELANCER_PROPOSAL_WEBHOOK_URL not configured")
                proposal = f"I can help you with this project. My bid is ${bid_amount}."
            else:
                try:
                    project_data = {
                        "id": project_id,
                        "title": title,
                        "description": project.get("preview_description", project.get("description", "No description available")),
                        "preview_description": project.get("preview_description", ""),
                        "url": f"https://www.freelancer.com/projects/{project.get('seo_url', project_id)}",
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
                    
                    async with httpx.AsyncClient(timeout=300.0) as client:
                        response = await client.post(webhook_url, json=payload, headers=headers)
                        
                        if response.status_code == 200:
                            try:
                                response_text = response.text
                                logger.info(f"🔍 User {user_id}: Webhook response text: {response_text[:500]}...")
                                
                                data = response.json()
                                logger.info(f"🔍 User {user_id}: Webhook response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
                                
                                # Try multiple possible response formats
                                proposal = None
                                if isinstance(data, dict):
                                    # Try different possible paths
                                    proposal = (data.get("data", {}).get("proposal") or 
                                              data.get("proposal") or 
                                              data.get("result", {}).get("proposal") or
                                              data.get("output") or  # Added for your webhook format
                                              data.get("message") or
                                              data.get("text"))
                                elif isinstance(data, str):
                                    proposal = data
                                
                                if not proposal or proposal.strip() == "":
                                    logger.error(f"❌ User {user_id}: No proposal found in response structure: {data}")
                                    raise Exception("Empty proposal")
                                    
                                logger.info(f"✅ User {user_id}: AI Proposal Generated ({len(proposal)} chars)")
                            except Exception as parse_error:
                                logger.error(f"❌ User {user_id}: Parse error: {parse_error}")
                                logger.error(f"❌ User {user_id}: Raw response: {response.text}")
                                raise Exception(f"Failed to parse AI proposal: {parse_error}")
                        else:
                            raise Exception(f"AI failed: {response.status_code}")
                except Exception as e:
                    logger.error(f"❌ User {user_id}: Error generating AI proposal: {e}")
                    logger.info("⏭️  Skipping this project")
                    return False
            
            # Step 2: Get Freelancer credentials and place REAL bid
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
                
                headers = {
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://www.freelancer.com",
                    "Referer": f"https://www.freelancer.com/projects/{project.get('seo_url', project_id)}",
                    "x-requested-with": "XMLHttpRequest"
                }
                
                if user_id_cookie and auth_hash:
                    headers["freelancer-auth-v2"] = f"{user_id_cookie};{auth_hash}"
                
                if csrf_token:
                    headers["X-CSRF-Token"] = csrf_token
                    headers["X-XSRF-TOKEN"] = csrf_token
                    headers["x-csrf-token"] = csrf_token
                    headers["x-xsrf-token"] = csrf_token

                if credentials.access_token and credentials.access_token != "using_cookies":
                    headers["Authorization"] = f"Bearer {credentials.access_token}"
                    headers["freelancer-oauth-v1"] = credentials.access_token
                
                bid_payload = {
                    "project_id": int(project_id),
                    "bidder_id": int(user_id_cookie),
                    "amount": float(bid_amount),
                    "period": 7,
                    "milestone_percentage": 100,
                    "highlighted": False,
                    "sponsored": False,
                    "ip_contract": False,
                    "anonymous": False,
                    "description": proposal
                }
                
                api_url = "https://www.freelancer.com/api/projects/0.1/bids/?compact=true&new_errors=true&new_pools=true"
                
                logger.info(f"📤 User {user_id}: Sending bid request...")
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    bid_response = await client.post(
                        api_url,
                        headers=headers,
                        cookies=cookies_dict,
                        json=bid_payload
                    )
                    
                    logger.info(f"📨 Response: {bid_response.status_code}")
                    response_text = bid_response.text
                    
                    if bid_response.status_code == 200 or bid_response.status_code == 201:
                        try:
                            response_data = bid_response.json()
                            if response_data.get("status") == "error":
                                error_message = response_data.get("message", "Unknown error")
                                logger.error(f"❌ User {user_id}: Freelancer returned error: {error_message}")
                                
                                # Check if it's "already bid" error - don't save to history, just return False
                                if "already bid" in error_message.lower() or "you have already bid" in error_message.lower():
                                    logger.info(f"⏭️  User {user_id}: Already bid error detected, moving to next project...")
                                    return False
                                
                                # For other errors, save to history
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
                                return False
                            
                            logger.info(f"✅ User {user_id}: BID PLACED SUCCESSFULLY!")
                            logger.info(f"   Bid ID: {response_data.get('result', {}).get('id', 'N/A')}")
                            
                        except Exception as e:
                            logger.warning(f"⚠️  Could not parse response: {e}")
                        
                        await self._save_bid_history({
                            "user_id": user_id,
                            "project_id": str(project_id),
                            "project_title": title,
                            "project_url": f"https://www.freelancer.com/projects/{project.get('seo_url', project_id)}",
                            "bid_amount": bid_amount,
                            "proposal_text": proposal,
                            "status": "success"
                        })
                        return True
                    else:
                        error_text = bid_response.text
                        logger.error(f"❌ User {user_id}: Bid failed: {bid_response.status_code}")
                        
                        try:
                            error_data = bid_response.json()
                            error_message = error_data.get('message') or error_data.get('error') or str(error_data)
                        except:
                            error_message = error_text or f"HTTP {bid_response.status_code}"
                        
                        # Check if it's "already bid" error - don't save to history
                        if "already bid" in error_message.lower() or "you have already bid" in error_message.lower():
                            logger.info(f"⏭️  User {user_id}: Already bid error detected, moving to next project...")
                            return False
                        
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
                        return False
            finally:
                db.close()

        except Exception as e:
            logger.error(f"❌ ERROR BIDDING ON PROJECT for User {user_id}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    def _format_time_ago(self, timestamp):
        """Format timestamp to 'X hours/days ago' like manual flow"""
        if not timestamp:
            return "Unknown"
        
        from datetime import datetime
        now = datetime.utcnow()
        posted = datetime.fromtimestamp(timestamp)
        diff = now - posted
        
        minutes = diff.total_seconds() / 60
        if minutes < 60:
            return f"{int(minutes)} min ago"
        
        hours = minutes / 60
        if hours < 24:
            return f"{int(hours)} hours ago"
        
        days = hours / 24
        return f"{int(days)} days ago"

    async def _save_bid_history(self, bid_data: Dict):
        """Save bid attempt to database"""
        try:
            from database import SessionLocal
            from models import BidHistory
            
            db = SessionLocal()
            try:
                history = BidHistory(
                    user_id=bid_data.get("user_id", 1),
                    project_id=bid_data.get("project_id"),
                    project_title=bid_data.get("project_title"),
                    project_url=bid_data.get("project_url"),
                    bid_amount=bid_data.get("bid_amount", 0),
                    proposal_text=bid_data.get("proposal_text"),
                    status=bid_data.get("status", "pending"),
                    error_message=bid_data.get("error_message")
                )
                
                db.add(history)
                db.commit()
                logger.info("✅ Saved to bid history database")
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Failed to save bid history: {e}")

# Singleton accessor
bidder = AutoBidder()
