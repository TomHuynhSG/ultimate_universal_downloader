import asyncio
from playwright.async_api import async_playwright
import time

async def extract_single(browser, url):
    page = await browser.new_page()
    await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "stylesheet", "font", "media"] else route.continue_())
    
    # Just go to reader page directly
    reader_url = url
    
    js_eval = """
    () => {
        return galleryinfo.files.length;
    }
    """
    
    image_urls = None
    max_retries = 3
    for attempt in range(max_retries):
        await page.goto(reader_url, wait_until="domcontentloaded")
        try:
            await page.wait_for_function("typeof galleryinfo !== 'undefined'", timeout=10000)
            image_urls = await page.evaluate(js_eval)
            if image_urls:
                break
        except Exception as e:
            print(f"[{url}] Attempt {attempt+1} failed: {e}")
            
        if attempt < max_retries - 1:
            await asyncio.sleep(2 * (attempt + 1))
            
    await page.close()
    if not image_urls:
        print(f"[{url}] Failed permanently.")
    else:
        print(f"[{url}] Success: {image_urls}")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        urls = [
            "https://hitomi.la/reader/3997723.html#1",
            "https://hitomi.la/reader/2396342.html#1",
            "https://hitomi.la/reader/2126245.html#1",
            "https://hitomi.la/reader/2126246.html#1",
            "https://hitomi.la/reader/2126247.html#1"
        ]
        
        sem = asyncio.Semaphore(1)
        async def fetch_link(link):
            async with sem:
                # Add a 1 second delay between requests to be safe
                await asyncio.sleep(1)
                await extract_single(browser, link)
                
        start = time.time()
        tasks = [fetch_link(link) for link in urls]
        await asyncio.gather(*tasks)
        print(f"Time taken: {time.time() - start:.2f}s")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
