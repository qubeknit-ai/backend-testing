import asyncio
import logging
from datetime import datetime, timedelta
import random
import json
import time
from typing import Optional, Dict, Any, List
import httpx

logger = logging.getLogger("AutoBidder")

class AutoBidderFilterMixin:
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

            # 3. Check skill matching - MANDATORY Check to avoid Freelancer.com errors
            # Use enhanced skill extraction
            project_skills_data = self._extract_project_skills(project)
            project_skill_ids = [skill.get("id") for skill in project_skills_data if skill.get("id")]
            project_skill_names = [skill.get("name") for skill in project_skills_data if skill.get("name")]
            project_skill_names = list(set([skill.strip() for skill in project_skill_names if skill and skill.strip()]))
            
            # MANDATORY SAFETY CHECK: If the user has a profile and the project has skills, 
            # we MUST have at least one match, otherwise Freelancer will reject the bid with a 400 error.
            if user_selected_skill_ids and project_skill_ids:
                matching_skill_ids = set(user_selected_skill_ids) & set(project_skill_ids)
                if not matching_skill_ids:
                    logger.info(f"❌ MANDATORY SKILL REJECTION: User has 0 matching skill IDs for this project. Skipping to avoid Freelancer 400 error.")
                    filter_stats["skill_rejected"] += 1
                    continue
            
            # Additional user-defined filter (min_skill_match)
            if min_skill_match > 0:
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

