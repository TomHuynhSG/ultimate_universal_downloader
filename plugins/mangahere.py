import re
from urllib.parse import urljoin
from backend.plugins.base import BaseExtractor

class MangaHereExtractor(BaseExtractor):
    URLS = ['mangahere.cc', 'mangahere.co']
    
    def _unpack(self, p, a, c, k):
        def base36encode(number):
            alphabet = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
            if number == 0:
                return '0'
            base36 = ''
            sign = ''
            if number < 0:
                sign = '-'
                number = -number
            while number != 0:
                number, i = divmod(number, a)
                base36 = alphabet[i] + base36
            return sign + base36

        for i in range(c - 1, -1, -1):
            if k[i]:
                encoded = base36encode(i)
                p = re.sub(r'\b' + encoded + r'\b', k[i], p)
        return p

    async def extract(self, session):
        self.title = "MangaHere Download"
        
        # 1. Fetch the main URL
        response = await session.get(self.url)
        html = response.text
        
        # Extract title if possible
        title_match = re.search(r'<span class="detail-info-right-title-font">(.*?)</span>', html)
        if title_match:
            self.title = title_match.group(1).strip()
            
        # Extract thumbnail if possible
        import html as html_lib
        img_tag = re.search(r'<img[^>]*class="detail-info-cover-img"[^>]*>', html)
        if img_tag:
            src_match = re.search(r'src="([^"]+)"', img_tag.group(0))
            if src_match:
                url = src_match.group(1)
                url = html_lib.unescape(url)
                self.thumbnail = "https:" + url if url.startswith("//") else url
            
        chapter_urls = []
        
        # Check if it's a chapter page or main page
        if "/c" in self.url and "/1.html" in self.url:
            # It's a single chapter
            chapter_urls.append(self.url)
            # Try to get chapter title
            ch_match = re.search(r'<p class="reader-header-title-2"\s*>(.*?)</p>', html)
            if ch_match:
                self.title += f" - {ch_match.group(1).strip()}"
        else:
            # It's a main manga page, extract all chapters
            from urllib.parse import urlparse
            base_path = urlparse(self.url).path
            
            links = re.findall(r'<a href="(/manga/[^"]+/c\d+(?:\.\d+)?/1\.html)"', html)
            # Reverse links because they are listed newest first
            links = reversed(list(dict.fromkeys(links)))  # deduplicate and keep order
            for link in links:
                if link.startswith(base_path):
                    chapter_urls.append(urljoin(self.url, link))
                
        all_images = []
        
        for ch_url in chapter_urls:
            print(f"Fetching chapter: {ch_url}")
            ch_resp = await session.get(ch_url)
            ch_html = ch_resp.text
            
            # Extract the actual chapter name for the folder
            ch_folder = "Chapter"
            ch_title_match = re.search(r'<p class="reader-header-title-2"\s*>(.*?)</p>', ch_html)
            if ch_title_match:
                # Sanitize the chapter title for safe directory creation
                raw_title = ch_title_match.group(1).strip()
                ch_folder = re.sub(r'[\\/*?:"<>|]', "", raw_title).strip()
            else:
                # Fallback to URL identifier if title not found
                folder_match = re.search(r'/(c\d+(?:\.\d+)?)/', ch_url)
                if folder_match:
                    ch_folder = folder_match.group(1)
            
            # Find the eval packer script
            # Example: eval(function(p,a,c,k,e,d){...}('...',62,126,'...'.split('|'),0,{}))
            packer_match = re.search(r"eval\(function\(p,a,c,k,e,d\).*?\}\('(.*?)',\s*(\d+)\s*,\s*(\d+)\s*,\s*'(.*?)'\.split\('\|'\)", ch_html)
            
            if packer_match:
                p_payload = packer_match.group(1)
                a_radix = int(packer_match.group(2))
                c_count = int(packer_match.group(3))
                k_dict = packer_match.group(4).split('|')
                
                # Unpack the javascript
                unpacked = self._unpack(p_payload, a_radix, c_count, k_dict)
                
                # Find newImgs array
                # Expected output: var newImgs=['//zjcdn.mangahere.org/...','//...']
                imgs_match = re.search(r"newImgs=\[(.*?)\]", unpacked)
                if imgs_match:
                    imgs_str = imgs_match.group(1)
                    # Extract individual urls
                    urls = re.findall(r"'(//.*?)'", imgs_str)
                    for u in urls:
                        # Clean any trailing backslash
                        u = u.rstrip('\\')
                        full_url = "https:" + u
                        
                        all_images.append({
                            "url": full_url,
                            "folder": ch_folder
                        })
                else:
                    print("Could not find newImgs in unpacked script.")
            else:
                print(f"Could not find packer script in {ch_url}")
                
        self.urls = all_images
        print(f"Total images extracted: {len(self.urls)}")
        return self.urls
