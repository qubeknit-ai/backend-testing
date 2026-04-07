import asyncio
import logging
import os
import sys
import json
from datetime import datetime


# Set level
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("UpworkCron")

# Ensure backend directory is in path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from autobidder.upwork_bidder import UpworkAutoBidder

async def run_upwork_cron():
    logger.info(f"⏰ Starting Upwork Cron Cycle at {datetime.now()}")
    
    bidder = UpworkAutoBidder()
    try:
        # Run one cycle
        results = await bidder.run_cycle_batch()
        logger.info(f"📊 Upwork Cycle Results: {json.dumps(results, indent=2)}")
        logger.info("✅ Upwork Cron Cycle Complete")
    except Exception as e:

        logger.error(f"❌ Upwork Cron Cycle Failed: {e}")
        import traceback
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    asyncio.run(run_upwork_cron())
