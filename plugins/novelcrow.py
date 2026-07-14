import re
from bs4 import BeautifulSoup
from backend.plugins.base import BaseExtractor

class NovelCrowExtractor(BaseExtractor):
    """
    Plugin for novelcrow.com (Madara Theme)
    Functions nearly identically to allporncomic.
    """
    URLS = ['novelcrow.com', 'www.novelcrow.com']

    async def extract(self, session):
        resp = await session.get(self.url)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        media_items = []
        
        # Check if we are on a Chapter page (has images directly inside .page-break)
        images = soup.select(".page-break img")
        
        if images:
            # We are on a chapter/sub-album page!
            # Extract main title from breadcrumb
            breadcrumb_items = soup.select("ol.breadcrumb li")
            if breadcrumb_items:
                # The last breadcrumb is the Chapter Title
                self.title = breadcrumb_items[-1].text.strip()
            else:
                self.title = "Unknown Comic"
                
            # Get parent URL from breadcrumb to fetch thumbnail
            parent_a = None
            if len(breadcrumb_items) >= 2:
                parent_a = breadcrumb_items[-2].find('a')
            parent_url = parent_a['href'] if parent_a else None
            self.thumbnail = None
            if parent_url:
                try:
                    p_resp = await session.get(parent_url)
                    p_soup = BeautifulSoup(p_resp.text, 'html.parser')
                    thumb = p_soup.select_one(".summary_image img")
                    if thumb:
                        self.thumbnail = thumb.get('data-src') or thumb.get('src')
                except Exception:
                    pass
                
            for img in images:
                src = img.get('data-src') or img.get('src')
                if src:
                    media_items.append({
                        "url": src.strip(),
                        "referer": self.url
                    })
                    
            return media_items
            
        else:
            # We are on the Main Album page!
            title_el = soup.find("h1")
            self.title = title_el.text.strip() if title_el else "Unknown Comic"
            
            # Find thumbnail if possible
            thumb = soup.select_one(".summary_image img")
            if thumb:
                self.thumbnail = thumb.get('data-src') or thumb.get('src')
                
            # Find all chapter items
            chapter_items = soup.select("li.wp-manga-chapter")
            
            if not chapter_items:
                raise Exception("No chapters found on this page. Make sure you provided a valid series or chapter URL.")
                
            # Chapters are usually listed newest -> oldest. Reverse them to download oldest -> newest.
            chapter_items.reverse()
            
            valid_chapters = []
            for li in chapter_items:
                a_tag = li.find("a")
                if a_tag and a_tag.text.strip():
                    valid_chapters.append(a_tag)
            
            for chap in valid_chapters:
                chap_title = chap.text.strip()
                # Clean up folder name if needed
                chap_title = re.sub(r'[\\/*?:"<>|]', "", chap_title).strip()
                chap_url = chap['href']
                
                # Fetch chapter page
                c_resp = await session.get(chap_url)
                if c_resp.status_code != 200:
                    print(f"Failed to fetch chapter {chap_url}, skipping...")
                    continue
                    
                c_soup = BeautifulSoup(c_resp.text, 'html.parser')
                c_images = c_soup.select(".page-break img")
                
                for img in c_images:
                    src = img.get('data-src') or img.get('src')
                    if src:
                        src = src.strip()
                        
                        # If there is only ONE chapter in the main album, do NOT create a subfolder.
                        if len(valid_chapters) > 1:
                            media_items.append({
                                "url": src,
                                "referer": chap_url,
                                "folder": chap_title
                            })
                        else:
                            media_items.append({
                                "url": src,
                                "referer": chap_url
                            })
                            
        return media_items
