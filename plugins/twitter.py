import re
from pathlib import Path
from urllib.parse import urlparse

from backend.core.paths import MAX_FILENAME_LENGTH, sanitize_component
from backend.plugins.base import BaseExtractor

VIDEO_EXTENSIONS = {".mp4", ".gif", ".webm"}

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
        text = data.get("text", "")

        # Collapse newlines to spaces, then let the shared sanitizer strip every
        # character Windows rejects in a path component.
        description = sanitize_component(
            re.sub(r'[\r\n]+', ' ', text),
            fallback="",
            max_length=MAX_FILENAME_LENGTH * 4,
        )

        videos = [
            media
            for media in data.get("media_extended", [])
            if media.get("type") in ("video", "gif")
        ]
        if not videos:
            raise Exception("No video or gif found in this X post.")

        self.title = f"X.com - {user}"
        self.flat_directory = True
        self.thumbnail = videos[0].get("thumbnail_url")

        media_items = []
        for position, media in enumerate(videos, start=1):
            video_url = media.get("url") or ""
            extension = Path(urlparse(video_url).path).suffix.lower()
            if extension not in VIDEO_EXTENSIONS:
                extension = ".mp4"

            # The tweet ID separates posts that have no description, and the
            # position separates videos inside one post. Both must survive the
            # engine's filename budget, so the description absorbs the
            # truncation instead of the tail: a name trimmed from the right
            # would leave every video of a post pointing at the same file.
            tail = f" - {tweet_id}"
            if len(videos) > 1:
                tail += f"_{position}"
            tail += extension

            head = f"{user} - {description}" if description else user
            budget = max(1, MAX_FILENAME_LENGTH - len(tail))
            if len(head) > budget:
                head = head[:budget]
            head = head.strip().rstrip(". ") or user

            media_items.append({
                "url": video_url,
                "referer": "https://twitter.com/",
                "type": "video",
                "filename": f"{head}{tail}",
            })

        return media_items
