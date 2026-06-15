from playwright.async_api import async_playwright
import asyncio

class BrowserSolver:
    @staticmethod
    async def solve_cloudflare(url: str):
        """
        Launches headless chromium to solve turnstile/cloudflare
        and returns the clearance cookies.
        """
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            
            try:
                await page.goto(url, wait_until="networkidle")
                # Wait for Cloudflare challenge to pass
                await page.wait_for_selector("title", state="attached", timeout=15000)
                
                cookies = await context.cookies()
                
                # Format cookies for curl_cffi
                formatted_cookies = {cookie['name']: cookie['value'] for cookie in cookies}
                return formatted_cookies
            except Exception as e:
                print(f"Error solving Cloudflare: {e}")
                return {}
            finally:
                await browser.close()
