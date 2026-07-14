import json
import re
import base64
from bs4 import BeautifulSoup
from backend.plugins.base import BaseExtractor

class HentaiReadExtractor(BaseExtractor):
    URLS = ['hentairead.com/hentai/']

    async def extract(self, session):
        read_url = self.url
        
        # If the user passed the main gallery page, we need to append the chapter reading path.
        # Typically HentaiRead's first page is at /english/p/1/
        if not re.search(r'/p/\d+/?', read_url):
            read_url = read_url.rstrip('/') + '/english/p/1/'
            
        resp = await session.get(read_url)
        if resp.status_code == 404 and 'english/p/1' in read_url:
            # Fallback: Maybe it's not "english". Fetch the main page and look for the chapter link.
            main_url = self.url.split('/english/p/')[0].rstrip('/') + '/'
            resp_main = await session.get(main_url)
            soup_main = BeautifulSoup(resp_main.text, 'html.parser')
            
            # Find the first chapter link
            chapter_link = soup_main.select_one('li.wp-manga-chapter a')
            if chapter_link:
                read_url = chapter_link.get('href')
                resp = await session.get(read_url)
            else:
                raise Exception("Could not find the reading page link from the main gallery page.")
        
        if resp.status_code != 200:
            raise Exception(f"Failed to fetch HentaiRead reading page: HTTP {resp.status_code}")
            
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 1. Extract Title, Artist, and ID from the page title
        title_str = soup.title.string if soup.title else ""
        # Example: Reading Toaru Jukujo... Page 1 of 34 by "Tsukino Jyogi" - #291814 - ...
        m_title = re.search(r'Reading (.*?) Page \d+ of \d+ by "(.*?)" - #(\d+)', title_str)
        if m_title:
            raw_title = f"{m_title.group(1)} by {m_title.group(2)} ({m_title.group(3)})"
        else:
            # Fallback parsing
            m_title2 = re.search(r'Reading (.*?) Page', title_str)
            raw_title = m_title2.group(1) if m_title2 else "Unknown HentaiRead Gallery"
            
        self.title = re.sub(r'[\\/*?:"<>|]', "", raw_title).strip()
        
        # 2. Extract base64 encoded JSON payload (window.m...)
        images_data = None
        for script in soup.find_all('script'):
            text = script.text
            if text and 'window.m' in text and 'eyJ' in text:
                m_b64 = re.search(r"window\.m[A-Za-z0-9_]+ = '(eyJ[A-Za-z0-9+/=]+)'", text)
                if m_b64:
                    b64 = m_b64.group(1)
                    # Fix padding if necessary
                    b64 += "=" * ((4 - len(b64) % 4) % 4)
                    decoded = base64.b64decode(b64).decode('utf-8')
                    try:
                        images_data = json.loads(decoded)['data']['chapter']['images']
                    except Exception:
                        pass
                    break
                    
        if not images_data:
            raise Exception("Failed to locate the base64 encoded image payload in HentaiRead.")
            
        # 3. Extract the CDN Base URL
        base_url = "https://henread.xyz" # default fallback
        m_extra = re.search(r'var chapterExtraData = (\{.*?\});', resp.text)
        if m_extra:
            try:
                extra = json.loads(m_extra.group(1))
                base_url = extra.get('baseUrl', base_url)
            except Exception:
                pass
                
        # 4. Construct Media URLs
        # Note: The backend engine automatically attaches 'Referer': self.url
        media_items = []
        for img in images_data:
            img_url = f"{base_url.rstrip('/')}/{img['src']}"
            media_items.append(img_url)
            
        return media_items
