import html
import json
import re
import urllib.parse

from bs4 import BeautifulSoup

from backend.core.config import get_settings
from backend.plugins.base import BaseExtractor
from backend.plugins.utils import bounded_map, deduplicate


class EightMusesExtractor(BaseExtractor):
    URLS = ["8muses.com", "8muses.io"]

    @staticmethod
    def rot47(value):
        return "".join(
            chr(33 + ((ord(character) - 33 + 47) % 94))
            if 33 <= ord(character) <= 126
            else character
            for character in value
        )

    async def get_page_data(self, session, url):
        response = await session.get(url, timeout=get_settings()["request_timeout_seconds"])
        if response.status_code != 200:
            raise RuntimeError(f"8Muses returned HTTP {response.status_code}: {url}")
        soup = BeautifulSoup(response.text, "html.parser")
        for script in soup.find_all("script"):
            text = script.get_text().strip()
            if text.startswith("!L"):
                decoded = self.rot47(html.unescape(text))
                start = decoded.find("{")
                if start >= 0:
                    try:
                        return json.loads(decoded[start:])
                    except json.JSONDecodeError:
                        continue
        raise RuntimeError(f"8Muses page data was not found: {url}")

    @staticmethod
    def page_url(url, page):
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        query["page"] = str(page)
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))

    async def extract_recursive(self, session, current_url, current_folder, visited):
        canonical = urllib.parse.urlunparse(urllib.parse.urlparse(current_url)._replace(query="", fragment=""))
        if canonical in visited:
            return []
        visited.add(canonical)

        data = await self.get_page_data(session, current_url)
        if self.title is None and data.get("album"):
            self.title = re.sub(
                r'[\\/*?:"<>|]', "", data["album"].get("name", "8Muses Comics")
            ).strip() or "8Muses Comics"
        domain = urllib.parse.urlparse(current_url).netloc

        def process(page_data):
            result = []
            for picture in page_data.get("pictures", []):
                uri = picture.get("publicUri")
                if uri:
                    result.append(
                        {
                            "url": f"https://{domain}/image/fl/{uri}.jpg",
                            "folder": current_folder,
                            "filename": f"{picture.get('name', picture.get('id'))}.jpg",
                        }
                    )
            return result

        pages = max(1, int(data.get("pages", 1)))
        remaining_page_numbers = range(2, pages + 1)
        page_results = await bounded_map(
            remaining_page_numbers,
            lambda page: self.get_page_data(session, self.page_url(current_url, page)),
            limit=min(6, get_settings()["max_extract_concurrency"]),
            return_exceptions=True,
        )
        page_data = [data] + [result for result in page_results if isinstance(result, dict)]
        self.extraction_errors.extend(
            str(result) for result in page_results if isinstance(result, Exception)
        )
        media = [item for result in page_data for item in process(result)]
        albums = deduplicate(
            [album for result in page_data for album in result.get("albums", [])],
            key=lambda album: album.get("id") or album.get("permalink"),
        )

        async def fetch_album(album):
            permalink = album.get("permalink")
            if not permalink:
                return []
            name = re.sub(
                r'[\\/*?:"<>|]', "", album.get("name", str(album.get("id", "Unknown")))
            ).strip() or "Unknown"
            folder = f"{current_folder} - {name}" if current_folder else name
            url = urllib.parse.urljoin(current_url, f"/comics/album/{permalink}")
            return await self.extract_recursive(session, url, folder, visited)

        album_results = await bounded_map(
            albums,
            fetch_album,
            limit=min(4, get_settings()["max_extract_concurrency"]),
            return_exceptions=True,
        )
        self.extraction_errors.extend(
            str(result) for result in album_results if isinstance(result, Exception)
        )
        media.extend(
            item for result in album_results if isinstance(result, list) for item in result
        )
        return media

    async def extract(self, session):
        self.title = None
        self.extraction_errors = []
        media = await self.extract_recursive(session, self.url.rstrip("/"), "", set())
        if not media:
            raise RuntimeError("No 8Muses media was found")
        return media
