import asyncio
from playwright.async_api import async_playwright
import time

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        # Block images and CSS
        await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "stylesheet", "font", "media"] else route.continue_())
        
        start = time.time()
        print("Navigating to Hitomi...")
        await page.goto("https://hitomi.la/reader/3997723.html#1", wait_until="domcontentloaded")
        
        try:
            await page.wait_for_function("typeof galleryinfo !== 'undefined'", timeout=10000)
            res = await page.evaluate("galleryinfo.files.length")
            print("Files length:", res)
        except Exception as e:
            print("Exception:", e)
            
        print(f"Time taken: {time.time() - start:.2f}s")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
