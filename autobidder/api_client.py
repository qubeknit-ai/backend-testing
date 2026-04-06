import asyncio
import logging
from datetime import datetime, timedelta
import random
import json
import time
from typing import Optional, Dict, Any, List
import httpx

logger = logging.getLogger("AutoBidder")

class AutoBidderAPIMixin:
    async def _get_http_client(self):
        """PERFORMANCE: Get or create reusable HTTP client with connection pooling"""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)
                # Removed http2=True to avoid dependency issues
            )
        return self._http_client

    async def _fetch_projects_with_fallbacks(self, user_id: int) -> List[Dict]:
        """PERFORMANCE: Fetch projects with multiple API endpoint fallbacks (optimized order and timeouts)"""
        
        # PERFORMANCE: Try fastest/most reliable strategies first
        strategies = [
            ("recent_all", "All recent projects", 15),      # 15s timeout
            ("skill_based", "Projects matching user skills", 20),  # 20s timeout
            ("recommended", "Recommended projects", 15),    # 15s timeout
            ("popular", "Popular projects", 15)             # 15s timeout
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

