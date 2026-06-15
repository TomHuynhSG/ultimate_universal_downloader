import re
import asyncio
from bs4 import BeautifulSoup
from backend.plugins.base import BaseExtractor

class WeebCentralExtractor(BaseExtractor):
    """
    Plugin for weebcentral.com
    Downloads all chapters of a manga series or a single chapter.
    """
    
    URLS = ["weebcentral.com", "www.weebcentral.com"]

    async def extract(self, session):
        # We need to determine if it's a series URL or a chapter URL.
        # e.g., https://weebcentral.com/series/01JR0CM9B3DD3FP3DMT0MT2AQA/name
        # e.g., https://weebcentral.com/chapters/01KTDDZGRV7DTSTD56JHHEMTTD
        
        is_series = "/series/" in self.url
        is_chapter = "/chapters/" in self.url
        
        if not (is_series or is_chapter):
            raise Exception("URL must be a WeebCentral series or chapter link.")
            
        if is_series:
            # 1. Fetch the series main page to get the title
            resp = await session.get(self.url)
            if resp.status_code != 200:
                raise Exception(f"Failed to fetch series page! HTTP {resp.status_code}")
                
            soup = BeautifulSoup(resp.text, 'html.parser')
            h1 = soup.find('h1')
            self.title = h1.text.strip() if h1 else "Unknown Series"
            
            # 2. Get full chapter list
            # The series ID is the part right after /series/
            m = re.search(r'/series/([A-Z0-9]+)', self.url)
            if not m:
                raise Exception("Could not find series ID in URL.")
            series_id = m.group(1)
            
            full_list_url = f"https://weebcentral.com/series/{series_id}/full-chapter-list"
            print(f"Fetching full chapter list: {full_list_url}")
            
            cl_resp = await session.get(full_list_url)
            if cl_resp.status_code != 200:
                raise Exception(f"Failed to fetch chapter list! HTTP {cl_resp.status_code}")
                
            cl_soup = BeautifulSoup(cl_resp.text, 'html.parser')
            
            chapter_links = []
            for a in cl_soup.find_all('a', href=True):
                href = a['href']
                if '/chapters/' in href:
                    if href not in chapter_links:
                        chapter_links.append(href)
                        
            if not chapter_links:
                raise Exception("No chapters found for this series.")
                
            print(f"Found {len(chapter_links)} chapters.")
            
            # 3. For each chapter, fetch images concurrently
            all_media = []
            
            async def fetch_chapter_images(chap_url, chapter_index):
                # Weebcentral loads images via an API:
                # {chapter_url}/images?is_prev=False&current_page=1&reading_style=long_strip
                img_api_url = f"{chap_url}/images?is_prev=False&current_page=1&reading_style=long_strip"
                c_resp = await session.get(img_api_url)
                if c_resp.status_code == 200:
                    c_soup = BeautifulSoup(c_resp.text, 'html.parser')
                    imgs = c_soup.find_all('img')
                    
                    media = []
                    page_num = 1
                    for img in imgs:
                        src = img.get('src')
                        # Filter out blank images, trackers, or UI elements
                        if src and src.startswith('http'):
                            media.append({
                                "url": src,
                                "referer": chap_url,
                                # We can optionally specify a filename format, e.g. "Chapter 41/001.png"
                                "filename": f"Chapter {len(chapter_links) - chapter_index:03d}/{page_num:03d}.png"
                            })
                            page_num += 1
                    return media
                else:
                    print(f"Error fetching {chap_url}: HTTP {c_resp.status_code}")
                    return []

            # We'll fetch them sequentially to prevent HTTP 429 (Too Many Requests)
            # WeebCentral strictly rate limits their image API if requested concurrently.
            for i, chap_url in enumerate(chapter_links):
                media = await fetch_chapter_images(chap_url, i)
                all_media.extend(media)
                # Small delay to respect rate limits
                await asyncio.sleep(0.5)
                
            return all_media

        elif is_chapter:
            # Single chapter
            self.title = "WeebCentral Chapter"
            
            img_api_url = f"{self.url}/images?is_prev=False&current_page=1&reading_style=long_strip"
            c_resp = await session.get(img_api_url)
            if c_resp.status_code != 200:
                raise Exception(f"Failed to fetch chapter images! HTTP {c_resp.status_code}")
                
            c_soup = BeautifulSoup(c_resp.text, 'html.parser')
            imgs = c_soup.find_all('img')
            
            media = []
            for img in imgs:
                src = img.get('src')
                if src and src.startswith('http'):
                    media.append({
                        "url": src,
                        "referer": self.url
                    })
            return media
