import asyncio
import httpx
import json

async def test():
    url = "https://www.freelancer.com/api/projects/0.1/projects/active/?limit=2"
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        data = response.json()
        with open("scratch/freelancer_active.json", "w") as f:
            json.dump(data, f, indent=2)

if __name__ == "__main__":
    asyncio.run(test())
