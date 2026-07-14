import json
import re
from bs4 import BeautifulSoup
from backend.plugins.base import BaseExtractor

class NHentaiExtractor(BaseExtractor):
    URLS = ['nhentai.net/g/']

    async def extract(self, session):
        # Fetch the gallery page
        resp = await session.get(self.url)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # nhentai uses SvelteKit and embeds the full gallery JSON in a script tag
        scripts = soup.find_all('script', type="application/json")
        
        media_items = []
        
        for script in scripts:
            if script.has_attr('data-url') and '/api/v2/galleries/' in script['data-url']:
                data = json.loads(script.string)
                body_str = data.get('body')
                if body_str:
                    gallery = json.loads(body_str)
                    media_id = gallery.get('media_id')
                    
                    # Try to get the English title, fallback to pretty or japanese
                    title_dict = gallery.get('title', {})
                    self.title = title_dict.get('english') or title_dict.get('pretty') or title_dict.get('japanese') or 'Unknown nhentai Gallery'
                    
                    # Extract artists
                    artists = [t.get('name') for t in gallery.get('tags', []) if t.get('type') == 'artist']
                    artist_str = f" by {', '.join(artists)}" if artists else ""
                    
                    # Construct full raw title
                    raw_title = f"{self.title}{artist_str} ({gallery.get('id')})"
                    
                    # Clean up the title to avoid invalid characters in folder names
                    self.title = re.sub(r'[\\/*?:"<>|]', "", raw_title).strip()
                    
                    # Extract thumbnail if available
                    thumb_path = gallery.get('thumbnail', {}).get('path')
                    if thumb_path:
                        self.thumbnail = f"https://t.nhentai.net/{thumb_path}"
                    else:
                        self.thumbnail = None
                    
                    # Extract page URLs
                    for page in gallery.get('pages', []):
                        path = page.get('path')
                        if path:
                            # Original high-res images are stored on i.nhentai.net
                            img_url = f"https://i.nhentai.net/{path}"
                            media_items.append(img_url)
                    
                    # Once we successfully parse the gallery script, break out
                    break
        
        if not media_items:
            raise Exception("Failed to find gallery data in the page HTML. Are you sure this is a valid nhentai gallery URL?")
            
        return media_items
