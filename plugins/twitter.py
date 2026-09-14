import re
from pathlib import Path
from urllib.parse import urlparse

from backend.core.paths import MAX_FILENAME_LENGTH, sanitize_component
from backend.plugins.base import BaseExtractor

VIDEO_EXTENSIONS = {".mp4", ".gif", ".webm"}
VIDEO_TYPES = ("video", "gif")
BROADCAST_PATTERN = re.compile(
    r'(?:twitter\.com|x\.com)/i/broadcasts/([A-Za-z0-9]+)'
)


def _parse_fxtwitter(data):
    """Normalize an api.fxtwitter.com payload (FixTweet schema)."""
    if data.get("code") != 200:
        raise ValueError(f"code {data.get('code')}: {data.get('message')}")

    tweet = data.get("tweet") or {}
    author = tweet.get("author") or {}
    # A text-only post carries no "media" key at all, so this walk has to stay
    # None-safe at every level rather than assuming an empty container.
    media = (tweet.get("media") or {}).get("all") or []
    # Facets carry the expanded target of every t.co link, which is the only
    # place a broadcast URL survives once the body text is shortened.
    facets = (tweet.get("raw_text") or {}).get("facets") or []
    text = tweet.get("text") or ""
    return {
        "user": author.get("screen_name"),
        "text": text,
        "media": media,
        "links": [text, *(facet.get("replacement") or "" for facet in facets)],
    }


def _parse_vxtwitter(data):
    """Normalize an api.vxtwitter.com payload (flat schema)."""
    text = data.get("text") or ""
    return {
        "user": data.get("user_screen_name"),
        "text": text,
        "media": data.get("media_extended") or [],
        "links": [text],
    }


# Ordered fallbacks. Both mirrors are free services that periodically get
# rate-limited or blocked outright, so a single hard-coded host is a single
# point of failure: api.vxtwitter.com started answering with a Cloudflare 403
# for every request and took the whole extractor down with it.
PROVIDERS = (
    ("fxtwitter", "https://api.fxtwitter.com/i/status/{tweet_id}", _parse_fxtwitter),
    ("vxtwitter", "https://api.vxtwitter.com/i/status/{tweet_id}", _parse_vxtwitter),
)


class TwitterExtractor(BaseExtractor):
    """
    Downloads videos from X / Twitter posts natively via public mirror APIs.
    Bypasses dynamic state tokens and aggressive rate limits.
    """

    URLS = ["x.com", "twitter.com", "api.vxtwitter.com", "api.fxtwitter.com"]

    async def _fetch_post(self, session, tweet_id):
        """Return the first mirror's normalized payload, else raise with every failure."""
        failures = []
        for name, template, parse in PROVIDERS:
            api_url = template.format(tweet_id=tweet_id)
            print(f"Fetching X Post via {name} API: {api_url}")
            try:
                resp = await session.get(api_url)
                if resp.status_code != 200:
                    raise ValueError(f"HTTP {resp.status_code}")
                post = parse(resp.json())
            except Exception as exc:
                failures.append(f"{name}: {exc}")
                continue
            if not post["user"]:
                failures.append(f"{name}: response carried no author")
                continue
            return post

        raise Exception(
            "Failed to fetch X post from every mirror! " + "; ".join(failures)
        )

    async def extract(self, session):
        # Clean URL to get the tweet ID
        m = re.search(r'status/(\d+)', self.url)
        if not m:
            raise Exception("Invalid X/Twitter URL. Cannot find the status ID.")

        tweet_id = m.group(1)
        post = await self._fetch_post(session, tweet_id)
        user = post["user"]

        # Collapse newlines to spaces, then let the shared sanitizer strip every
        # character Windows rejects in a path component.
        description = sanitize_component(
            re.sub(r'[\r\n]+', ' ', post["text"]),
            fallback="",
            max_length=MAX_FILENAME_LENGTH * 4,
        )

        videos = [
            media
            for media in post["media"]
            if media.get("type") in VIDEO_TYPES
        ]

        if videos:
            sources = [((media.get("url") or ""), "video") for media in videos]
            self.thumbnail = videos[0].get("thumbnail_url")
        else:
            # A broadcast replay is never listed in a mirror's media array. It
            # lives behind the /i/broadcasts/ link in the post body, and only
            # yt-dlp can resolve its HLS playlist, so hand the engine the page
            # URL as an hls item and let the existing branch do the work.
            broadcast = next(
                (
                    match.group(1)
                    for match in map(BROADCAST_PATTERN.search, post["links"])
                    if match
                ),
                None,
            )
            if not broadcast:
                raise Exception(
                    "No video, gif, or broadcast found in this X post."
                )
            sources = [(f"https://x.com/i/broadcasts/{broadcast}", "hls")]

        self.title = f"X.com - {user}"
        self.flat_directory = True

        media_items = []
        for position, (video_url, item_type) in enumerate(sources, start=1):
            extension = Path(urlparse(video_url).path).suffix.lower()
            if extension not in VIDEO_EXTENSIONS:
                extension = ".mp4"

            # The tweet ID separates posts that have no description, and the
            # position separates videos inside one post. Both must survive the
            # engine's filename budget, so the description absorbs the
            # truncation instead of the tail: a name trimmed from the right
            # would leave every video of a post pointing at the same file.
            tail = f" - {tweet_id}"
            if len(sources) > 1:
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
                "type": item_type,
                "filename": f"{head}{tail}",
            })

        return media_items
