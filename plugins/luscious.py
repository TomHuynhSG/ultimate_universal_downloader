import asyncio
from bs4 import BeautifulSoup
import re
from backend.plugins.base import BaseExtractor

class LusciousExtractor(BaseExtractor):
    """
    Downloads static images and GIFs from luscious.net albums.
    Bypasses dynamic URL resolutions by probing CDN extensions concurrently.
    """
    
    URLS = ["luscious.net", "www.luscious.net", "members.luscious.net"]

    async def extract(self, session):
        # Initial page load
        resp = await session.get(self.url)
        if resp.status_code != 200:
            raise Exception(f"Failed to fetch Luscious album: HTTP {resp.status_code}. The album may have been deleted.")

        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Extract title
        h1 = soup.find('h1')
        title = h1.text.strip() if h1 else "Unknown Album"
        
        # Check if URL has an ID for better naming
        m = re.search(r'_(\d+)(?:/|$)', self.url)
        if m:
            title = f"{title} ({m.group(1)})"
        self.title = title
        
        # Find thumbnail to show in UI
        og_img = soup.find('meta', {'property': 'og:image'})
        if og_img:
            self.thumbnail = og_img.get('content')
            
        print(f"Extracting Luscious Album: {title}")
        
        # Find total number of pages
        max_page = 1
        pagination_links = soup.select('.o-pagination-item')
        for link in pagination_links:
            text = link.text.strip()
            if text.isdigit():
                max_page = max(max_page, int(text))
                
        print(f"Found {max_page} pages to parse...")
        
        all_thumbnails = []
        
        # Helper to parse a single page
        async def fetch_page(page_num):
            sep = '&' if '?' in self.url else '?'
            page_url = f"{self.url}{sep}page={page_num}"
            
            p_resp = await session.get(page_url)
            if p_resp.status_code == 200:
                p_soup = BeautifulSoup(p_resp.text, 'html.parser')
                images = p_soup.find_all("img")
                for img in images:
                    src = img.get("src", "")
                    if "ah-img.luscious.net" in src and "avatar" not in src.lower():
                        all_thumbnails.append(src)

        # 1. Parse page 1 (we already have it)
        images = soup.find_all("img")
        for img in images:
            src = img.get("src", "")
            if "ah-img.luscious.net" in src and "avatar" not in src.lower():
                all_thumbnails.append(src)
                
        # 2. Concurrently fetch the rest of the HTML pages
        if max_page > 1:
            page_tasks = [fetch_page(p) for p in range(2, max_page + 1)]
            await asyncio.gather(*page_tasks)
            
        # Deduplicate preserving order
        seen = set()
        unique_thumbnails = []
        for thumb in all_thumbnails:
            if thumb not in seen:
                seen.add(thumb)
                unique_thumbnails.append(thumb)
                
        print(f"Discovered {len(unique_thumbnails)} media items. Probing CDNs...")
        
        final_urls = []
        probe_semaphore = asyncio.Semaphore(30) # Prevent hammering the CDN
        
        async def probe_url(idx, thumb_url):
            async with probe_semaphore:
                # Strip thumbnail modifiers like .315x0.jpg or .640x0.jpg
                base_url = re.sub(r'\.\d+x\d+\.jpg$', '', thumb_url)
                
                # Check GIF first (since user specifically requested GIF support and it's a common case)
                gif_url = f"{base_url}.gif"
                head_resp = await session.head(gif_url)
                if head_resp.status_code == 200:
                    filename = f"{idx+1:03d}.gif"
                    final_urls.append({"url": gif_url, "filename": filename, "idx": idx})
                    return
                
                # Check MP4 if it's animated but GIF failed (Luscious sometimes uses MP4 for animations)
                mp4_url = f"{base_url}.mp4"
                head_resp = await session.head(mp4_url)
                if head_resp.status_code == 200:
                    filename = f"{idx+1:03d}.mp4"
                    final_urls.append({"url": mp4_url, "filename": filename, "idx": idx})
                    return
                
                # Check standard JPG
                jpg_url = f"{base_url}.jpg"
                head_resp = await session.head(jpg_url)
                if head_resp.status_code == 200:
                    filename = f"{idx+1:03d}.jpg"
                    final_urls.append({"url": jpg_url, "filename": filename, "idx": idx})
                    return
                    
                # Check PNG
                png_url = f"{base_url}.png"
                head_resp = await session.head(png_url)
                if head_resp.status_code == 200:
                    filename = f"{idx+1:03d}.png"
                    final_urls.append({"url": png_url, "filename": filename, "idx": idx})
                    return
                    
                # Fallback, just use original thumbnail
                final_urls.append({"url": thumb_url, "filename": f"{idx+1:03d}_thumb.jpg", "idx": idx})
                
        # Fire off all probes concurrently
        probe_tasks = [probe_url(i, thumb) for i, thumb in enumerate(unique_thumbnails)]
        await asyncio.gather(*probe_tasks)
        
        # Sort back to original index order
        final_urls.sort(key=lambda x: x["idx"])
        
        return final_urls
