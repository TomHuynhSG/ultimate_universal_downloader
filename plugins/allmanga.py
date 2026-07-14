import re
import urllib.parse
from backend.plugins.base import BaseExtractor
from playwright.async_api import async_playwright
import json

class AllMangaExtractor(BaseExtractor):
    URLS = ['allmanga.to']

    async def extract_single(self, session, manga_id, chapter_string):
        query = '''
        {
          chaptersForRead(mangaId:"%s", translationType:sub, chapterString:"%s") {
            edges {
              pictureUrls
              pictureUrlsProcessed
              pictureUrlHead
            }
          }
        }
        ''' % (manga_id, chapter_string)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://allmanga.to",
            "Referer": "https://allmanga.to/",
            "Content-Type": "application/json"
        }
        
        api_url = "https://api.allanime.day/api"
        r = await session.post(api_url, json={"query": query}, headers=headers)
        data = r.json()
        
        if data.get('errors'):
            err = data['errors'][0].get('message', '')
            if err == "NEED_CAPTCHA":
                raise Exception("NEED_CAPTCHA: Cloudflare block active. Captcha cookies are required.")
            elif "countryOfOrigin" in err:
                raise Exception("TOBEPARSED: API returned encrypted tobeparsed payload. Captcha cookies are required.")
            else:
                raise Exception(f"GraphQL Error: {err}")
                
        try:
            edges = data['data']['chaptersForRead']
            if not edges:
                return []
            
            # Since chaptersForRead returns [Chapter!] we just get it directly. Wait! The introspection said chaptersForRead is ChaptersConnection but my earlier query returned "NEED_CAPTCHA" on it. Let's see what it returns.
            # Usually it returns a list of dictionaries with pictureUrls etc.
            # But earlier chaptersForRead error said 'Field "chaptersForRead" of type "[Chapter!]" ...' no, it said 'Cannot query field "pictureUrls" on type "ChaptersConnection"'.
            # Wait, ChaptersConnection means we need `edges { pictureUrls }`? No, the error said `edges` is `[Chapter!]` in chapterPages.
            # Let me just assume we get pictureUrls from the first edge.
            
            # Since I couldn't fully map the schema due to CAPTCHA, I will handle both list and connection formats.
            if isinstance(edges, list):
                chapter_data = edges[0]
            elif isinstance(edges, dict) and 'edges' in edges:
                chapter_data = edges['edges'][0] if edges['edges'] else {}
            else:
                chapter_data = {}

            pics = chapter_data.get('pictureUrls') or chapter_data.get('pictureUrlsProcessed')
            
            if not pics:
                return []
                
            media_items = []
            for pic in pics:
                img_url = pic
                # If pictureUrls processed has relative path
                if img_url.startswith('/'):
                    img_url = f"https://allanimenews.com{img_url}"
                elif not img_url.startswith('http'):
                    img_url = f"https://allanimenews.com/{img_url}"
                
                media_items.append({
                    "url": img_url,
                    "referer": "https://allanimenews.com",
                    "folder": f"Chapter {chapter_string}"
                })
                
            return media_items
        except Exception as e:
            print(f"Error parsing chapter {chapter_string}: {e}")
            return []

    async def extract(self, session):
        # Determine if it's a chapter or full manga
        # Format: https://allmanga.to/manga/sWvMacT74hFPND7ac or https://allmanga.to/manga/sWvMacT74hFPND7ac/chapter-1-sub
        
        match = re.search(r'/manga/([a-zA-Z0-9]+)', self.url)
        if not match:
            raise Exception("Invalid allmanga URL. Expected /manga/ID format.")
        
        manga_id = match.group(1)
        
        chapter_match = re.search(r'/chapter-([0-9.]+)-', self.url)
        specific_chapter = chapter_match.group(1) if chapter_match else None
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://allmanga.to",
            "Referer": "https://allmanga.to/",
            "Content-Type": "application/json"
        }
        
        api_url = "https://api.allanime.day/api"
        
        # 1. Fetch Manga Metadata
        manga_query = '''
        {
          manga(_id:"%s") {
            name
            thumbnail
            availableChaptersDetail
          }
        }
        ''' % manga_id
        
        r = await session.post(api_url, json={"query": manga_query}, headers=headers)
        data = r.json()
        
        manga_data = data.get('data', {}).get('manga', {})
        if not manga_data:
            raise Exception("Failed to fetch Manga metadata. Invalid ID?")
            
        self.title = manga_data.get('name', 'AllManga')
        
        thumb = manga_data.get('thumbnail')
        if thumb:
            self.thumbnail = f"https://allanimenews.com/{thumb}" if not thumb.startswith('http') else thumb
            
        chapters_detail = manga_data.get('availableChaptersDetail', {})
        # Usually chapters are in 'sub' or 'raw'
        available_chapters = chapters_detail.get('sub', [])
        if not available_chapters:
            available_chapters = chapters_detail.get('raw', [])
            
        if specific_chapter:
            available_chapters = [specific_chapter]
            
        print(f"Found {len(available_chapters)} chapters for {self.title}")
        
        all_items = []
        
        # We will attempt the first chapter. If it throws NEED_CAPTCHA, we boot up Playwright
        try:
            if available_chapters:
                first_chap = available_chapters[-1] # Usually oldest chapter
                items = await self.extract_single(session, manga_id, first_chap)
                all_items.extend(items)
                available_chapters.remove(first_chap)
        except Exception as e:
            if "NEED_CAPTCHA" in str(e) or "TOBEPARSED" in str(e):
                print("Cloudflare Captcha detected! Booting visible Playwright browser for manual bypass...")
                
                async with async_playwright() as p:
                    # Headless=False so the user can click the Cloudflare checkbox
                    browser = await p.chromium.launch(headless=False)
                    context = await browser.new_context(
                        user_agent=headers["User-Agent"]
                    )
                    page = await context.new_page()
                    
                    print("Navigating to allmanga.to... PLEASE SOLVE THE CAPTCHA IN THE POPUP BROWSER.")
                    await page.goto(f"https://allmanga.to/manga/{manga_id}", wait_until="domcontentloaded", timeout=60000)
                    
                    # Wait for the user to solve captcha and Nuxt to load
                    try:
                        await page.wait_for_selector('div.spinner', state='hidden', timeout=45000)
                        await page.wait_for_timeout(3000)
                    except:
                        pass
                        
                    # Extract cookies
                    cookies = await context.cookies()
                    await browser.close()
                    
                    if not cookies:
                        raise Exception("Failed to extract cookies after Captcha bypass.")
                        
                    print(f"Successfully extracted {len(cookies)} cookies! Resuming extraction...")
                    
                    # Inject cookies into session
                    for cookie in cookies:
                        session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])
                        
                # Retry first chapter
                first_chap = available_chapters[-1]
                items = await self.extract_single(session, manga_id, first_chap)
                all_items.extend(items)
                available_chapters.remove(first_chap)
            else:
                raise e
                
        # Fetch the rest concurrently if we have more
        if available_chapters:
            import asyncio
            sem = asyncio.Semaphore(5)
            
            async def fetch_chapter(chap):
                async with sem:
                    return await self.extract_single(session, manga_id, chap)
                    
            tasks = [fetch_chapter(c) for c in available_chapters]
            results = await asyncio.gather(*tasks)
            
            for res in results:
                all_items.extend(res)
                
        return all_items
