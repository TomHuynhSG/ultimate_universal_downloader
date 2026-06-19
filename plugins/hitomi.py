import re
import urllib.parse
from backend.plugins.base import BaseExtractor
from playwright.async_api import async_playwright

class HitomiExtractor(BaseExtractor):
    URLS = ['hitomi.la']

    async def extract_single(self, page, url, is_sub=False):
        match = re.search(r'-(\d+)\.html|/galleries/(\d+)\.html|/reader/(\d+)\.html', url)
        if not match: return []
        gallery_id = next(g for g in match.groups() if g)
        
        await page.goto(url, wait_until="domcontentloaded")
        title = await page.title()
        raw_title = title.replace('| Hitomi.la', '').strip()
        
        # Replace normal pipe with fullwidth pipe for Windows folder compatibility
        raw_title = raw_title.replace('|', '｜')
        
        try:
            artists = await page.eval_on_selector_all('h2 ul.comma-list li a', 'elements => elements.map(e => e.innerText)')
        except Exception:
            artists = []
            
        if artists:
            import re as regex
            # Hitomi appends " by artist_name" at the end of the title. Strip it case-insensitively.
            parts = regex.split(r'(?i)\s+by\s+', raw_title)
            if len(parts) > 1:
                # Rejoin all but the last part (which is the artist name)
                raw_title = " by ".join(parts[:-1])
                
            if len(raw_title) > 150:
                raw_title = raw_title[:150].strip() + "..."
                
            artist_str = ", ".join([a.title() for a in artists])
            if len(artist_str) > 50:
                artist_str = artist_str[:50].strip() + "..."
                
            gallery_title = f"{raw_title} by {artist_str} ({gallery_id})"
        else:
            if len(raw_title) > 150:
                raw_title = raw_title[:150].strip() + "..."
            gallery_title = f"{raw_title} ({gallery_id})"
            
        if not is_sub:
            self.title = gallery_title
            
        try:
            cover_el = await page.wait_for_selector(".cover img, .gallery-preview img", timeout=5000)
            if cover_el and not is_sub:
                self.thumbnail = await cover_el.get_attribute("src")
                if self.thumbnail and self.thumbnail.startswith("//"):
                    self.thumbnail = "https:" + self.thumbnail
        except Exception:
            pass
            
        reader_url = f"https://hitomi.la/reader/{gallery_id}.html#1"
        
        js_eval = f"""
        () => {{
            const items = [];
            for (let i = 0; i < galleryinfo.files.length; i++) {{
                const url = url_from_url_from_hash("{gallery_id}", galleryinfo.files[i], "webp");
                let originalName = galleryinfo.files[i].name;
                originalName = originalName.replace(/\\.[^/.]+$/, ".webp");
                items.push({{
                    "url": url,
                    "filename": originalName,
                    "referer": "https://hitomi.la/"
                }});
            }}
            return items;
        }}
        """
        
        image_urls = []
        max_retries = 5
        for attempt in range(max_retries):
            await page.goto(reader_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_function("typeof galleryinfo !== 'undefined'", timeout=15000)
                image_urls = await page.evaluate(js_eval)
                if image_urls:
                    break
            except Exception:
                pass
                
            if attempt < max_retries - 1:
                import asyncio
                await asyncio.sleep(5 * (attempt + 1))
                
        if not image_urls:
            raise Exception("Failed to evaluate hitomi JS: galleryinfo was missing. The site might be serving an HTTP 503 rate-limit page or Cloudflare challenge.")
            
        if is_sub:
            import re as regex
            folder = regex.sub(r'[\\/*?:"<>|]', "", gallery_title).strip()
            if len(folder) > 200:
                folder = folder[:200].strip()
            for img in image_urls:
                img['folder'] = folder
        return image_urls

    async def extract(self, session):
        is_collection = not re.search(r'-(?:\d+)\.html|/galleries/(?:\d+)\.html|/reader/(?:\d+)\.html', self.url)
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            if is_collection:
                self.title = "Hitomi Collection"
                all_items = []
                seen_galleries = set()
                
                parsed = urllib.parse.urlparse(self.url)
                base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                
                await page.goto(self.url, wait_until="domcontentloaded")
                title = await page.title()
                raw_title = title.replace('| Hitomi.la', '').strip()
                
                # Strip out language suffixes like " (English)" and title-case the artist name
                import re as regex
                raw_title = regex.sub(r'(?i)\s*\([^)]*\)$', '', raw_title).strip()
                self.title = raw_title.title()
                
                p_num = 1
                while True:
                    page_url = f"{base_url}?page={p_num}" if p_num > 1 else self.url
                    if p_num > 1:
                        await page.goto(page_url, wait_until="domcontentloaded")
                        
                    try:
                        await page.wait_for_selector('.gallery-content a.lillie, .manga h1 a', state='attached', timeout=10000)
                    except Exception:
                        pass
                        
                    links = await page.eval_on_selector_all('.gallery-content a.lillie', 'elements => elements.map(e => e.href)')
                    if not links:
                        links = await page.eval_on_selector_all('.manga h1 a', 'elements => elements.map(e => e.href)')
                        
                    new_links = []
                    for link in links:
                        match = re.search(r'-(\d+)\.html|/galleries/(\d+)\.html', link)
                        if match:
                            gid = next(g for g in match.groups() if g)
                            if gid not in seen_galleries:
                                seen_galleries.add(gid)
                                new_links.append(link)
                                
                    if not new_links:
                        break
                        
                    for link in new_links:
                        items = await self.extract_single(page, link, is_sub=True)
                        all_items.extend(items)
                        
                    p_num += 1
                    
                await browser.close()
                return all_items
            else:
                items = await self.extract_single(page, self.url)
                await browser.close()
                return items
