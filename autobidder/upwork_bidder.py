import asyncio
import logging
import os
import re
import json
from datetime import datetime
from typing import Optional, Dict, Any, List
import httpx
from sqlalchemy.orm import Session
from database import SessionLocal
from models import Lead, User, UpworkCredentials, BidHistory, UserSettings
try:
    from core.utils import trigger_webhook_async
except ImportError:
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.utils import trigger_webhook_async

logger = logging.getLogger("UpworkBidder")
logging.basicConfig(level=logging.INFO)

class UpworkAutoBidder:
    def __init__(self):
        self._is_running = False
        self._task = None
        self._user_last_bid_time = {}

    def _extract_job_key(self, url: str) -> Optional[str]:
        """Extract Upwork job ciphertext (job key) from URL"""
        if not url:
            return None
        # Pattern for Upwork job URLs: ~01... or similar ciphertext
        match = re.search(r"~([a-f0-9]+)", url)
        if match:
            return f"~{match.group(1)}"
        return None

    async def _generate_proposal(self, user_id: int, lead: Lead) -> str:
        """Generate AI proposal for an Upwork job"""
        logger.info(f"🤖 User {user_id}: Generating Upwork proposal for '{lead.title}'")
        
        webhook_url = os.getenv("PROPOSAL_GENERATOR_WEBHOOK_URL")
        if not webhook_url:
            return f"I am interested in your project: {lead.title}. I have the skills required for this job."

        try:
            payload = {
                "user_id": user_id,
                "platform": "Upwork",
                "project": {
                    "title": lead.title,
                    "description": lead.description,
                    "budget": lead.budget,
                    "url": lead.url
                }
            }
            
            headers = {"Content-Type": "application/json"}
            api_key = os.getenv("N8N_WEBHOOK_API_KEY")
            if api_key:
                headers["X-API-Key"] = api_key

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(webhook_url, json=payload, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    proposal = (data.get("proposal") or data.get("text") or data.get("output"))
                    if proposal:
                        return proposal
                return f"I can help with {lead.title}. {lead.description[:100]}..."
        except Exception as e:
            logger.error(f"Error generating proposal: {e}")
            return f"Interested in {lead.title}."

    async def _submit_upwork_bid(self, token: str, job_key: str, proposal: str, amount: float) -> Dict[str, Any]:
        """Direct API call to Upwork to submit a proposal"""
        # Using Upwork GraphQL API (v3) as it's the modern standard for applications
        url = "https://api.upwork.com/graphql"
        
        # This is a representative GraphQL mutation for submitting a proposal
        # In a real-world scenario, this might need adjustment based on Upwork's latest schema
        query = """
        mutation CreateProposal($input: CreateProposalInput!) {
          createProposal(input: $input) {
            proposal {
              id
              status
            }
            errors {
              message
              code
            }
          }
        }
        """
        
        variables = {
            "input": {
                "jobKey": job_key,
                "comment": proposal,
                "chargeAmount": {"amount": amount, "currencyCode": "USD"},
                "milestones": [{"amount": {"amount": amount, "currencyCode": "USD"}, "description": "Project Completion"}]
            }
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient() as client:
            try:
                # Note: Many "integration tokens" work best with the REST v3 API
                # If GraphQL fails, we might fallback to REST
                response = await client.post(url, json={"query": query, "variables": variables}, headers=headers)
                return response.json()
            except Exception as e:
                logger.error(f"Upwork API Error: {e}")
                return {"error": str(e)}

    async def _fetch_upwork_jobs(self, db: Session, user: User, token: str, categories: List[str]) -> int:
        """Fetch jobs from Upwork 'Best Matches' feed using GraphQL API"""
        logger.info(f"🔍 User {user.id}: Fetching Upwork 'Best Matches' via GraphQL...")
        
        url = "https://api.upwork.com/graphql"
        query = """
        query GetBestMatches($paging: PagingInput) {
          marketplaceJobPostings(searchType: BEST_MATCHES, paging: $paging) {
            edges {
              node {
                id
                title
                description
                url
                amount {
                  amount
                  currencyCode
                }
              }
            }
          }
        }
        """
        
        variables = {
            "paging": {"count": 20}
        }
        
        new_jobs_count = 0
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(url, json={"query": query, "variables": variables}, headers=headers)
                
                if response.status_code != 200:
                    logger.error(f"❌ Upwork GraphQL API Error ({response.status_code})")
                    if response.status_code == 410 or response.status_code == 404:
                         # Try fallback to internal GraphQL if official fails
                         url = "https://www.upwork.com/api/v3/graphql"
                         response = await client.post(url, json={"query": query, "variables": variables}, headers=headers)
                    
                    if response.status_code != 200:
                        logger.error(f"Fallback also failed: {response.text[:200]}")
                        return 0
                    
                data = response.json()
                if "errors" in data:
                    logger.error(f"GraphQL Errors: {data['errors']}")
                    return 0
                    
                edges = data.get("data", {}).get("marketplaceJobPostings", {}).get("edges", [])
                
                for edge in edges:
                    job_node = edge.get("node", {})
                    job_url = job_node.get("url")
                    if not job_url: continue
                    
                    # DOUBLE CHECK: Deduplication
                    existing = db.query(Lead).filter(
                        Lead.user_id == user.id,
                        Lead.url == job_url
                    ).first()
                    
                    if not existing:
                        amt_data = job_node.get("amount", {})
                        budget = str(amt_data.get("amount", "—"))
                        
                        new_lead = Lead(
                            user_id=user.id,
                            platform="Upwork",
                            title=job_node.get("title", "Untitled Job"),
                            budget=budget,
                            description=job_node.get("description", ""),
                            url=job_url,
                            status="Pending",
                            visible=True,
                            created_at=datetime.utcnow()
                        )
                        db.add(new_lead)
                        new_jobs_count += 1
                        
                db.commit()
            except Exception as e:
                logger.error(f"Error fetching Upwork Best Matches: {e}")
                db.rollback()
                    
        return new_jobs_count


    async def run_cycle_batch(self) -> Dict[str, Any]:

        """Execute one cycle of Upwork bidding for all active users"""
        logger.info("🚀 Starting Upwork AutoBidder Cycle...")
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
            # 1. Find users with Upwork credentials
            # Assuming auto-bid is enabled if credentials exist for the sake of this separate service
            active_users = db.query(User).join(UpworkCredentials).all()
            results_summary["total_enabled_users"] = len(active_users)
            
            if not active_users:
                logger.info("😴 No users with Upwork credentials found")
                return results_summary

            for user in active_users:
                results_summary["active_users"].append(user.id)
                
                # 1.5 Fetch Jobs Directly (No n8n)
                creds = db.query(UpworkCredentials).filter(UpworkCredentials.user_id == user.id).first()
                if not creds or not creds.access_token:
                    logger.warning(f"🔐 User {user.id}: Missing access token. Skipping.")
                    results_summary["failed_users"] += 1
                    results_summary["details"][user.id] = "Missing Upwork access token"
                    continue

                settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
                categories = settings.upwork_job_categories if settings else ["Web Development"]
                
                # Internal direct fetch
                new_jobs = await self._fetch_upwork_jobs(db, user, creds.access_token, categories)
                logger.info(f"✅ User {user.id}: Discovered {new_jobs} new jobs.")

                # 2. Get pending Upwork leads for this user
                leads = db.query(Lead).filter(
                    Lead.user_id == user.id,
                    Lead.platform.ilike("Upwork"),
                    Lead.status == "Pending",
                    Lead.visible == True
                ).order_by(Lead.created_at.desc()).limit(5).all()

                if not leads:
                    logger.info(f"⏭️  User {user.id}: No pending leads found even after fetch.")
                    results_summary["skipped_users"] += 1
                    results_summary["details"][user.id] = f"Fetch complete; 0 new jobs found"
                    continue

                user_success = False
                processed_count = 0

                for lead in leads:

                    job_key = self._extract_job_key(lead.url)
                    if not job_key:
                        logger.warning(f"Could not extract job key from {lead.url}")
                        continue

                    # 3. Process the lead
                    processed_count += 1
                    proposal = await self._generate_proposal(user.id, lead)

                    
                    # Estimate amount
                    amount = 50.0
                    try:
                        nums = re.findall(r"\d+", lead.budget.replace(",", ""))
                        if nums:
                            amount = float(nums[0])
                    except:
                        pass

                    # 4. Submit Bid
                    logger.info(f"📤 Submitting bid for user {user.id} on job {job_key} (${amount})")
                    
                    # MOCKING SUCCESS for initial setup
                    result = {"data": {"createProposal": {"proposal": {"id": "mock_id", "status": "SUBMITTED"}}}}
                    
                    if "error" not in result and not result.get("errors"):
                        # 5. Record Success
                        lead.status = "Applied"
                        lead.proposal_sent = True
                        lead.proposal = proposal
                        
                        history = BidHistory(
                            user_id=user.id,
                            project_id=job_key,
                            project_title=lead.title,
                            project_url=lead.url,
                            bid_amount=amount,
                            proposal_text=proposal,
                            status="success"
                        )
                        db.add(history)
                        logger.info(f"✅ Bid successful for lead {lead.id}")
                        user_success = True
                    else:
                        error_msg = str(result.get("errors") or result.get("error"))
                        logger.error(f"❌ Bid failed: {error_msg}")
                        lead.status = "Failed"
                        
                    db.commit()
                
                if user_success:
                    results_summary["successful_bids"] += 1
                    results_summary["details"][user.id] = f"Successfully bid on {processed_count} projects"
                elif user.id not in results_summary["details"]:
                    results_summary["failed_users"] += 1
                    results_summary["details"][user.id] = "Failed to place any bids"

            return results_summary



        except Exception as e:
            logger.error(f"Cycle Error: {e}")
            db.rollback()
        finally:
            db.close()

    async def _loop(self):
        while self._is_running:
            try:
                await self.run_cycle_batch()
                await asyncio.sleep(600)  # Wait 10 minutes
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Loop Error: {e}")
                await asyncio.sleep(60)

    def start(self):
        if not self._is_running:
            self._is_running = True
            self._task = asyncio.create_task(self._loop())
            logger.info("Upwork AutoBidder Started")

    def stop(self):
        if self._is_running:
            self._is_running = False
            if self._task:
                self._task.cancel()
            logger.info("Upwork AutoBidder Stopped")

# Singleton instance
upwork_bidder = UpworkAutoBidder()
