from models import User
from routers.users import get_freelancer_project_details
import json
import asyncio
from database import SessionLocal

async def test_endpoint():
    db = SessionLocal()
    try:
        # We need a project ID to test
        res = await get_freelancer_project_details(40444977, "feldsheroffical@gmail.com", db)
        print(f"Project ID: {res['id']}")
        print(f"Owner ID returned: {res.get('owner_id')}")
        owner = res.get('owner', {})
        print("Owner Data:")
        print(json.dumps({
            "id": owner.get('id'),
            "status": owner.get('status'),
            "reputation": owner.get('reputation'),
            "verification": owner.get('verification')
        }, indent=2))
    except Exception as e:
        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    asyncio.run(test_endpoint())
