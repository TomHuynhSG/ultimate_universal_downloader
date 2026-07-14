import re
import asyncio
from bs4 import BeautifulSoup
from backend.plugins.base import BaseExtractor

class EHentaiExtractor(BaseExtractor):
    URLS = ['e-hentai.org']

    async def extract(self, session):
        # Set nw=1 cookie to bypass adult warning wall
        session.cookies.set("nw", "1", domain=".e-hentai.org")
        
        resp = await session.get(self.url)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 1. Get Title
        title_el = soup.find("h1", id="gn")
        if title_el:
            self.title = title_el.text.strip()
        else:
            self.title = "E-Hentai Gallery"
            
        # Append gallery ID to the title
        m_id = re.search(r'/g/(\d+)/', self.url)
        if m_id:
            gallery_id = m_id.group(1)
            self.title = f"{self.title} ({gallery_id})"
            
        self.title = re.sub(r'[\\/*?:"<>|]', "", self.title).strip()
            
        # 2. Get Thumbnail
        thumb = soup.select_one("#gd1 div")
        self.thumbnail = None
        if thumb and "background:transparent url(" in thumb.get("style", ""):
            m = re.search(r'url\((.*?)\)', thumb.get("style"))
            if m:
                self.thumbnail = m.group(1)
                
        # 3. Get all pagination URLs
        page_links = soup.select("table.ptt td a")
        pages = set([self.url])
        for a in page_links:
            href = a.get("href")
            if href and "/g/" in href:
                pages.add(href)
                
        def get_p(u):
            m = re.search(r'\?p=(\d+)', u)
            return int(m.group(1)) if m else 0
            
        sorted_pages = sorted(list(pages), key=get_p)
        
        # 4. Extract all image page URLs and original filenames
        image_page_items = []
        for p_url in sorted_pages:
            if p_url != self.url:
                p_resp = await session.get(p_url)
                p_soup = BeautifulSoup(p_resp.text, 'html.parser')
            else:
                p_soup = soup
                
            links = p_soup.select("#gdt a")
            for a in links:
                img_page_url = a["href"]
                div = a.find("div")
                filename = None
                if div and div.has_attr("title"):
                    m = re.search(r'Page \d+: (.*)', div["title"])
                    if m:
                        filename = m.group(1).strip()
                image_page_items.append({
                    "page_url": img_page_url,
                    "filename": filename
                })
                
        # 5. Concurrently resolve real image URLs from image pages
        real_image_items = []
        sem = asyncio.Semaphore(3) # Max 3 concurrent HTML fetches to avoid E-Hentai IP ban
        
        async def fetch_real_img(item):
            async with sem:
                try:
                    img_resp = await session.get(item["page_url"])
                    img_soup = BeautifulSoup(img_resp.text, "html.parser")
                    real_img = img_soup.select_one("#img")
                    if real_img:
                        res = {
                            "url": real_img["src"]
                        }
                        if item["filename"]:
                            res["filename"] = item["filename"]
                        return res
                except Exception as e:
                    print(f"Failed to extract e-hentai image page: {e}")
                return None
                
        tasks = [fetch_real_img(item) for item in image_page_items]
        results = await asyncio.gather(*tasks)
        
        for res in results:
            if res:
                real_image_items.append(res)
                
        return real_image_items
