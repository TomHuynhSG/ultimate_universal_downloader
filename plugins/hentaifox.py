import json
import re
from bs4 import BeautifulSoup
from backend.plugins.base import BaseExtractor

class HentaiFoxExtractor(BaseExtractor):
    URLS = ['hentaifox.com/gallery/']

    async def extract(self, session):
        resp = await session.get(self.url)
        if resp.status_code != 200:
            raise Exception(f"Failed to fetch HentaiFox gallery: HTTP {resp.status_code}")
            
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 1. Extract Title
        h1 = soup.find('h1')
        title = h1.text.strip() if h1 else "Unknown HentaiFox Gallery"
        
        # Get Gallery ID from URL for deduplication
        m = re.search(r'gallery/(\d+)', self.url)
        gallery_id = m.group(1) if m else "unknown"
        
        # Extract Artists
        artists = []
        artist_links = soup.select('ul.artists li a')
        for a in artist_links:
            # The text includes the badge number (e.g. "sunahama nosame 8"), so we should isolate the artist name
            # by removing the span.t_badge text
            badge = a.find('span', class_='t_badge')
            if badge:
                badge.extract()
            artists.append(a.text.strip())
            
        artist_str = f" by {', '.join(artists)}" if artists else ""
        
        # Clean title
        raw_title = f"{title}{artist_str} ({gallery_id})"
        self.title = re.sub(r'[\\/*?:"<>|]', "", raw_title).strip()
        
        # 2. Extract Thumbnail
        cover = soup.select_one('.cover img')
        if cover:
            self.thumbnail = cover.get('src')
        else:
            self.thumbnail = None
            
        # 3. Extract Metadata (load_dir, load_id, load_pages)
        load_dir_input = soup.find('input', {'id': 'load_dir'})
        load_id_input = soup.find('input', {'id': 'load_id'})
        load_pages_input = soup.find('input', {'id': 'load_pages'})
        
        if not (load_dir_input and load_id_input and load_pages_input):
            raise Exception("Could not find gallery metadata inputs (load_dir, load_id, load_pages).")
            
        load_dir = load_dir_input.get('value')
        load_id = load_id_input.get('value')
        total_pages = int(load_pages_input.get('value', '0'))
        
        # 4. Extract Extension Mapping (g_th)
        # var g_th = $.parseJSON('{"1":"w,1201,1685",...}');
        g_th_match = re.search(r'var g_th = \$\.parseJSON\(\'({.*?})\'\);', resp.text)
        
        ext_map = {
            'j': 'jpg',
            'p': 'png',
            'g': 'gif',
            'w': 'webp'
        }
        
        page_extensions = {}
        if g_th_match:
            try:
                g_th_data = json.loads(g_th_match.group(1))
                for page_num, data in g_th_data.items():
                    # data is like "w,1201,1685"
                    char = data.split(',')[0]
                    page_extensions[int(page_num)] = ext_map.get(char, 'jpg')
            except Exception:
                pass
                
        # 5. Build URLs
        media_items = []
        
        # Extract the correct CDN subdomain from the cover image (e.g. i1, i2, i3)
        if self.thumbnail:
            base_img_url = self.thumbnail.rsplit('/', 1)[0]
        else:
            base_img_url = f"https://i3.hentaifox.com/{load_dir}/{load_id}"
            
        
        for p in range(1, total_pages + 1):
            ext = page_extensions.get(p, 'jpg') # Fallback to jpg
            img_url = f"{base_img_url}/{p}.{ext}"
            media_items.append(img_url)
            
        return media_items
