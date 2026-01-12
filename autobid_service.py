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
    _user_last_bid_time = {}  # Track last bid time per user (only when bid is PLACED)
    
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

    def _should_skip_user(self, user_id: int, settings: Dict) -> bool:
        """Check if user should be skipped due to frequency limits"""
        current_time = datetime.now()
        last_bid_time = self._user_last_bid_time.get(user_id)
        
        if last_bid_time:
            time_since_last_bid = (current_time - last_bid_time).total_seconds() / 60
            if time_since_last_bid < settings["frequency_minutes"]:
                remaining_minutes = settings["frequency_minutes"] - time_since_last_bid
                logger.info(f"⏰ User {user_id}: Skipping - {time_since_last_bid:.1f}m since last bid, need {settings['frequency_minutes']}m (wait {remaining_minutes:.1f}m more)")
                return True
            else:
                logger.info(f"✅ User {user_id}: Ready to bid - {time_since_last_bid:.1f}m since last bid (frequency: {settings['frequency_minutes']}m)")
        else:
            logger.info(f"🆕 User {user_id}: First bid attempt (frequency: {settings['frequency_minutes']}m)")
        
        return False

    async def _loop(self):
        """Main bidding loop that handles multiple users in parallel"""
        logger.info("AutoBidder Loop Initiated (Multi-User Parallel Mode)")
        
        from database import SessionLocal
        from models import AutoBidSettings as DBAutoBidSettings
        
        while self._is_running:
            try:
                db = SessionLocal()
                
                try:
                    # Fetch ALL enabled auto-bid settings
                    enabled_settings = db.query(DBAutoBidSettings).filter(
                        DBAutoBidSettings.enabled == True
                    ).all()
                    
                    if not enabled_settings:
                        logger.info("😴 No users have auto-bidding enabled. Sleeping...")
                        await asyncio.sleep(30)
                        continue
                    
                    logger.info(f"👥 Found {len(enabled_settings)} users with auto-bidding enabled")
                    
                    # Prepare tasks for parallel processing
                    tasks = []
                    active_users = []
                    
                    for db_setting in enabled_settings:
                        user_id = db_setting.user_id
                        
                        # Convert DB model to dict settings
                        settings = {
                            "enabled": db_setting.enabled,
                            "daily_bids": db_setting.daily_bids,
                            "currencies": db_setting.currencies,
                            "frequency_minutes": db_setting.frequency_minutes,
                            "max_project_bids": db_setting.max_project_bids,
                            "smart_bidding": db_setting.smart_bidding,
                            "min_skill_match": getattr(db_setting, 'min_skill_match', 1)
                        }
                        
                        # Check if user should be skipped due to frequency limits
                        if self._should_skip_user(user_id, settings):
                            continue
                        
                        logger.info(f"🔄 Adding User {user_id} to parallel processing queue")
                        active_users.append(user_id)
                        
                        # Create task for parallel execution
                        task = asyncio.create_task(self._run_bid_cycle(user_id, settings))
                        tasks.append((user_id, task))
                    
                    # Process all users in parallel
                    if tasks:
                        logger.info(f"🚀 Processing {len(tasks)} users in parallel (different skills = different projects)...")
                        
                        # Run all users simultaneously
                        results = await asyncio.gather(*[task for _, task in tasks], return_exceptions=True)
                        
                        # Update last bid times for successful bids and handle results
                        current_time = datetime.now()
                        successful_bids = 0
                        
                        for i, (user_id, _) in enumerate(tasks):
                            result = results[i]
                            
                            if isinstance(result, Exception):
                                logger.error(f"❌ User {user_id}: Exception during parallel processing: {result}")
                            elif result is True:  # Successful bid
                                self._user_last_bid_time[user_id] = current_time
                                successful_bids += 1
                                logger.info(f"✅ User {user_id}: Successful bid placed in parallel processing")
                            elif result == "BID_LIMIT_REACHED":
                                logger.error(f"🚫 User {user_id}: Bid limit reached during parallel processing")
                            elif result == "CREDENTIALS_EXPIRED":
                                logger.error(f"🔐 User {user_id}: Credentials expired during parallel processing")
                            else:
                                logger.info(f"ℹ️  User {user_id}: No bid placed during parallel processing")
                        
                        logger.info(f"📊 Parallel processing complete: {successful_bids}/{len(tasks)} users placed successful bids")
                    else:
                        logger.info("⏭️  No users ready for processing this cycle")
                        
                finally:
                    db.close()
                
                # Smart wait time: Check frequently enough for the most active user
                # but don't check too often to waste resources
                if enabled_settings:
                    min_frequency_minutes = min(setting.frequency_minutes for setting in enabled_settings)
                    # Wait for 1/4 of the minimum frequency, but at least 30 seconds, max 5 minutes
                    smart_wait_minutes = max(0.5, min(5, min_frequency_minutes / 4))
                    wait_seconds = int(smart_wait_minutes * 60)
                    logger.info(f"✅ Parallel cycle complete for users {active_users}. Next check in {smart_wait_minutes} minutes ({wait_seconds}s) - min user frequency: {min_frequency_minutes}m")
                else:
                    wait_seconds = 30  # Check every 30 seconds if no users enabled
                    logger.info(f"✅ No enabled users. Waiting {wait_seconds} seconds...")
                
                # Add heartbeat log every cycle to prevent serverless timeout
                logger.info(f"💓 AutoBidder heartbeat - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - Parallel service running normally")
                
                await asyncio.sleep(wait_seconds)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in AutoBidder parallel loop: {e}")
                import traceback
                logger.error(traceback.format_exc())
                await asyncio.sleep(60)

    async def _check_daily_bid_limit(self, user_id: int, settings: Dict) -> bool:
        """Check if user has reached their daily bid limit"""
        daily_limit = settings.get("daily_bids", 10)
        
        try:
            from database import SessionLocal
            from models import BidHistory
            from datetime import datetime, timedelta
            
            db = SessionLocal()
            try:
                # Get today's date range
                today = datetime.now().date()
                start_of_day = datetime.combine(today, datetime.min.time())
                end_of_day = datetime.combine(today, datetime.max.time())
                
                # Count successful bids placed today
                today_bids = db.query(BidHistory).filter(
                    BidHistory.user_id == user_id,
                    BidHistory.status == "success",
                    BidHistory.created_at >= start_of_day,
                    BidHistory.created_at <= end_of_day
                ).count()
                
                logger.info(f"📊 User {user_id}: Daily bid count: {today_bids}/{daily_limit}")
                
                if today_bids >= daily_limit:
                    logger.error(f"🚫 User {user_id}: Daily bid limit reached ({today_bids}/{daily_limit})! Disabling auto-bidding...")
                    await self._disable_user_autobidding(user_id)
                    return False
                
                return True
                
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error checking daily bid limit for User {user_id}: {e}")
            return True  # Allow bidding if we can't check (fail-safe)

    async def _run_bid_cycle(self, user_id: int, settings: Dict):
        """Execute one complete bidding cycle for a specific user"""
        try:
            # 0. Check daily bid limit first
            if not await self._check_daily_bid_limit(user_id, settings):
                logger.error(f"🚫 User {user_id}: Daily bid limit reached - stopping bid cycle")
                return False
            
            # 0.5. Get user's selected skills from database
            user_selected_skills = []
            try:
                from database import SessionLocal
                from models import FreelancerCredentials
                
                db = SessionLocal()
                try:
                    credentials = db.query(FreelancerCredentials).filter(
                        FreelancerCredentials.user_id == user_id
                    ).first()
                    
                    if credentials and credentials.selected_skills:
                        user_selected_skills = credentials.selected_skills
                        logger.info(f"🎯 User {user_id}: Found {len(user_selected_skills)} selected skills: {user_selected_skills}")
                    else:
                        logger.info(f"ℹ️  User {user_id}: No selected skills found, will use all profile skills")
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"❌ Error getting selected skills for User {user_id}: {e}")
            
            # 1. Fetch projects using existing API for THIS user
            projects = await self._fetch_projects(user_id)
            if not projects:
                logger.info(f"📭 User {user_id}: No projects found")
                return False

            logger.info(f"📥 User {user_id}: Found {len(projects)} total projects")

            # Don't filter by "seen" projects - let the database bid history handle filtering
            # We want to process ALL projects and let the bid history check determine what to skip
            projects_to_process = projects
            logger.info(f"📥 User {user_id}: Processing all {len(projects_to_process)} projects (filtering by bid history)")

            # 2. Filter projects by criteria for THIS user (including freshness check and skill matching)
            filtered_projects = self._filter_projects(projects_to_process, settings, user_selected_skills)
            if not filtered_projects:
                min_skill_match = settings.get("min_skill_match", 1)
                logger.info(f"🔍 User {user_id}: No projects match criteria (currencies: {settings.get('currencies', ['USD'])}, max bids: {settings.get('max_project_bids', 50)}, min skills: {min_skill_match}, max age: 10 minutes)")
                return False

            min_skill_match = settings.get("min_skill_match", 1)
            logger.info(f"✅ User {user_id}: {len(filtered_projects)} FRESH projects match criteria (currencies: {settings.get('currencies', ['USD'])}, min skills: {min_skill_match}, posted within 10 minutes)")

            # 3. Sort by NEWEST first ONLY - prioritize the freshest opportunities
            filtered_projects.sort(key=lambda p: -(p.get("time_submitted", 0)))  # Only sort by newest first
            
            # Log the sorting to verify we're prioritizing the absolute newest projects
            if filtered_projects:
                logger.info(f"📅 User {user_id}: Projects sorted by NEWEST first (freshest opportunities):")
                for i, project in enumerate(filtered_projects[:5]):  # Show first 5
                    time_submitted = project.get("time_submitted", 0)
                    posted_time = self._format_time_ago(time_submitted) if time_submitted else "Unknown time"
                    import time
                    current_timestamp = time.time()
                    age_minutes = (current_timestamp - time_submitted) / 60 if time_submitted else 999
                    logger.info(f"   {i+1}. '{project.get('title', 'Unknown')[:40]}...' posted {posted_time} ({age_minutes:.1f}m ago)")
            
            # 4. Try to bid on projects in order (newest first) until one succeeds
            # Removed historical bid filtering - let it bid on fresh projects
            for project in filtered_projects:
                project_id = str(project.get("id"))
                project_title = project.get("title", "Unknown")[:50]
                
                # Log project details before attempting bid
                time_submitted = project.get("time_submitted", 0)
                posted_time = self._format_time_ago(time_submitted) if time_submitted else "Unknown time"
                budget = project.get("budget", {})
                budget_str = f"${budget.get('minimum', 0)}-${budget.get('maximum', 0)}"
                project_currency = self._get_project_currency(project)
                
                # Debug: Log timestamp details
                if time_submitted:
                    import time
                    current_timestamp = time.time()
                    age_minutes = (current_timestamp - time_submitted) / 60
                    logger.info(f"🕐 Fresh project - Current: {current_timestamp}, Project: {time_submitted}, Age: {age_minutes:.1f} minutes")
                
                logger.info(f"🎯 User {user_id}: Attempting bid on FRESH project '{project_title}...' (ID: {project_id})")
                logger.info(f"   📅 Posted: {posted_time}")
                logger.info(f"   💰 Budget: {budget_str} ({project_currency})")
                logger.info(f"   👥 Bids: {project.get('bid_stats', {}).get('bid_count', 0)}")
                
                try:
                    bid_result = await self._bid_on_project(user_id, project, settings)
                    if bid_result == True:
                        logger.info(f"✅ User {user_id}: Successfully placed bid on fresh project!")
                        
                        # Check if we've now reached the daily limit after this successful bid
                        if not await self._check_daily_bid_limit(user_id, settings):
                            logger.info(f"🚫 User {user_id}: Daily bid limit reached after successful bid - stopping cycle")
                            return True  # Return True because we did place a bid successfully
                        
                        return True
                    elif bid_result == "BID_LIMIT_REACHED":
                        logger.error(f"🚫 User {user_id}: Bid limit reached - stopping bid cycle for this user")
                        return False  # Stop the entire cycle for this user
                    elif bid_result == "CREDENTIALS_EXPIRED":
                        logger.error(f"🔐 User {user_id}: Credentials expired - stopping bid cycle for this user")
                        return False  # Stop the entire cycle for this user
                    else:
                        logger.info(f"❌ User {user_id}: Bid failed on fresh project, trying next...")
                        continue
                except Exception as e:
                    logger.error(f"Failed to bid on fresh project {project.get('title')} for User {user_id}: {e}")
                    continue
            
            logger.info(f"ℹ️  User {user_id}: No successful bids placed this cycle")
            
            # Simplified summary - focus on fresh opportunities
            logger.info(f"📊 User {user_id}: CYCLE SUMMARY:")
            logger.info(f"   📥 Total projects fetched: {len(projects)}")
            logger.info(f"   ⚡ Fresh projects (≤10min): {len(filtered_projects)}")
            logger.info(f"   💡 Reason: All fresh projects failed to bid - check credentials or try again next cycle")
            
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
                        
                        # Get selected skills from database instead of all profile skills
                        selected_skill_names = credentials.selected_skills or []
                        logger.info(f"🎯 User {user_id}: Selected skills from database: {selected_skill_names}")
                        
                        # If user has selected skills, find their IDs from the profile
                        if selected_skill_names and user_profile.get("jobs"):
                            profile_jobs = user_profile["jobs"]
                            # Match selected skill names with profile job IDs
                            for job in profile_jobs:
                                if job.get("name") in selected_skill_names:
                                    user_skills.append(job["id"])
                            
                            logger.info(f"✅ User {user_id}: Matched skill IDs from profile: {user_skills}")
                            
                            # If no matches found in profile, we'll use all profile skills as fallback
                            if not user_skills and profile_jobs:
                                logger.warning(f"⚠️  User {user_id}: No selected skills matched profile, using all profile skills as fallback")
                                user_skills = [job["id"] for job in profile_jobs]
                        elif user_profile.get("jobs"):
                            # No skills selected, use all profile skills
                            user_skills = [job["id"] for job in user_profile["jobs"]]
                            logger.info(f"✅ User {user_id}: Using all profile skills (no selection): {user_skills}")
                        else:
                            logger.info(f"ℹ️  User {user_id}: No skills found in user profile")
                    else:
                        logger.warning(f"⚠️  User {user_id}: Could not get user profile: {user_response.status_code}")
                        logger.warning(f"⚠️  User {user_id}: Profile response: {user_response.text[:300]}...")
                    
                    # Step 2: Try different fetch limits to find new projects
                    projects = []
                    for limit in [50, 100, 150]:  # Try increasing limits
                        logger.info(f"🔍 User {user_id}: Trying to fetch {limit} projects...")
                        
                        # Build URL with user skills - ALWAYS sort by NEWEST first for fresh opportunities
                        if user_skills:
                            skills_params = "&".join([f"jobs[]={skill_id}" for skill_id in user_skills])
                            # CRITICAL: Always fetch NEWEST projects first to get fresh opportunities
                            # Add full_description=true to get more project details including skills
                            url = f"https://www.freelancer.com/api/projects/0.1/projects/active/?compact=false&limit={limit}&user_details=true&jobs=true&full_description=true&sort_field=time_submitted&sort_order=desc&{skills_params}&languages[]=en"
                        else:
                            # CRITICAL: Always fetch NEWEST projects first to get fresh opportunities
                            # Add full_description=true to get more project details including skills
                            url = f"https://www.freelancer.com/api/projects/0.1/projects/active/?compact=false&limit={limit}&user_details=true&jobs=true&full_description=true&sort_field=time_submitted&sort_order=desc&user_recommended=true"
                        
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
                            
                            logger.info(f"✅ User {user_id}: Successfully fetched {len(projects)} projects with limit {limit}")
                            
                            # Check if we have enough projects or if we should try a higher limit
                            if len(projects) >= limit * 0.8:  # If we got close to the limit, try higher
                                logger.info(f"🔄 User {user_id}: Got {len(projects)}/{limit} projects, trying higher limit...")
                                continue
                            else:
                                logger.info(f"✅ User {user_id}: Got {len(projects)}/{limit} projects, sufficient for processing")
                                break
                                
                        elif response.status_code == 401:
                            logger.error(f"🔐 User {user_id}: Freelancer credentials EXPIRED (401) - user needs to refresh session")
                            logger.error(f"🔐 User {user_id}: AutoBidder will skip this user until credentials are refreshed")
                            return []
                        else:
                            logger.error(f"❌ User {user_id}: Failed to fetch projects: HTTP {response.status_code}")
                            logger.error(f"❌ User {user_id}: Response body: {response.text[:500]}...")
                            break
                    
                    # Log first few projects with their posting times for debugging
                    if projects and len(projects) > 0:
                        first_project = projects[0]
                        logger.info(f"📝 User {user_id}: Sample project keys: {list(first_project.keys()) if isinstance(first_project, dict) else type(first_project)}")
                        
                        # Log detailed structure of first project to find skills/tags
                        logger.info(f"🔍 User {user_id}: First project detailed structure:")
                        for key, value in first_project.items():
                            if isinstance(value, (list, dict)):
                                if len(str(value)) < 500:  # Show full content for smaller objects
                                    logger.info(f"   {key}: {value}")
                                else:
                                    logger.info(f"   {key}: {type(value)} (length: {len(value) if isinstance(value, (list, dict)) else len(str(value))})")
                            else:
                                logger.info(f"   {key}: {value}")
                        
                        # Also check if there are nested objects that might contain skills
                        if 'owner' in first_project and isinstance(first_project['owner'], dict):
                            logger.info(f"🔍 User {user_id}: Project owner structure:")
                            for key, value in first_project['owner'].items():
                                logger.info(f"   owner.{key}: {value}")
                        
                        if 'budget' in first_project and isinstance(first_project['budget'], dict):
                            logger.info(f"🔍 User {user_id}: Project budget structure:")
                            for key, value in first_project['budget'].items():
                                logger.info(f"   budget.{key}: {value}")
                        
                        # Log posting times of first 5 projects to verify we're getting latest
                        for i, project in enumerate(projects[:5]):
                            time_submitted = project.get("time_submitted", 0)
                            if time_submitted:
                                posted_time = self._format_time_ago(time_submitted)
                                project_currency = self._get_project_currency(project)
                                bid_count = project.get("bid_stats", {}).get("bid_count", 0)
                                logger.info(f"📋 User {user_id}: Project {i+1} '{project.get('title', 'Unknown')[:50]}...' posted {posted_time} - Currency: {project_currency} - Bids: {bid_count}")
                            else:
                                logger.info(f"📋 User {user_id}: Project {i+1} '{project.get('title', 'Unknown')[:50]}...' - no timestamp")
                    
                    # If no projects found with skills, try without skills filter - ALWAYS get NEWEST first
                    if len(projects) == 0 and user_skills:
                        logger.info(f"🔄 User {user_id}: No projects with skills filter, trying general search...")
                        # CRITICAL: Always fetch NEWEST projects first to get fresh opportunities
                        # Add full_description=true to get more project details including skills
                        fallback_url = "https://www.freelancer.com/api/projects/0.1/projects/active/?compact=false&limit=100&user_details=true&jobs=true&full_description=true&sort_field=time_submitted&sort_order=desc"
                        
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
                        elif fallback_response.status_code == 401:
                            logger.error(f"🔐 User {user_id}: Freelancer credentials EXPIRED (401) - user needs to refresh session")
                            logger.error(f"🔐 User {user_id}: AutoBidder will skip this user until credentials are refreshed")
                            logger.error(f"🔐 User {user_id}: User should open Freelancer.com in browser to refresh cookies")
                            
                            # Mark credentials as expired in database
                            try:
                                from database import SessionLocal
                                from models import FreelancerCredentials
                                
                                db = SessionLocal()
                                try:
                                    credentials = db.query(FreelancerCredentials).filter(
                                        FreelancerCredentials.user_id == user_id
                                    ).first()
                                    
                                    if credentials:
                                        credentials.is_validated = False  # Mark as invalid
                                        db.commit()
                                        logger.info(f"🔐 User {user_id}: Marked credentials as expired in database")
                                finally:
                                    db.close()
                            except Exception as e:
                                logger.error(f"❌ Failed to update credential status: {e}")
                            
                            return []
                    
                    return projects
                        
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error fetching projects for User {user_id}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return []

    def _get_project_currency(self, project: Dict) -> str:
        """Extract currency from project data"""
        budget = project.get("budget", {})
        
        # Method 1: Check budget.currency
        if budget.get("currency"):
            if isinstance(budget["currency"], str):
                return budget["currency"]
            elif isinstance(budget["currency"], dict):
                if budget["currency"].get("code"):
                    return budget["currency"]["code"]
                elif budget["currency"].get("id"):
                    # Freelancer API currency mapping based on common IDs
                    currency_map = {
                        1: 'USD', 2: 'EUR', 3: 'GBP', 4: 'AUD', 5: 'CAD', 6: 'INR', 7: 'JPY', 8: 'CNY',
                        9: 'BRL', 10: 'MXN', 11: 'ZAR', 12: 'SGD', 13: 'NZD', 14: 'HKD', 15: 'SEK',
                        16: 'NOK', 17: 'DKK', 18: 'PLN', 19: 'CHF', 20: 'RUB', 21: 'TRY', 22: 'THB',
                        23: 'PHP', 24: 'IDR', 25: 'MYR', 26: 'VND', 27: 'KRW', 28: 'AED', 29: 'SAR',
                        30: 'PKR', 31: 'BDT', 32: 'NGN', 33: 'EGP', 34: 'ARS', 35: 'CLP', 36: 'COP',
                        37: 'PEN', 38: 'UAH', 39: 'ILS', 40: 'CZK', 41: 'HUF', 42: 'RON'
                    }
                    return currency_map.get(budget["currency"]["id"], "USD")
        
        # Method 2: Check budget.currency_id
        if budget.get("currency_id"):
            currency_map = {
                1: 'USD', 2: 'EUR', 3: 'GBP', 4: 'AUD', 5: 'CAD', 6: 'INR', 7: 'JPY', 8: 'CNY',
                28: 'AED'  # Added AED mapping
            }
            return currency_map.get(budget["currency_id"], "USD")
        
        # Method 3: Check project-level currency fields
        if project.get("currency"):
            if isinstance(project["currency"], str):
                return project["currency"]
            elif isinstance(project["currency"], dict) and project["currency"].get("code"):
                return project["currency"]["code"]
        
        # Method 4: Try to detect from client location as fallback
        if project.get("owner"):
            client_country = None
            if project["owner"].get("location", {}).get("country", {}).get("code"):
                client_country = project["owner"]["location"]["country"]["code"]
            elif project["owner"].get("country", {}).get("code"):
                client_country = project["owner"]["country"]["code"]
            
            if client_country:
                country_currency_map = {
                    'GB': 'GBP', 'UK': 'GBP', 'CA': 'CAD', 'AU': 'AUD', 'IN': 'INR',
                    'JP': 'JPY', 'CN': 'CNY', 'BR': 'BRL', 'MX': 'MXN', 'ZA': 'ZAR',
                    'SG': 'SGD', 'NZ': 'NZD', 'HK': 'HKD', 'SE': 'SEK', 'NO': 'NOK',
                    'DK': 'DKK', 'PL': 'PLN', 'CH': 'CHF', 'RU': 'RUB', 'TR': 'TRY',
                    'TH': 'THB', 'PH': 'PHP', 'ID': 'IDR', 'MY': 'MYR', 'VN': 'VND',
                    'KR': 'KRW', 'AE': 'AED', 'SA': 'SAR', 'PK': 'PKR', 'BD': 'BDT',
                    'NG': 'NGN', 'EG': 'EGP'
                }
                return country_currency_map.get(client_country, "USD")
        
        # Default fallback
        return "USD"

    def _filter_projects(self, projects: List[Dict], settings: Dict, user_selected_skills: List[str] = None) -> List[Dict]:
        """Filter projects based on settings - focus on currency, freshness, and skill matching"""
        
        max_bids = settings.get("max_project_bids", 50)
        supported_currencies = settings.get("currencies", ["USD"])
        min_skill_match = settings.get("min_skill_match", 1)
        max_age_minutes = 10  # Only bid on projects posted within last 10 minutes for freshness
        
        filtered = []
        import time
        current_timestamp = time.time()

        logger.info(f"🔍 Starting filter process - Max bids: {max_bids}, Currencies: {supported_currencies}, Min skill match: {min_skill_match}, Max age: {max_age_minutes}m")
        if user_selected_skills:
            logger.info(f"🎯 User selected skills for matching: {user_selected_skills}")
        else:
            logger.info(f"ℹ️  No user selected skills - skill matching will be skipped")

        for project in projects:
            project_id = project.get("id")
            project_title = project.get("title", "Unknown")[:40]
            
            # 1. Check if project currency is in user's supported currencies FIRST
            project_currency = self._get_project_currency(project)
            if project_currency not in supported_currencies:
                logger.debug(f"❌ Project {project_id} '{project_title}...' - CURRENCY MISMATCH: {project_currency} not in {supported_currencies}")
                continue

            # 2. Check project age - only bid on FRESH projects (within last 10 minutes)
            time_submitted = project.get("time_submitted", 0)
            if time_submitted > 0:
                age_minutes = (current_timestamp - time_submitted) / 60
                if age_minutes > max_age_minutes:
                    posted_time = self._format_time_ago(time_submitted)
                    logger.debug(f"❌ Project {project_id} '{project_title}...' - TOO OLD: {posted_time} ({age_minutes:.1f}m > {max_age_minutes}m)")
                    continue
                else:
                    posted_time = self._format_time_ago(time_submitted)
                    logger.info(f"✅ Project {project_id} '{project_title}...' - FRESH: {posted_time} ({age_minutes:.1f}m ago) - Currency: {project_currency}")
            else:
                logger.debug(f"❌ Project {project_id} '{project_title}...' - NO TIMESTAMP")
                continue

            # 3. Check skill matching if user has selected skills
            if user_selected_skills and min_skill_match > 0:
                project_skills = []
                
                # Try multiple ways to extract skills from project data
                # Method 1: Standard jobs field
                if project.get("jobs"):
                    project_skills.extend([job.get("name") for job in project["jobs"] if job.get("name")])
                
                # Method 2: Direct skills field
                if project.get("skills"):
                    if isinstance(project["skills"], list):
                        project_skills.extend([skill.get("name") if isinstance(skill, dict) else str(skill) for skill in project["skills"]])
                
                # Method 3: Tags field
                if project.get("tags"):
                    if isinstance(project["tags"], list):
                        project_skills.extend([tag.get("name") if isinstance(tag, dict) else str(tag) for tag in project["tags"]])
                
                # Method 4: Categories field
                if project.get("categories"):
                    if isinstance(project["categories"], list):
                        project_skills.extend([cat.get("name") if isinstance(cat, dict) else str(cat) for cat in project["categories"]])
                
                # Method 5: Job categories field
                if project.get("job_categories"):
                    if isinstance(project["job_categories"], list):
                        project_skills.extend([cat.get("name") if isinstance(cat, dict) else str(cat) for cat in project["job_categories"]])
                
                # Method 6: Check for other possible skill fields
                for field_name in ['job_skills', 'project_skills', 'required_skills', 'skill_tags', 'job_tags']:
                    if project.get(field_name):
                        if isinstance(project[field_name], list):
                            project_skills.extend([item.get("name") if isinstance(item, dict) else str(item) for item in project[field_name]])
                
                # Method 7: Try to extract from description or title (as last resort)
                # Common skill keywords that might appear in project descriptions
                common_skills = [
                    'PHP', 'JavaScript', 'Python', 'Java', 'C++', 'HTML', 'CSS', 'React', 'Angular', 'Vue',
                    'Node.js', 'Laravel', 'WordPress', 'Shopify', 'Android', 'iOS', 'Flutter', 'React Native',
                    'Data Entry', 'Excel', 'Graphic Design', 'Logo Design', 'Photoshop', 'Illustrator',
                    'SEO', 'Digital Marketing', 'Content Writing', 'Copywriting', 'Translation',
                    'MySQL', 'PostgreSQL', 'MongoDB', 'AWS', 'Azure', 'Docker', 'Kubernetes'
                ]
                
                description = project.get("description", "") + " " + project.get("preview_description", "") + " " + project.get("title", "")
                description_lower = description.lower()
                
                for skill in common_skills:
                    if skill.lower() in description_lower:
                        project_skills.append(f"{skill} (from description)")
                
                # Remove duplicates and None values
                project_skills = list(set([skill for skill in project_skills if skill]))
                
                # Enhanced logging for debugging
                logger.info(f"🔍 Project {project_id} '{project_title}...' - SKILL DEBUG:")
                logger.info(f"   User selected skills: {user_selected_skills}")
                logger.info(f"   Project skills (extracted): {project_skills}")
                logger.info(f"   Raw jobs field: {project.get('jobs', 'NOT_FOUND')}")
                logger.info(f"   Raw skills field: {project.get('skills', 'NOT_FOUND')}")
                logger.info(f"   Raw tags field: {project.get('tags', 'NOT_FOUND')}")
                logger.info(f"   Raw categories field: {project.get('categories', 'NOT_FOUND')}")
                logger.info(f"   Description snippet: {(project.get('description', '') + ' ' + project.get('preview_description', ''))[:100]}...")
                
                # Log all available fields for debugging
                logger.info(f"   Available project fields: {list(project.keys())}")
                
                # If project has no skills data, skip skill matching (allow the project)
                if not project_skills:
                    logger.info(f"⚠️  Project {project_id} '{project_title}...' - NO SKILLS DATA - Allowing project (API may not return skills)")
                else:
                    # Count matching skills with flexible matching
                    matching_skills = []
                    user_skills_lower = [skill.lower().strip() for skill in user_selected_skills]
                    project_skills_lower = [skill.lower().strip() for skill in project_skills]
                    
                    # Exact match first
                    for i, skill in enumerate(user_selected_skills):
                        if skill in project_skills:
                            matching_skills.append(skill)
                    
                    # If no exact matches, try case-insensitive matching
                    if not matching_skills:
                        for i, user_skill in enumerate(user_skills_lower):
                            for j, project_skill in enumerate(project_skills_lower):
                                if user_skill == project_skill:
                                    matching_skills.append(user_selected_skills[i])
                                    break
                    
                    # If still no matches, try partial matching (contains)
                    if not matching_skills:
                        for i, user_skill in enumerate(user_skills_lower):
                            for j, project_skill in enumerate(project_skills_lower):
                                if user_skill in project_skill or project_skill in user_skill:
                                    matching_skills.append(f"{user_selected_skills[i]} (partial: {project_skills[j]})")
                                    break
                    
                    skill_match_count = len(matching_skills)
                    logger.info(f"   Matching skills: {matching_skills}")
                    logger.info(f"   Match count: {skill_match_count}/{min_skill_match}")
                    
                    if skill_match_count < min_skill_match:
                        logger.info(f"❌ Project {project_id} '{project_title}...' - SKILL MISMATCH: {skill_match_count}/{min_skill_match} skills match")
                        continue
                    else:
                        logger.info(f"🎯 Project {project_id} '{project_title}...' - SKILL MATCH: {skill_match_count}/{min_skill_match} skills match: {matching_skills}")
            else:
                logger.info(f"⏭️  Project {project_id} '{project_title}...' - SKILL CHECK SKIPPED (no selected skills or min_skill_match=0)")

            # 4. Check bid count LAST (less important than freshness, currency, and skills)
            bid_count = project.get("bid_stats", {}).get("bid_count", 0)
            if bid_count > max_bids:
                logger.debug(f"❌ Project {project_id} '{project_title}...' - TOO MANY BIDS: {bid_count} > {max_bids}")
                continue

            logger.info(f"🎯 Project {project_id} '{project_title}...' - PASSED ALL FILTERS - Adding to bid list")
            filtered.append(project)
        
        logger.info(f"🔍 Filter complete: {len(filtered)} projects passed all filters out of {len(projects)} total")
        return filtered

    async def _generate_proposal(self, user_id: int, project: Dict, bid_amount: float) -> str:
        """Generate AI proposal for a project"""
        logger.info(f"🤖 User {user_id}: Generating AI proposal...")
        
        import os
        webhook_url = os.getenv("FREELANCER_PROPOSAL_WEBHOOK_URL")
        
        if not webhook_url:
            logger.warning("⚠️  FREELANCER_PROPOSAL_WEBHOOK_URL not configured")
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
            
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(webhook_url, json=payload, headers=headers)
                
                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"🔍 User {user_id}: Webhook response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
                    
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
                        logger.error(f"❌ User {user_id}: No proposal found in response structure: {data}")
                        raise Exception("Empty proposal")
                        
                    logger.info(f"✅ User {user_id}: AI Proposal Generated ({len(proposal)} chars)")
                    return proposal
                else:
                    raise Exception(f"AI failed: {response.status_code}")
                    
        except Exception as e:
            logger.error(f"❌ User {user_id}: Error generating AI proposal: {e}")
            raise Exception(f"Failed to generate proposal: {e}")

    def _build_bid_payload(self, user_id: int, project: Dict, bid_amount: float, proposal: str, user_id_cookie: str) -> Dict:
        """Build the bid payload for Freelancer API"""
        project_id = project.get("id")
        
        return {
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

    def _get_freelancer_headers(self, project: Dict, user_id_cookie: str, auth_hash: str, csrf_token: str, access_token: str) -> Dict:
        """Build headers for Freelancer API requests"""
        project_id = project.get("id")
        
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

        if access_token and access_token != "using_cookies":
            headers["Authorization"] = f"Bearer {access_token}"
            headers["freelancer-oauth-v1"] = access_token
        
        return headers

    async def _submit_bid(self, headers: Dict, cookies_dict: Dict, bid_payload: Dict) -> httpx.Response:
        """Submit bid to Freelancer API"""
        api_url = "https://www.freelancer.com/api/projects/0.1/bids/?compact=true&new_errors=true&new_pools=true"
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.post(
                api_url,
                headers=headers,
                cookies=cookies_dict,
                json=bid_payload
            )
    
    def _format_time_ago(self, timestamp):
        """Format timestamp to 'X hours/days ago' like manual flow"""
        if not timestamp:
            return "Unknown"
        
        from datetime import datetime
        import time
        
        # Get current time as Unix timestamp
        now_timestamp = time.time()
        
        # Calculate difference in seconds
        diff_seconds = now_timestamp - timestamp
        
        if diff_seconds < 0:
            # Handle future timestamps (shouldn't happen but just in case)
            return "Just posted"
        
        minutes = diff_seconds / 60
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
                    
                    # Check if it's "already bid" error - don't save to history
                    if "already bid" in error_message.lower() or "you have already bid" in error_message.lower():
                        logger.info(f"⏭️  User {user_id}: Already bid error detected, moving to next project...")
                        return "ALREADY_BID"
                    
                    # Save other errors to history
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
                logger.info(f"⏭️  User {user_id}: Already bid error detected, moving to next project...")
                return "ALREADY_BID"
            
            if "used all of your bids" in error_message.lower() or "you have used all your bids" in error_message.lower() or "daily limit" in error_message.lower():
                logger.error(f"🚫 User {user_id}: Used all available bids or reached daily limit! Temporarily disabling auto-bidding...")
                await self._disable_user_autobidding(user_id)
                return "BID_LIMIT_REACHED"
            
            # Save other errors to history
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

    async def _mark_credentials_expired(self, user_id: int):
        """Mark user credentials as expired in database"""
        try:
            from database import SessionLocal
            from models import FreelancerCredentials
            
            db = SessionLocal()
            try:
                credentials = db.query(FreelancerCredentials).filter(
                    FreelancerCredentials.user_id == user_id
                ).first()
                
                if credentials:
                    credentials.is_validated = False
                    db.commit()
                    logger.info(f"🔐 User {user_id}: Marked credentials as expired in database")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"❌ Failed to update credential status: {e}")

    async def _disable_user_autobidding(self, user_id: int):
        """Disable auto-bidding for user who hit bid limit"""
        try:
            from database import SessionLocal
            from models import AutoBidSettings as DBAutoBidSettings
            
            db = SessionLocal()
            try:
                db_settings = db.query(DBAutoBidSettings).filter(
                    DBAutoBidSettings.user_id == user_id
                ).first()
                
                if db_settings:
                    db_settings.enabled = False
                    db.commit()
                    logger.info(f"🚫 User {user_id}: Auto-bidding disabled due to bid limit reached")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"❌ Failed to disable auto-bidding: {e}")

    async def _bid_on_project(self, user_id: int, project: Dict, settings: Dict):
        """Place a REAL bid on Freelancer.com with AI-generated proposal"""
        logger.info(f"\n💼 User {user_id}: BIDDING ON PROJECT")
        
        try:
            title = project.get("title", "Unknown")
            project_id = project.get("id")
            
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

# Singleton accessor
bidder = AutoBidder()