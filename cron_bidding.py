import asyncio
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

# Ensure we can import from the current directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Load environment variables
load_dotenv()

from autobid_service import bidder
from database import engine, SessionLocal

async def run_cron_cycle():
    """ Execute a single bidding cycle for all enabled users """
    print(f"--- 🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    print("🚀 AutoBidder GitHub Action: Starting Cron Cycle...")
    
    try:
        # Trigger the batch logic (handles all enabled users in parallel)
        results = await bidder.run_cycle_batch()
        
        print("📊 CRON CYCLE SUMMARY:")
        print(f"   👥 Total Enabled Users: {results.get('total_enabled_users', 0)}")
        print(f"   ✅ Successful Bids: {results.get('successful_bids', 0)}")
        print(f"   ⏸️  Skipped Users: {results.get('skipped_users', 0)}")
        print(f"   ❌ Failed Users: {results.get('failed_users', 0)}")
        
        if results.get('active_users'):
             print(f"   👤 Active User IDs: {results.get('active_users')}")
        
        print("✅ Cron Cycle Completed Successfully.")
        
    except Exception as e:
        print(f"❌ CRITICAL ERROR in Cron Cycle: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    # Run the async cycle
    asyncio.run(run_cron_cycle())
