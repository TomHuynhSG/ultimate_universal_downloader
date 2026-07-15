import html as html_lib
import re
from urllib.parse import urljoin, urlparse

from backend.core.config import get_settings
from backend.plugins.base import BaseExtractor
from backend.plugins.utils import bounded_map, deduplicate


class MangaHereExtractor(BaseExtractor):
    URLS = ["mangahere.cc", "mangahere.co"]

    @staticmethod
    def _unpack(payload, radix, count, keywords):
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

        def encode(number):
            if number == 0:
                return "0"
            result = ""
            while number:
                number, remainder = divmod(number, radix)
                result = alphabet[remainder] + result
            return result

        for index in range(count - 1, -1, -1):
            if index < len(keywords) and keywords[index]:
                payload = re.sub(r"\b" + encode(index) + r"\b", keywords[index], payload)
        return payload

    @staticmethod
    def _chapter_title(page_html, chapter_url):
        title_match = re.search(r'<p class="reader-header-title-2"\s*>(.*?)</p>', page_html)
        if title_match:
            return re.sub(r'[\\/*?:"<>|]', "", title_match.group(1)).strip() or "Chapter"
        match = re.search(r"/(c\d+(?:\.\d+)?)/", chapter_url)
        return match.group(1) if match else "Chapter"

    async def extract(self, session):
        timeout = get_settings()["request_timeout_seconds"]
        response = await session.get(self.url, timeout=timeout)
        if response.status_code != 200:
            raise RuntimeError(f"MangaHere returned HTTP {response.status_code}")
        page_html = response.text
        title_match = re.search(r'<span class="detail-info-right-title-font">(.*?)</span>', page_html)
        self.title = title_match.group(1).strip() if title_match else "MangaHere Download"
        image_tag = re.search(r'<img[^>]*class="detail-info-cover-img"[^>]*>', page_html)
        if image_tag and (source := re.search(r'src="([^"]+)"', image_tag.group(0))):
            value = html_lib.unescape(source.group(1))
            self.thumbnail = f"https:{value}" if value.startswith("//") else value

        if "/c" in self.url and "/1.html" in self.url:
            chapter_urls = [self.url]
        else:
            base_path = urlparse(self.url).path
            links = re.findall(r'<a href="(/manga/[^"]+/c\d+(?:\.\d+)?/1\.html)"', page_html)
            chapter_urls = [
                urljoin(self.url, link)
                for link in reversed(deduplicate(links))
                if link.startswith(base_path)
            ]
        if not chapter_urls:
            raise RuntimeError("No MangaHere chapters were found")

        async def fetch_chapter(chapter_url):
            chapter_response = await session.get(chapter_url, timeout=timeout)
            if chapter_response.status_code != 200:
                raise RuntimeError(f"Chapter returned HTTP {chapter_response.status_code}")
            chapter_html = chapter_response.text
            packer = re.search(
                r"eval\(function\(p,a,c,k,e,d\).*?\}\('(.*?)',\s*(\d+)\s*,\s*(\d+)\s*,\s*'(.*?)'\.split\('\|'\)",
                chapter_html,
            )
            if not packer:
                raise RuntimeError(f"Could not find packed image data in {chapter_url}")
            unpacked = self._unpack(
                packer.group(1),
                int(packer.group(2)),
                int(packer.group(3)),
                packer.group(4).split("|"),
            )
            images = re.search(r"newImgs=\[(.*?)\]", unpacked)
            if not images:
                raise RuntimeError(f"Could not find image list in {chapter_url}")
            folder = self._chapter_title(chapter_html, chapter_url)
            return [
                {"url": f"https:{value.rstrip('\\\\')}", "folder": folder, "referer": chapter_url}
                for value in re.findall(r"'(//.*?)'", images.group(1))
            ]

        async def progress(completed, total, errors):
            await self.report_progress(
                phase="extracting", completed=completed, total=total, errors=errors
            )

        results = await bounded_map(
            chapter_urls,
            fetch_chapter,
            limit=min(5, get_settings()["max_extract_concurrency"]),
            return_exceptions=True,
            on_progress=progress,
        )
        self.extraction_errors = [str(result) for result in results if isinstance(result, Exception)]
        media = [item for result in results if isinstance(result, list) for item in result]
        if not media:
            raise RuntimeError("Every MangaHere chapter failed extraction")
        self.urls = media
        return media
