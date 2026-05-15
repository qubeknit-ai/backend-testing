import asyncio
import httpx
import re

async def test_scrape():
    # A real project ID and seo_url
    project_id = 40445011
    seo_url = "algorithm/Quotex-Binary-Bot"
    
    project_url = f"https://www.freelancer.com/projects/{seo_url}"
    print(f"Scraping URL: {project_url}")
    
    scrape_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        html_resp = await client.get(project_url, headers=scrape_headers)
        print(f"Status Code: {html_resp.status_code}")
        
        if html_resp.status_code == 200:
            html = html_resp.text
            
            # Print page title just to be sure we got the page
            title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
            print(f"Page Title: {title_match.group(1) if title_match else 'None'}")
            
            payment_match = re.search(r'<span[^>]*data-color="([^"]+)"[^>]*title="Payment verified"', html)
            if payment_match:
                print(f"Payment match group: {payment_match.group(1)}")
                is_verified = (payment_match.group(1) == 'success')
                print(f"Payment verified: {is_verified}")
            else:
                print("Payment match not found in HTML!")
                
            review_match = re.search(r'<fl-review-count[^>]*>.*?<span[^>]*>\s*(\d+)\s*</span>', html, re.DOTALL)
            if review_match:
                count = int(review_match.group(1))
                print(f"Review count: {count}")
            else:
                print("Review match not found in HTML!")
        else:
            print("Failed to fetch HTML!")

if __name__ == "__main__":
    asyncio.run(test_scrape())
