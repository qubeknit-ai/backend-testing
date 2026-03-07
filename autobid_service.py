import asyncio
import logging
from datetime import datetime, timedelta
import random
import json
import time
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
    _user_retry_count = {}    # Track retry attempts per user
    _user_backoff_until = {}  # Track backoff periods per user
    
    # PERFORMANCE: Add caching
    _settings_cache = {}  # Cache user settings
    _settings_cache_time = {}  # Cache timestamps
    _bid_history_cache = {}  # Cache bid history per user
    _bid_history_cache_time = {}  # Cache timestamps
    _http_client = None  # Reusable HTTP client
    
    # Removed global settings - now using per-user settings from database

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AutoBidder, cls).__new__(cls)
        return cls._instance

    async def _get_http_client(self):
        """PERFORMANCE: Get or create reusable HTTP client with connection pooling"""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)
                # Removed http2=True to avoid dependency issues
            )
        return self._http_client

    async def _get_cached_bid_history(self, user_id: int) -> set:
        """PERFORMANCE: Get cached bid history to avoid repeated DB queries"""
        now = time.time()
        cache_ttl = 300  # 5 minutes cache
        
        # Check if cache is valid
        if (user_id in self._bid_history_cache and 
            now - self._bid_history_cache_time.get(user_id, 0) < cache_ttl):
            return self._bid_history_cache[user_id]
        
        # Fetch from database
        try:
            from database import SessionLocal
            from models import BidHistory
            
            db = SessionLocal()
            try:
                # Fetch all project IDs user has bid on (much faster than checking one by one)
                bid_history_ids = db.query(BidHistory.project_id).filter(
                    BidHistory.user_id == user_id
                ).all()
                
                # Convert to set for O(1) lookup
                bid_history_set = {str(pid[0]) for pid in bid_history_ids}
                
                # Cache it
                self._bid_history_cache[user_id] = bid_history_set
                self._bid_history_cache_time[user_id] = now
                
                logger.info(f"📦 User {user_id}: Cached {len(bid_history_set)} bid history entries")
                return bid_history_set
                
            finally:
                db.close()
        except Exception as e:
            logger.error(f"❌ Error fetching bid history cache for User {user_id}: {e}")
            return set()

    def _invalidate_bid_history_cache(self, user_id: int):
        """PERFORMANCE: Invalidate cache when new bid is placed"""
        if user_id in self._bid_history_cache:
            del self._bid_history_cache[user_id]
        if user_id in self._bid_history_cache_time:
            del self._bid_history_cache_time[user_id]

    async def debug_skill_extraction(self, user_id: int, limit: int = 3):
        """Debug function to test skill extraction from projects"""
        logger.info("🔍 DEBUG: Testing skill extraction from projects...")
        
        projects = await self._fetch_projects_with_fallbacks(user_id)
        
        if not projects:
            logger.info("❌ No projects found for skill extraction testing")
            return
        
        logger.info(f"📊 Testing skill extraction on {min(limit, len(projects))} projects")
        
        for i, project in enumerate(projects[:limit]):
            logger.info(f"\n🔍 PROJECT {i+1} SKILL EXTRACTION:")
            logger.info(f"   Title: {project.get('title', 'Unknown')[:60]}...")
            logger.info(f"   ID: {project.get('id')}")
            
            # Test enhanced skill extraction
            extracted_skills = self._extract_project_skills(project)
            
            logger.info(f"   🎯 Extracted {len(extracted_skills)} skills:")
            for skill in extracted_skills[:5]:  # Show first 5 skills
                skill_id = skill.get("id", "No ID")
                skill_name = skill.get("name", "No Name")
                skill_category = skill.get("category", "No Category")
                logger.info(f"      - ID: {skill_id}, Name: '{skill_name}', Category: {skill_category}")
            
            if len(extracted_skills) > 5:
                logger.info(f"      ... and {len(extracted_skills) - 5} more skills")
            
            # Show raw project structure for comparison
            logger.info(f"   📋 Raw project fields:")
            if project.get("jobs"):
                logger.info(f"      - jobs: {len(project['jobs'])} items")
            if project.get("skills"):
                logger.info(f"      - skills: {len(project['skills'])} items")
            if project.get("categories"):
                logger.info(f"      - categories: {len(project['categories'])} items")
            if project.get("job_details"):
                logger.info(f"      - job_details: {len(project['job_details'])} items")
            
            logger.info("   " + "─" * 60)
        
        logger.info("🔍 DEBUG: Skill extraction testing complete")

    async def debug_user_skills(self, user_id: int):
        """Debug function to check user's skills from Freelancer profile"""
        logger.info(f"🔍 DEBUG: Checking User {user_id} skills from Freelancer profile...")
        
        try:
            from database import SessionLocal
            from models import FreelancerCredentials
            
            db = SessionLocal()
            try:
                credentials = db.query(FreelancerCredentials).filter(
                    FreelancerCredentials.user_id == user_id,
                    FreelancerCredentials.is_validated == True
                ).first()
                
                if not credentials:
                    logger.error(f"❌ No valid credentials found for User {user_id}")
                    return
                
                headers = {"Content-Type": "application/json"}
                cookies_dict = credentials.cookies if credentials.cookies else {}
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    # Get user skills
                    user_skills = await self._get_user_skill_ids(user_id, client, headers, cookies_dict)
                    
                    logger.info(f"✅ User {user_id} has {len(user_skills)} skills in profile:")
                    logger.info(f"   Skill IDs: {user_skills}")
                    
                    # Also check selected skills from database
                    if credentials.selected_skills:
                        logger.info(f"   Selected skills in DB: {credentials.selected_skills}")
                    else:
                        logger.info(f"   No selected skills configured in database")
                        
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error debugging user skills: {e}")

    async def debug_project_structure(self, user_id: int, limit: int = 5):
        """Debug function to examine actual project structure from Freelancer API"""
        logger.info("🔍 DEBUG: Examining Freelancer API project structure...")
        
        projects = await self._fetch_projects_with_fallbacks(user_id)
        
        if not projects:
            logger.info("❌ No projects found for debugging")
            return
        
        logger.info(f"📊 Found {len(projects)} projects for structure analysis")
        
        # Analyze first few projects
        for i, project in enumerate(projects[:limit]):
            logger.info(f"\n🔍 PROJECT {i+1} STRUCTURE ANALYSIS:")
            logger.info(f"   Title: {project.get('title', 'Unknown')[:50]}...")
            logger.info(f"   ID: {project.get('id')}")
            
            # Show all top-level fields
            logger.info(f"   📋 Field count: {len(project.keys())}")
            
            # Examine potential skill-related fields in detail
            skill_fields = ['jobs', 'skills', 'categories', 'tags']
            
            for field in skill_fields:
                if project.get(field):
                    value = project[field]
                    logger.info(f"   🎯 {field}: Found ({len(value) if isinstance(value, list) else 'object'})")
                else:
                    logger.info(f"   ❌ {field}: NOT_FOUND")
            
            # Show title only
            logger.info(f"   📝 Title: {project.get('title', 'Unknown')[:80]}...")
            
            logger.info("   " + "─" * 50)
        
        logger.info("🔍 DEBUG: Project structure analysis complete")

    async def test_skill_extraction(self, user_id: int, selected_skills: list = None):
        """Test skill extraction with current logic"""
        if selected_skills is None:
            selected_skills = []  # Test with no selected skills
        
        logger.info(f"🧪 TESTING skill extraction with skills: {selected_skills if selected_skills else 'None (testing no skill filter)'}")
        
        projects = await self._fetch_projects_with_fallbacks(user_id)
        if not projects:
            logger.info("❌ No projects found for testing")
            return
        
        # Test filtering logic
        settings = {
            "currencies": ["USD"],
            "max_project_bids": 50,
            "min_skill_match": 1 if selected_skills else 0  # Set to 0 if no skills selected
        }
        
        filtered = self._filter_projects(projects[:10], settings, selected_skills)
        
        logger.info(f"🧪 TEST RESULTS:")
        logger.info(f"   📥 Total projects tested: {len(projects[:10])}")
        logger.info(f"   ✅ Projects passing skill filter: {len(filtered)}")
        logger.info(f"   📊 Success rate: {len(filtered)/len(projects[:10])*100:.1f}%")
        logger.info(f"   🎯 Skill matching mode: {'Specific skills' if selected_skills else 'No skill filter (allow all)'}")

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
        """Check if user should be skipped due to frequency limits or backoff"""
        current_time = datetime.now()
        
        # Check if user is in backoff period
        if user_id in self._user_backoff_until:
            if current_time < self._user_backoff_until[user_id]:
                remaining_backoff = (self._user_backoff_until[user_id] - current_time).total_seconds() / 60
                logger.info(f"⏸️  User {user_id}: In backoff period - {remaining_backoff:.1f}m remaining")
                return True
            else:
                # Backoff period ended, reset retry count
                del self._user_backoff_until[user_id]
                self._user_retry_count[user_id] = 0
                logger.info(f"🔄 User {user_id}: Backoff period ended, resuming normal operation")
        
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

    def _handle_user_failure(self, user_id: int):
        """Handle consecutive failures with fixed 5-minute backoff"""
        current_time = datetime.now()
        retry_count = self._user_retry_count.get(user_id, 0) + 1
        self._user_retry_count[user_id] = retry_count
        
        if retry_count >= 3:  # After 3 consecutive failures
            # Fixed 5-minute backoff, then reset count
            backoff_minutes = 5
            backoff_until = current_time + timedelta(minutes=backoff_minutes)
            self._user_backoff_until[user_id] = backoff_until
            
            # Reset retry count so it starts fresh after backoff
            self._user_retry_count[user_id] = 0
            
            logger.warning(f"⏸️  User {user_id}: 3 consecutive failures, backing off for {backoff_minutes}m (count reset)")
        else:
            logger.info(f"⚠️  User {user_id}: Failure #{retry_count}, continuing normal operation")

    def _handle_user_success(self, user_id: int):
        """Reset failure tracking on successful bid"""
        if user_id in self._user_retry_count:
            del self._user_retry_count[user_id]
        if user_id in self._user_backoff_until:
            del self._user_backoff_until[user_id]

    async def _loop(self):
        """Main bidding loop that handles multiple users in parallel"""
        logger.info("AutoBidder Loop Initiated (Multi-User Parallel Mode)")
        
        from database import SessionLocal
        from models import AutoBidSettings as DBAutoBidSettings
        
        # Enhanced monitoring variables
        consecutive_empty_cycles = 0
        last_successful_bid_time = None
        cycle_count = 0
        last_heartbeat_time = time.time()  # Track last heartbeat
        
        while self._is_running:
            try:
                # HEARTBEAT: Log every 2 minutes to prevent serverless timeout
                current_time = time.time()
                if current_time - last_heartbeat_time >= 120:  # 2 minutes = 120 seconds
                    logger.info(f"💓 AutoBidder heartbeat - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - Service running normally")
                    last_heartbeat_time = current_time
                
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
                            "min_skill_match": getattr(db_setting, 'min_skill_match', 1),
                            "commission_projects": getattr(db_setting, 'commission_projects', True)
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
                                self._handle_user_failure(user_id)
                            elif result is True:  # Successful bid
                                self._user_last_bid_time[user_id] = current_time
                                self._handle_user_success(user_id)
                                successful_bids += 1
                                logger.info(f"✅ User {user_id}: Successful bid placed in parallel processing")
                            elif result == "BID_LIMIT_REACHED":
                                logger.error(f"🚫 User {user_id}: Bid limit reached during parallel processing")
                                self._handle_user_failure(user_id)
                            elif result == "CREDENTIALS_EXPIRED":
                                logger.error(f"🔐 User {user_id}: Credentials expired during parallel processing")
                                self._handle_user_failure(user_id)
                            else:
                                logger.info(f"ℹ️  User {user_id}: No bid placed during parallel processing")
                                self._handle_user_failure(user_id)
                        
                        logger.info(f"📊 Parallel processing complete: {successful_bids}/{len(tasks)} users placed successful bids")
                        
                        # Enhanced monitoring
                        cycle_count += 1
                        if successful_bids > 0:
                            consecutive_empty_cycles = 0
                            last_successful_bid_time = current_time
                        else:
                            consecutive_empty_cycles += 1
                        
                        # Alert if no bids for too long
                        if consecutive_empty_cycles >= 10:  # 10 cycles without bids
                            logger.warning(f"🚨 ALERT: {consecutive_empty_cycles} consecutive cycles without successful bids!")
                            logger.warning(f"🚨 Last successful bid: {last_successful_bid_time}")
                            # Could send webhook alert here
                        
                        # Periodic health check
                        if cycle_count % 20 == 0:  # Every 20 cycles
                            await self._health_check_report(enabled_settings, consecutive_empty_cycles)
                    else:
                        logger.info("⏭️  No users ready for processing this cycle")
                        
                finally:
                    db.close()
                
                # Smart wait time: Check frequently enough for the most active user
                # but don't check too often to waste resources
                if enabled_settings:
                    min_frequency_minutes = min(setting.frequency_minutes for setting in enabled_settings)
                    # Wait for 1/2 of the minimum frequency, but at least 2 minutes, max 5 minutes
                    smart_wait_minutes = max(2, min(5, min_frequency_minutes / 2))
                    wait_seconds = int(smart_wait_minutes * 60)
                    logger.info(f"✅ Parallel cycle complete for users {active_users}. Next check in {smart_wait_minutes} minutes ({wait_seconds}s) - min user frequency: {min_frequency_minutes}m")
                else:
                    wait_seconds = 120  # Check every 120 seconds if no users enabled
                    logger.info(f"✅ No enabled users. Waiting {wait_seconds} seconds...")
                
                await asyncio.sleep(wait_seconds)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in AutoBidder parallel loop: {e}")
                import traceback
                logger.error(traceback.format_exc())
                await asyncio.sleep(60)

    async def _health_check_report(self, enabled_settings, consecutive_empty_cycles):
        """Generate health check report for monitoring"""
        try:
            from database import SessionLocal
            from models import BidHistory, FreelancerCredentials
            from datetime import datetime, timedelta
            
            db = SessionLocal()
            try:
                # Check recent bid activity (last 24 hours)
                yesterday = datetime.now() - timedelta(days=1)
                recent_bids = db.query(BidHistory).filter(
                    BidHistory.created_at >= yesterday,
                    BidHistory.status == "success"
                ).count()
                
                # Check credential status
                expired_credentials = db.query(FreelancerCredentials).filter(
                    FreelancerCredentials.is_validated == False
                ).count()
                
                logger.info(f"🏥 HEALTH CHECK:")
                logger.info(f"   📊 Active users: {len(enabled_settings)}")
                logger.info(f"   ✅ Successful bids (24h): {recent_bids}")
                logger.info(f"   🔐 Expired credentials: {expired_credentials}")
                logger.info(f"   ⚠️  Empty cycles: {consecutive_empty_cycles}")
                
                if recent_bids == 0 and len(enabled_settings) > 0:
                    logger.error(f"🚨 CRITICAL: No successful bids in 24h with {len(enabled_settings)} active users!")
                
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Health check failed: {e}")

    async def _cleanup_old_bid_history(self, user_id: int, days_to_keep: int = 7):
        """Clean up old bid history entries to prevent database bloat"""
        try:
            from database import SessionLocal
            from models import BidHistory
            from datetime import datetime, timedelta
            
            db = SessionLocal()
            try:
                # Delete bid history older than specified days
                cutoff_date = datetime.now() - timedelta(days=days_to_keep)
                
                deleted_count = db.query(BidHistory).filter(
                    BidHistory.user_id == user_id,
                    BidHistory.created_at < cutoff_date
                ).delete()
                
                if deleted_count > 0:
                    db.commit()
                    logger.info(f"🧹 User {user_id}: Cleaned up {deleted_count} old bid history entries (>{days_to_keep} days)")
                
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error cleaning up bid history for User {user_id}: {e}")

    async def _has_bid_history(self, user_id: int, project_id: str) -> bool:
        """PERFORMANCE: Check if user has already attempted to bid on this project (using cache)"""
        # This method is now deprecated in favor of batch checking in _run_bid_cycle
        # Kept for backward compatibility
        try:
            bid_history_set = await self._get_cached_bid_history(user_id)
            return project_id in bid_history_set
        except Exception as e:
            logger.error(f"❌ Error checking bid history for User {user_id}, Project {project_id}: {e}")
            return False  # If we can't check, allow bidding (fail-safe)

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
            
            # 0.1. Clean up old bid history (once per cycle)
            await self._cleanup_old_bid_history(user_id, days_to_keep=7)
            
            # PERFORMANCE: Fetch bid history ONCE at start (instead of per-project)
            bid_history_set = await self._get_cached_bid_history(user_id)
            logger.info(f"📦 User {user_id}: Loaded {len(bid_history_set)} bid history entries from cache")
            
            # 0.5. Get user's selected skills from database
            user_selected_skills = []
            user_selected_skill_ids = []  # Also get skill IDs for matching
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
                        
                        # Also get the skill IDs from the user's profile for ID-based matching
                        headers = {"Content-Type": "application/json"}
                        cookies_dict = credentials.cookies if credentials.cookies else {}
                        
                        client = await self._get_http_client()
                        user_skill_ids = await self._get_user_skill_ids(user_id, client, headers, cookies_dict)
                        user_selected_skill_ids = user_skill_ids
                        logger.info(f"🎯 User {user_id}: Found {len(user_selected_skill_ids)} skill IDs: {user_selected_skill_ids}")
                    else:
                        logger.info(f"ℹ️  User {user_id}: No selected skills found, will use all profile skills")
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"❌ Error getting selected skills for User {user_id}: {e}")
            
            # 1. Fetch projects using enhanced fallback strategy
            projects = await self._fetch_projects_with_fallbacks(user_id)
            if not projects:
                logger.info(f"📭 User {user_id}: No projects found")
                return False

            logger.info(f"📥 User {user_id}: Found {len(projects)} total projects")

            # Don't filter by "seen" projects - let the database bid history handle filtering
            # We want to process ALL projects and let the bid history check determine what to skip
            projects_to_process = projects
            logger.info(f"📥 User {user_id}: Processing all {len(projects_to_process)} projects (filtering by bid history)")

            # 2. Filter projects by criteria for THIS user (including freshness check and skill matching)
            filtered_projects = self._filter_projects(projects_to_process, settings, user_selected_skills, user_selected_skill_ids)
            
            if not filtered_projects:
                min_skill_match = settings.get("min_skill_match", 1)
                logger.info(f"🔍 User {user_id}: No projects match criteria (currencies: {settings.get('currencies', ['USD'])}, max bids: {settings.get('max_project_bids', 50)}, min skills: {min_skill_match}, max age: 15 minutes)")
                return False

            min_skill_match = settings.get("min_skill_match", 1)
            logger.info(f"✅ User {user_id}: {len(filtered_projects)} FRESH projects match criteria (currencies: {settings.get('currencies', ['USD'])}, min skills: {min_skill_match}, posted within 15 minutes)")

            # 3. Sort by NEWEST first ONLY - prioritize the freshest opportunities
            filtered_projects.sort(key=lambda p: -(p.get("time_submitted", 0)))  # Only sort by newest first
            
            # Log the sorting to verify we're prioritizing the absolute newest projects
            if filtered_projects:
                logger.info(f"📅 User {user_id}: {len(filtered_projects)} fresh projects sorted by newest first")
            
            # 4. Try to bid on projects in order (newest first) until one succeeds
            # PERFORMANCE: Check bid history using in-memory set (O(1) lookup instead of DB query)
            for project in filtered_projects:
                project_id = str(project.get("id"))
                project_title = project.get("title", "Unknown")[:50]
                
                # PERFORMANCE: Check in-memory set instead of database query
                if project_id in bid_history_set:
                    logger.info(f"⏭️  User {user_id}: Skipping '{project_title}...' - Already attempted (in bid history)")
                    continue  # Move to next project
                
                # Log project details before attempting bid
                time_submitted = project.get("time_submitted", 0)
                posted_time = self._format_time_ago(time_submitted) if time_submitted else "Unknown time"
                budget = project.get("budget", {})
                budget_str = f"${budget.get('minimum', 0)}-${budget.get('maximum', 0)}"
                project_currency = self._get_project_currency(project)
                
                logger.info(f"🎯 User {user_id}: Bidding on '{project_title}...' - {posted_time} - {budget_str} ({project_currency}) - {project.get('bid_stats', {}).get('bid_count', 0)} bids")
                
                try:
                    bid_result = await self._bid_on_project(user_id, project, settings)
                    if bid_result == True:
                        logger.info(f"✅ User {user_id}: Successfully placed bid on fresh project!")
                        
                        # PERFORMANCE: Invalidate cache after successful bid
                        self._invalidate_bid_history_cache(user_id)
                        
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
            
            # Count how many were skipped due to bid history
            skipped_count = 0
            for project in filtered_projects:
                project_id = str(project.get("id"))
                if await self._has_bid_history(user_id, project_id):
                    skipped_count += 1
            
            if skipped_count > 0:
                logger.info(f"   ⏭️  Already attempted: {skipped_count}")
                logger.info(f"   🆕 New projects to try: {len(filtered_projects) - skipped_count}")
            
            logger.info(f"   💡 Result: No successful bids this cycle")
            
            return False

        except Exception as e:
            logger.error(f"Error in bid cycle for User {user_id}: {e}")
            return False

    async def _fetch_projects_with_fallbacks(self, user_id: int) -> List[Dict]:
        """PERFORMANCE: Fetch projects with multiple API endpoint fallbacks (optimized order and timeouts)"""
        
        # PERFORMANCE: Try fastest/most reliable strategies first
        strategies = [
            ("recent_all", "All recent projects", 15),      # Fastest, no filtering - 15s timeout
            ("skill_based", "Projects matching user skills", 20),  # More specific - 20s timeout
            ("recommended", "Recommended projects", 15),    # Medium speed - 15s timeout
            ("popular", "Popular projects", 15)             # Fallback - 15s timeout
        ]
        
        for strategy, description, timeout in strategies:
            logger.info(f"🔍 User {user_id}: Trying {description} (timeout: {timeout}s)...")
            try:
                # PERFORMANCE: Add timeout per strategy to fail fast
                projects = await asyncio.wait_for(
                    self._fetch_projects_strategy(user_id, strategy),
                    timeout=timeout
                )
                
                if projects and len(projects) > 0:
                    logger.info(f"✅ User {user_id}: {description} returned {len(projects)} projects")
                    return projects
                else:
                    logger.info(f"❌ User {user_id}: {description} returned no projects")
            except asyncio.TimeoutError:
                logger.warning(f"⏱️  User {user_id}: {description} timed out after {timeout}s")
                continue
            except Exception as e:
                logger.error(f"❌ User {user_id}: {description} failed: {e}")
                continue
        
        logger.warning(f"⚠️  User {user_id}: All API strategies failed")
        return []

    async def _fetch_projects_strategy(self, user_id: int, strategy: str) -> List[Dict]:
        """PERFORMANCE: Fetch projects using specific strategy (using reusable HTTP client)"""
        try:
            from database import SessionLocal
            from models import FreelancerCredentials
            
            db = SessionLocal()
            try:
                credentials = db.query(FreelancerCredentials).filter(
                    FreelancerCredentials.user_id == user_id,
                    FreelancerCredentials.is_validated == True
                ).first()
                
                if not credentials:
                    return []
                
                headers = {"Content-Type": "application/json"}
                cookies_dict = credentials.cookies if credentials.cookies else {}
                
                # PERFORMANCE: Use reusable HTTP client instead of creating new one
                client = await self._get_http_client()
                
                # Build URL based on strategy
                base_url = "https://www.freelancer.com/api/projects/0.1/projects/active/"
                params = "compact=false&limit=20&user_details=true&jobs=true&job_details=true&full_description=true&sort_field=time_submitted&sort_order=desc"
                
                if strategy == "skill_based":
                    # Get user skills first
                    user_skills = await self._get_user_skill_ids(user_id, client, headers, cookies_dict)
                    if user_skills:
                        # Use user's actual skill IDs for API call
                        skills_params = "&".join([f"jobs[]={skill_id}" for skill_id in user_skills[:10]])  # Limit to top 10 skills
                        url = f"{base_url}?{params}&{skills_params}&languages[]=en"
                        logger.info(f"🎯 User {user_id}: Fetching projects for skill IDs: {user_skills[:10]}")
                    else:
                        logger.warning(f"⚠️  User {user_id}: No skills found, falling back to general search")
                        return []
                elif strategy == "recommended":
                    url = f"{base_url}?{params}&user_recommended=true"
                elif strategy == "recent_all":
                    url = f"{base_url}?{params}"
                elif strategy == "popular":
                    url = f"{base_url}?{params}&sort_field=bid_count&sort_order=asc"  # Fewer bids = less competitive
                else:
                    return []
                
                response = await client.get(url, headers=headers, cookies=cookies_dict)
                
                if response.status_code == 200:
                    data = response.json()
                    result = data.get("result", {})
                    if isinstance(result, dict):
                        projects = result.get("projects", [])
                        logger.info(f"✅ User {user_id}: {strategy} strategy returned {len(projects)} projects")
                        return projects
                elif response.status_code == 401:
                    logger.error(f"🔐 User {user_id}: Credentials expired in {strategy} strategy")
                    await self._mark_credentials_expired(user_id)
                    return []
                else:
                    logger.warning(f"⚠️  User {user_id}: {strategy} strategy failed with status {response.status_code}")
                
                return []
                    
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error in {strategy} strategy for User {user_id}: {e}")
            return []

    async def _get_user_skill_ids(self, user_id: int, client, headers: Dict, cookies_dict: Dict) -> List[int]:
        """Get user skill IDs from profile with enhanced extraction"""
        try:
            # Get full user profile with all skill details
            user_profile_url = "https://www.freelancer.com/api/users/0.1/self?jobs=true&job_details=true&webapp=1&compact=false"
            user_response = await client.get(user_profile_url, headers=headers, cookies=cookies_dict)
            
            if user_response.status_code == 200:
                user_data = user_response.json()
                user_profile = user_data.get("result", {})
                
                skill_ids = []
                
                # Extract from jobs field (primary skills)
                if user_profile.get("jobs"):
                    for job in user_profile["jobs"]:
                        if isinstance(job, dict) and job.get("id"):
                            skill_ids.append(int(job["id"]))
                        elif isinstance(job, int):
                            skill_ids.append(job)
                
                # Also check for skills field (some profiles use this)
                if user_profile.get("skills"):
                    for skill in user_profile["skills"]:
                        if isinstance(skill, dict) and skill.get("id"):
                            skill_ids.append(int(skill["id"]))
                        elif isinstance(skill, int):
                            skill_ids.append(skill)
                
                # Remove duplicates and return
                unique_skill_ids = list(set(skill_ids))
                logger.info(f"✅ User {user_id}: Found {len(unique_skill_ids)} skill IDs from profile: {unique_skill_ids}")
                return unique_skill_ids
            
            elif user_response.status_code == 401:
                logger.error(f"🔐 User {user_id}: Credentials expired while getting skills")
                return []
            
            logger.warning(f"⚠️  User {user_id}: Failed to get user skills - status {user_response.status_code}")
            return []
            
        except Exception as e:
            logger.error(f"❌ Error getting user skills for User {user_id}: {e}")
            return []



    def _extract_project_skills(self, project: Dict) -> List[Dict]:
        """Enhanced skill extraction from project data with multiple fallback methods"""
        extracted_skills = []
        
        # Method 1: Extract from 'jobs' field (most common)
        if project.get("jobs"):
            jobs = project["jobs"]
            if isinstance(jobs, list):
                for job in jobs:
                    if isinstance(job, dict):
                        skill_info = {
                            "id": job.get("id"),
                            "name": (job.get("name") or job.get("job_name") or 
                                   job.get("title") or job.get("skill_name") or job.get("label")),
                            "category": job.get("category"),
                            "expertise_level": job.get("expertise_level")
                        }
                        if skill_info["id"] or skill_info["name"]:
                            extracted_skills.append(skill_info)
                    elif isinstance(job, str):
                        extracted_skills.append({"id": None, "name": job, "category": None})
        
        # Method 2: Extract from 'skills' field (alternative structure)
        if project.get("skills"):
            skills = project["skills"]
            if isinstance(skills, list):
                for skill in skills:
                    if isinstance(skill, dict):
                        skill_info = {
                            "id": skill.get("id") or skill.get("skill_id"),
                            "name": (skill.get("name") or skill.get("skill_name") or 
                                   skill.get("title") or skill.get("label")),
                            "category": skill.get("category")
                        }
                        if skill_info["id"] or skill_info["name"]:
                            extracted_skills.append(skill_info)
                    elif isinstance(skill, str):
                        extracted_skills.append({"id": None, "name": skill, "category": None})
        
        # Method 3: Extract from 'categories' field
        if project.get("categories"):
            categories = project["categories"]
            if isinstance(categories, list):
                for category in categories:
                    if isinstance(category, dict):
                        skill_info = {
                            "id": category.get("id"),
                            "name": category.get("name") or category.get("category_name"),
                            "category": "category"
                        }
                        if skill_info["id"] or skill_info["name"]:
                            extracted_skills.append(skill_info)
        
        # Method 4: Extract from 'job_details' field (detailed structure)
        if project.get("job_details"):
            job_details = project["job_details"]
            if isinstance(job_details, list):
                for detail in job_details:
                    if isinstance(detail, dict):
                        skill_info = {
                            "id": detail.get("job_id") or detail.get("id"),
                            "name": detail.get("job_name") or detail.get("name"),
                            "category": detail.get("category")
                        }
                        if skill_info["id"] or skill_info["name"]:
                            extracted_skills.append(skill_info)
        
        # Method 5: Extract from 'required_skills' field
        if project.get("required_skills"):
            req_skills = project["required_skills"]
            if isinstance(req_skills, list):
                for skill in req_skills:
                    if isinstance(skill, dict):
                        skill_info = {
                            "id": skill.get("id"),
                            "name": skill.get("name"),
                            "required": True
                        }
                        if skill_info["id"] or skill_info["name"]:
                            extracted_skills.append(skill_info)
        
        # Remove duplicates based on ID or name
        unique_skills = []
        seen_ids = set()
        seen_names = set()
        
        for skill in extracted_skills:
            skill_id = skill.get("id")
            skill_name = skill.get("name")
            
            if skill_id and skill_id not in seen_ids:
                seen_ids.add(skill_id)
                unique_skills.append(skill)
            elif skill_name and skill_name.lower().strip() not in seen_names and not skill_id:
                seen_names.add(skill_name.lower().strip())
                unique_skills.append(skill)
        
        return unique_skills
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

    def _extract_project_skills(self, project: Dict) -> List[Dict]:
        """Enhanced skill extraction from project data with multiple fallback methods"""
        extracted_skills = []
        
        # Method 1: Extract from 'jobs' field (most common)
        if project.get("jobs"):
            jobs = project["jobs"]
            if isinstance(jobs, list):
                for job in jobs:
                    if isinstance(job, dict):
                        skill_info = {
                            "id": job.get("id"),
                            "name": (job.get("name") or job.get("job_name") or 
                                   job.get("title") or job.get("skill_name") or job.get("label")),
                            "category": job.get("category"),
                            "expertise_level": job.get("expertise_level")
                        }
                        if skill_info["id"] or skill_info["name"]:
                            extracted_skills.append(skill_info)
                    elif isinstance(job, str):
                        extracted_skills.append({"id": None, "name": job, "category": None})
        
        # Method 2: Extract from 'skills' field (alternative structure)
        if project.get("skills"):
            skills = project["skills"]
            if isinstance(skills, list):
                for skill in skills:
                    if isinstance(skill, dict):
                        skill_info = {
                            "id": skill.get("id") or skill.get("skill_id"),
                            "name": (skill.get("name") or skill.get("skill_name") or 
                                   skill.get("title") or skill.get("label")),
                            "category": skill.get("category")
                        }
                        if skill_info["id"] or skill_info["name"]:
                            extracted_skills.append(skill_info)
                    elif isinstance(skill, str):
                        extracted_skills.append({"id": None, "name": skill, "category": None})
        
        # Method 3: Extract from 'categories' field
        if project.get("categories"):
            categories = project["categories"]
            if isinstance(categories, list):
                for category in categories:
                    if isinstance(category, dict):
                        skill_info = {
                            "id": category.get("id"),
                            "name": category.get("name") or category.get("category_name"),
                            "category": "category"
                        }
                        if skill_info["id"] or skill_info["name"]:
                            extracted_skills.append(skill_info)
        
        # Method 4: Extract from 'job_details' field (detailed structure)
        if project.get("job_details"):
            job_details = project["job_details"]
            if isinstance(job_details, list):
                for detail in job_details:
                    if isinstance(detail, dict):
                        skill_info = {
                            "id": detail.get("job_id") or detail.get("id"),
                            "name": detail.get("job_name") or detail.get("name"),
                            "category": detail.get("category")
                        }
                        if skill_info["id"] or skill_info["name"]:
                            extracted_skills.append(skill_info)
        
        # Method 5: Extract from 'required_skills' field
        if project.get("required_skills"):
            req_skills = project["required_skills"]
            if isinstance(req_skills, list):
                for skill in req_skills:
                    if isinstance(skill, dict):
                        skill_info = {
                            "id": skill.get("id"),
                            "name": skill.get("name"),
                            "required": True
                        }
                        if skill_info["id"] or skill_info["name"]:
                            extracted_skills.append(skill_info)
        
        # Remove duplicates based on ID or name
        unique_skills = []
        seen_ids = set()
        seen_names = set()
        
        for skill in extracted_skills:
            skill_id = skill.get("id")
            skill_name = skill.get("name")
            
            if skill_id and skill_id not in seen_ids:
                seen_ids.add(skill_id)
                unique_skills.append(skill)
            elif skill_name and skill_name.lower().strip() not in seen_names and not skill_id:
                seen_names.add(skill_name.lower().strip())
                unique_skills.append(skill)
        
        return unique_skills

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

    async def _validate_user_skills_for_project(self, user_id: int, project: Dict) -> bool:
        """Validate if user has required skills for the project before bidding"""
        try:
            from database import SessionLocal
            from models import FreelancerCredentials
            
            db = SessionLocal()
            try:
                credentials = db.query(FreelancerCredentials).filter(
                    FreelancerCredentials.user_id == user_id,
                    FreelancerCredentials.is_validated == True
                ).first()
                
                if not credentials:
                    logger.warning(f"⚠️  User {user_id}: No valid credentials for skill validation")
                    return False
                
                # Get user's actual skills from Freelancer profile
                headers = {"Content-Type": "application/json"}
                cookies_dict = credentials.cookies if credentials.cookies else {}
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    user_skills = await self._get_user_skill_ids(user_id, client, headers, cookies_dict)
                    
                    if not user_skills:
                        logger.warning(f"⚠️  User {user_id}: No skills found in profile")
                        return False
                    
                    # Extract project skills using enhanced method
                    project_skills = self._extract_project_skills(project)
                    
                    if not project_skills:
                        logger.info(f"✅ User {user_id}: No specific skills required for project - allowing bid")
                        return True
                    
                    # Check if user has any of the required skills (by ID)
                    project_skill_ids = [skill.get("id") for skill in project_skills if skill.get("id")]
                    
                    if project_skill_ids:
                        matching_skills = set(user_skills) & set(project_skill_ids)
                        if matching_skills:
                            logger.info(f"✅ User {user_id}: Has {len(matching_skills)} matching skill IDs: {matching_skills}")
                            return True
                        else:
                            logger.warning(f"❌ User {user_id}: No matching skill IDs. User: {user_skills[:5]}..., Project: {project_skill_ids[:5]}...")
                            return False
                    else:
                        # Fallback to name-based matching if no IDs available
                        project_skill_names = [skill.get("name").lower() for skill in project_skills if skill.get("name")]
                        
                        # Get user skill names for comparison (would need additional API call)
                        logger.info(f"ℹ️  User {user_id}: Using skill ID validation only (no name fallback)")
                        return len(user_skills) > 0  # Allow if user has any skills
                        
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Error validating skills for User {user_id}: {e}")
            return True  # Allow bidding if validation fails (fail-safe)

    def _filter_projects(self, projects: List[Dict], settings: Dict, user_selected_skills: List[str] = None, user_selected_skill_ids: List[int] = None) -> List[Dict]:
        """Filter projects based on settings - focus on currency, freshness, and skill matching"""
        
        max_bids = settings.get("max_project_bids", 50)
        supported_currencies = settings.get("currencies", ["USD"])
        min_skill_match = settings.get("min_skill_match", 1)
        max_age_minutes = 15  # Only bid on projects posted within last 15 minutes for freshness
        
        filtered = []
        import time
        current_timestamp = time.time()

        # Track filtering statistics
        filter_stats = {
            "total": len(projects),
            "currency_rejected": 0,
            "age_rejected": 0,
            "skill_rejected": 0,
            "bid_count_rejected": 0,
            "passed": 0
        }

        logger.info(f"🔍 Filter: Max bids: {max_bids}, Currencies: {supported_currencies}, Min skills: {min_skill_match}")
        if user_selected_skill_ids:
            logger.info(f"🎯 User skill IDs for matching: {user_selected_skill_ids[:5]}...")

        for project in projects:
            project_id = project.get("id")
            project_title = project.get("title", "Unknown")[:40]
            
            # 1. Check currency
            project_currency = self._get_project_currency(project)
            if project_currency not in supported_currencies:
                filter_stats["currency_rejected"] += 1
                continue

            # 2. Check age - only fresh projects (≤15 minutes)
            time_submitted = project.get("time_submitted", 0)
            if time_submitted > 0:
                age_minutes = (current_timestamp - time_submitted) / 60
                if age_minutes > max_age_minutes:
                    filter_stats["age_rejected"] += 1
                    continue
                else:
                    posted_time = self._format_time_ago(time_submitted)
                    logger.info(f"✅ Fresh project: {posted_time} - {project_currency}")
            else:
                filter_stats["age_rejected"] += 1
                continue

            # 2.5. Check commission projects setting
            commission_projects_enabled = settings.get("commission_projects", True)
            if not commission_projects_enabled:
                # Check if "Commission" appears in the project title
                if "commission" in project_title.lower():
                    logger.info(f"⏭️  Skipping commission project (found 'Commission' in title): {project_title}")
                    if "commission_rejected" not in filter_stats:
                        filter_stats["commission_rejected"] = 0
                    filter_stats["commission_rejected"] += 1
                    continue

            # 3. Check skill matching - ENHANCED VERSION with ID-based matching
            if min_skill_match > 0:
                # Use enhanced skill extraction
                project_skills_data = self._extract_project_skills(project)
                project_skill_ids = [skill.get("id") for skill in project_skills_data if skill.get("id")]
                project_skill_names = [skill.get("name") for skill in project_skills_data if skill.get("name")]
                project_skill_names = list(set([skill.strip() for skill in project_skill_names if skill and skill.strip()]))
                
                if not project_skill_ids and not project_skill_names:
                    logger.info(f"⚠️  No skills extracted from project - allowing (may be general project)")
                elif not user_selected_skills and not user_selected_skill_ids:
                    logger.info(f"✅ No skill filter configured - allowing project")
                else:
                    # PRIORITY 1: Try ID-based matching first (most accurate)
                    matching_skills = []
                    if user_selected_skill_ids and project_skill_ids:
                        matching_skill_ids = set(user_selected_skill_ids) & set(project_skill_ids)
                        if matching_skill_ids:
                            matching_skills = [f"ID:{sid}" for sid in matching_skill_ids]
                            logger.info(f"🎯 ID MATCH: Found {len(matching_skills)} matching skill IDs: {list(matching_skill_ids)[:3]}...")
                    
                    # PRIORITY 2: If no ID matches, try name-based matching
                    if not matching_skills and user_selected_skills and project_skill_names:
                        user_skills_lower = [skill.lower().strip() for skill in user_selected_skills]
                        project_skills_lower = [skill.lower().strip() for skill in project_skill_names]
                        
                        # Exact match first
                        for user_skill in user_selected_skills:
                            if user_skill in project_skill_names:
                                matching_skills.append(user_skill)
                        
                        # Case-insensitive match
                        if not matching_skills:
                            for i, user_skill_lower in enumerate(user_skills_lower):
                                for j, project_skill_lower in enumerate(project_skills_lower):
                                    if user_skill_lower == project_skill_lower:
                                        matching_skills.append(user_selected_skills[i])
                                        break
                        
                        # Partial/substring matching as fallback
                        if not matching_skills:
                            for user_skill in user_selected_skills:
                                user_skill_lower = user_skill.lower().strip()
                                for project_skill in project_skill_names:
                                    project_skill_lower = project_skill.lower().strip()
                                    if (user_skill_lower in project_skill_lower or 
                                        project_skill_lower in user_skill_lower):
                                        matching_skills.append(user_skill)
                                        break
                        
                        if matching_skills:
                            logger.info(f"🎯 NAME MATCH: Found {len(matching_skills)} matching skill names: {matching_skills[:3]}...")
                    
                    skill_match_count = len(set(matching_skills))  # Remove duplicates
                    
                    if skill_match_count < min_skill_match:
                        # Check for 50% fallback
                        skill_match_percentage = (skill_match_count / min_skill_match) * 100
                        if skill_match_percentage >= 50:
                            logger.info(f"🔄 FALLBACK: Need {min_skill_match} skills, found {skill_match_count} ({skill_match_percentage:.0f}% ≥ 50%) - ALLOWING")
                        else:
                            logger.info(f"❌ SKILL MISMATCH: Need {min_skill_match}, found {skill_match_count} ({skill_match_percentage:.0f}% < 50%)")
                            if user_selected_skills:
                                logger.info(f"   User skills: {user_selected_skills[:3]}...")
                            if user_selected_skill_ids:
                                logger.info(f"   User skill IDs: {user_selected_skill_ids[:3]}...")
                            if project_skill_names:
                                logger.info(f"   Project skills: {project_skill_names[:3]}...")
                            if project_skill_ids:
                                logger.info(f"   Project skill IDs: {project_skill_ids[:3]}...")
                            filter_stats["skill_rejected"] += 1
                            continue
                    else:
                        logger.info(f"🎯 SKILL MATCH: {skill_match_count}/{min_skill_match} - {matching_skills[:2]}...")

            # 4. Check bid count
            bid_count = project.get("bid_stats", {}).get("bid_count", 0)
            if bid_count > max_bids:
                filter_stats["bid_count_rejected"] += 1
                continue

            logger.info(f"🎯 Project PASSED all filters - adding to bid list")
            filtered.append(project)
            filter_stats["passed"] += 1
        
        # Enhanced logging with breakdown
        logger.info(f"🔍 Filter result: {len(filtered)}/{len(projects)} projects passed")
        logger.info(f"📊 Filter breakdown:")
        logger.info(f"   ✅ Passed all filters: {filter_stats['passed']}")
        logger.info(f"   💰 Currency rejected: {filter_stats['currency_rejected']}")
        logger.info(f"   ⏰ Age rejected (>15min): {filter_stats['age_rejected']}")
        if filter_stats.get('commission_rejected', 0) > 0:
            logger.info(f"   💼 Commission rejected: {filter_stats['commission_rejected']}")
        logger.info(f"   🎯 Skill rejected (<{min_skill_match} or <50%): {filter_stats['skill_rejected']}")
        logger.info(f"   📈 Bid count rejected (>{max_bids}): {filter_stats['bid_count_rejected']}")
        
        return filtered

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
            
            async with httpx.AsyncClient(timeout=300.0) as client:
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
        """PERFORMANCE: Save bid attempt to database and invalidate cache"""
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
                
                # PERFORMANCE: Invalidate cache after saving
                user_id = bid_data.get("user_id", 1)
                self._invalidate_bid_history_cache(user_id)
                
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

# Singleton accessor
bidder = AutoBidder()