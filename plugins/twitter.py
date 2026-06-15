import re
from backend.plugins.base import BaseExtractor

class TwitterExtractor(BaseExtractor):
    """
    Downloads videos from X / Twitter posts natively via api.vxtwitter.com
    Bypasses dynamic state tokens and aggressive rate limits.
    """
    
    URLS = ["x.com", "twitter.com", "api.vxtwitter.com"]

    async def extract(self, session):
        # Clean URL to get the tweet ID
        m = re.search(r'status/(\d+)', self.url)
        if not m:
            raise Exception("Invalid X/Twitter URL. Cannot find the status ID.")
        
        tweet_id = m.group(1)
        api_url = f"https://api.vxtwitter.com/i/status/{tweet_id}"
        
        print(f"Fetching X Post via vxtwitter API: {api_url}")
        
        resp = await session.get(api_url)
        if resp.status_code != 200:
            raise Exception(f"Failed to fetch X post! HTTP {resp.status_code}")
            
        data = resp.json()
        
        user = data.get("user_screen_name", "UnknownUser")
        text = data.get("text", "No Description")
        
        # Clean the text to be extremely safe for Windows file paths
        # Replace newlines with spaces
        safe_text = re.sub(r'[\r\n]+', ' ', text)
        # Strip all invalid Windows filename characters
        safe_text = re.sub(r'[\\/*?:"<>|]', "", safe_text)
        
        # Windows max path is 260 characters. We will conservatively limit the filename
        # itself to 150 characters to ensure the root path + title + filename doesn't crash.
        raw_title = f"{user} - {safe_text}"
        if len(raw_title) > 130:
            raw_title = raw_title[:127] + "..."
            
        # Append the unique tweet ID to prevent conflicts when posts have no description
        raw_title = f"{raw_title} - {tweet_id}"
        self.title = f"X.com - {user}"
        self.flat_directory = True
        
        # Look for thumbnail
        media_array = data.get("media_extended", [])
        if media_array:
            self.thumbnail = media_array[0].get("thumbnail_url")
            
        media_items = []
        video_count = 1
        
        for media in media_array:
            if media.get("type") in ["video", "gif"]:
                video_url = media.get("url")
                # Ensure we have an extension
                ext = ".mp4"
                if video_url.endswith(".mp4"):
                    ext = ".mp4"
                elif video_url.endswith(".gif"):
                    ext = ".gif"
                elif video_url.endswith(".webm"):
                    ext = ".webm"
                    
                # Add suffix if there are multiple videos in the same post
                suffix = f"_{video_count}" if len(media_array) > 1 else ""
                video_count += 1
                
                filename = f"{raw_title.strip()}{suffix}{ext}"
                
                media_items.append({
                    "url": video_url,
                    "referer": "https://twitter.com/",
                    "type": "video",
                    "filename": filename
                })
                
        # If no video was found, we will still raise an exception or let the pipeline mark it error
        if not media_items:
            raise Exception("No video or gif found in this X post.")
            
        return media_items
