
import os
import httpx
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)

with engine.connect() as conn:
    row = conn.execute(text("SELECT access_token FROM truelancer_credentials LIMIT 1")).fetchone()
    token = row[0]

async def check_project():
    async with httpx.AsyncClient() as client:
        # Fetch list first to get a valid ID
        resp = await client.post(
            "https://api.truelancer.com/api/v1/projects",
            headers={"Authorization": f"Bearer {token}"},
            json={"page": 1, "per_page": 1, "sort": "newest", "skill_matching": True}
        )
        data = resp.json()
        projects = data.get("projects", {}).get("data", [])
        if projects:
            pid = projects[0].get('id')
            print(f"Testing Project ID: {pid}")
            
            urls = [
                f"https://api.truelancer.com/api/v1/project?id={pid}",
                f"https://api.truelancer.com/api/v1/project/details?id={pid}",
                f"https://api.truelancer.com/api/v1/user/project-details?id={pid}"
            ]
            
            for url in urls:
                print(f"Trying: {url}")
                try:
                    r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
                    print(f"Status: {r.status_code}")
                    if r.status_code == 200:
                        d = r.json()
                        # Look for budget in various possible locations
                        budget = d.get('data', {}).get('budget') or d.get('project', {}).get('budget')
                        currency = d.get('data', {}).get('currency_code') or d.get('project', {}).get('currency_code')
                        print(f"FOUND! Budget: {budget}, Currency: {currency}")
                except Exception as e:
                    print(f"Error: {e}")

import asyncio
asyncio.run(check_project())
