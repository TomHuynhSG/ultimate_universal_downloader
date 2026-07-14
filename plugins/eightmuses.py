import json
import urllib.parse
import html
from bs4 import BeautifulSoup
from backend.plugins.base import BaseExtractor

class EightMusesExtractor(BaseExtractor):
    URLS = ['8muses.com', '8muses.io']
    
    def rot47(self, s):
        res = []
        for c in s:
            o = ord(c)
            if 33 <= o <= 126:
                res.append(chr(33 + ((o - 33 + 47) % 94)))
            else:
                res.append(c)
        return "".join(res)
        
    async def get_page_data(self, session, url):
        resp = await session.get(url)
        if resp.status_code != 200:
            return None
            
        soup = BeautifulSoup(resp.text, 'html.parser')
        for s in soup.find_all('script'):
            text = s.text.strip()
            if text and text.startswith('!L'):
                text = html.unescape(text)
                decoded = self.rot47(text)
                idx = decoded.find('{')
                if idx != -1:
                    try:
                        return json.loads(decoded[idx:])
                    except Exception:
                        pass
        return None

    async def extract_recursive(self, session, current_url, current_folder=""):
        media_items = []
        
        data = await self.get_page_data(session, current_url)
        if not data:
            return media_items
            
        if not getattr(self, "title", None) and 'album' in data:
            import re
            raw_title = data['album'].get('name', '8muses Comics')
            self.title = re.sub(r'[\\/*?:"<>|]', "", raw_title).strip()
            
        domain = urllib.parse.urlparse(current_url).netloc
        pages = data.get('pages', 1)
        
        def process_data(page_data):
            items = []
            for pic in page_data.get('pictures', []):
                uri = pic.get('publicUri')
                if not uri:
                    continue
                img_url = f"https://{domain}/image/fl/{uri}.jpg"
                filename = f"{pic.get('name', pic.get('id'))}.jpg"
                items.append({
                    "url": img_url,
                    "folder": current_folder,
                    "filename": filename
                })
            return items
            
        # Process page 1
        media_items.extend(process_data(data))
        
        # Collect sub-albums from page 1
        albums_to_visit = data.get('albums', [])
        
        # Process remaining pages
        for p in range(2, pages + 1):
            page_url = f"{current_url}?page={p}" if '?' not in current_url else f"{current_url}&page={p}"
            p_data = await self.get_page_data(session, page_url)
            if p_data:
                media_items.extend(process_data(p_data))
                albums_to_visit.extend(p_data.get('albums', []))
                
        # Recursively visit sub-albums
        for alb in albums_to_visit:
            permalink = alb.get('permalink')
            if not permalink:
                continue
            sub_url = urllib.parse.urljoin(current_url, f"/comics/album/{permalink}")
            sub_name = alb.get('name', str(alb.get('id', 'Unknown')))
            
            # Sanitize sub-folder name
            import re
            sub_name = re.sub(r'[\\/*?:"<>|]', "", sub_name).strip()
            sub_folder = f"{current_folder}/{sub_name}" if current_folder else sub_name
            
            media_items.extend(await self.extract_recursive(session, sub_url, sub_folder))
            
        return media_items

    async def extract(self, session):
        self.title = None
        # Remove any trailing slash to make sure query params append properly
        base_url = self.url.rstrip('/')
        return await self.extract_recursive(session, base_url)
