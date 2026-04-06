import asyncio
import logging
from datetime import datetime, timedelta
import random
import json
import time
from typing import Optional, Dict, Any, List
import httpx

logger = logging.getLogger("AutoBidder")

class AutoBidderSchedulerMixin:
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

    async def run_cycle_batch(self):
        """Execute one parallel bidding cycle for all enabled users (Single Iteration)"""
        logger.info("AutoBidder Cycle Batch Initiated")
        
        from database import SessionLocal
        from models import AutoBidSettings as DBAutoBidSettings
        
        db = SessionLocal()
        results_summary = {
            "total_enabled_users": 0,
            "active_users": [],
            "successful_bids": 0,
            "failed_users": 0,
            "skipped_users": 0,
            "timestamp": datetime.now().isoformat()
        }
        
        try:
            # Fetch ALL enabled auto-bid settings
            enabled_settings = db.query(DBAutoBidSettings).filter(
                DBAutoBidSettings.enabled == True
            ).all()
            
            results_summary["total_enabled_users"] = len(enabled_settings)
            
            if not enabled_settings:
                logger.info("😴 No users have auto-bidding enabled")
                return results_summary
            
            logger.info(f"👥 Found {len(enabled_settings)} users with auto-bidding enabled")
            
            # Prepare tasks for parallel processing
            tasks = []
            
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
                    results_summary["skipped_users"] += 1
                    continue
                
                logger.info(f"🔄 Adding User {user_id} to parallel processing queue")
                results_summary["active_users"].append(user_id)
                
                # Stagger the start to avoid slamming the API at the same millisecond
                await asyncio.sleep(random.uniform(0.5, 2.5))
                
                # Create task for parallel execution
                task = asyncio.create_task(self._run_bid_cycle(user_id, settings))
                tasks.append((user_id, task))
            
            # Process all users in parallel
            if tasks:
                logger.info(f"🚀 Processing {len(tasks)} users in parallel...")
                
                # Run all users simultaneously
                results = await asyncio.gather(*[task for _, task in tasks], return_exceptions=True)
                
                # Update last bid times for successful bids and handle results
                current_time = datetime.now()
                
                for i, (user_id, _) in enumerate(tasks):
                    result = results[i]
                    
                    if isinstance(result, Exception):
                        logger.error(f"❌ User {user_id}: Exception during parallel processing: {result}")
                        self._handle_user_failure(user_id)
                        results_summary["failed_users"] += 1
                    elif result is True:  # Successful bid
                        self._user_last_bid_time[user_id] = current_time
                        self._handle_user_success(user_id)
                        results_summary["successful_bids"] += 1
                        logger.info(f"✅ User {user_id}: Successful bid placed in parallel processing")
                    elif result == "BID_LIMIT_REACHED":
                        logger.error(f"🚫 User {user_id}: Bid limit reached during parallel processing")
                        self._handle_user_failure(user_id)
                        results_summary["failed_users"] += 1
                    elif result == "CREDENTIALS_EXPIRED":
                        logger.error(f"🔐 User {user_id}: Credentials expired during parallel processing")
                        self._handle_user_failure(user_id)
                        results_summary["failed_users"] += 1
                    else:
                        logger.info(f"ℹ️  User {user_id}: No bid placed during parallel processing")
                        self._handle_user_failure(user_id)
                        results_summary["failed_users"] += 1
                
                logger.info(f"📊 Batch complete: {results_summary['successful_bids']}/{len(tasks)} users placed successful bids")
            else:
                logger.info("⏭️  No users ready for processing this batch")
            
            return results_summary
            
        except Exception as e:
            logger.error(f"Error in run_cycle_batch: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return results_summary
        finally:
            db.close()

    async def _loop(self):
        """Main bidding loop that handles multiple users in parallel"""
        logger.info("AutoBidder Loop Initiated (Multi-User Parallel Mode)")
        
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
                
                # Execute one cycle batch
                results = await self.run_cycle_batch()
                
                successful_bids = results["successful_bids"]
                active_users_count = len(results["active_users"])
                
                # Enhanced monitoring
                cycle_count += 1
                if successful_bids > 0:
                    consecutive_empty_cycles = 0
                    last_successful_bid_time = datetime.now()
                else:
                    consecutive_empty_cycles += 1
                
                # Alert if no bids for too long
                if consecutive_empty_cycles >= 10:  # 10 cycles without bids
                    logger.warning(f"🚨 ALERT: {consecutive_empty_cycles} consecutive cycles without successful bids!")
                
                # Periodic health check every 20 cycles
                if cycle_count % 20 == 0:
                    # Fetch settings again for health check report
                    from database import SessionLocal
                    from models import AutoBidSettings as DBAutoBidSettings
                    db = SessionLocal()
                    try:
                        enabled_settings = db.query(DBAutoBidSettings).filter(DBAutoBidSettings.enabled == True).all()
                        await self._health_check_report(enabled_settings, consecutive_empty_cycles)
                    finally:
                        db.close()
                
                # Smart wait time
                wait_seconds = 300  # Default 5 minutes
                if results["total_enabled_users"] > 0:
                    wait_seconds = 120  # Fast check if users available
                
                logger.info(f"✅ Cycle {cycle_count} complete. Waiting {wait_seconds}s...")
                await asyncio.sleep(wait_seconds)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in AutoBidder loop: {e}")
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
                # Always fetch actual skill IDs from the user's Freelancer.com profile
                # This ensures we don't bid on projects that the user doesn't have skills for
                if credentials:
                    user_selected_skills = credentials.selected_skills if credentials.selected_skills else []
                    logger.info(f"🎯 User {user_id}: Using {len(user_selected_skills)} selected skills from DB: {user_selected_skills}")
                    
                    # Fetch ACTUAL skill IDs from Freelancer API for all users
                    headers = {"Content-Type": "application/json"}
                    cookies_dict = credentials.cookies if credentials.cookies else {}
                    
                    client = await self._get_http_client()
                    user_skill_ids = await self._get_user_skill_ids(user_id, client, headers, cookies_dict)
                    user_selected_skill_ids = user_skill_ids
                    
                    if user_selected_skill_ids:
                        logger.info(f"🎯 User {user_id}: Verified {len(user_selected_skill_ids)} profile skills on Freelancer.com")
                    else:
                        logger.warning(f"⚠️  User {user_id}: No skills found on Freelancer.com profile. This user cannot bid on any projects.")
                else:
                    logger.error(f"❌ User {user_id}: No credentials found in database")
                    return False
            except Exception as e:
                logger.error(f"❌ Error getting selected skills for User {user_id}: {e}")
            finally:
                db.close()
            
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

