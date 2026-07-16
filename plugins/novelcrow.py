import re

from bs4 import BeautifulSoup

from backend.core.config import get_settings
from backend.plugins.base import BaseExtractor
from backend.plugins.utils import bounded_map


class NovelCrowExtractor(BaseExtractor):
    URLS = ["novelcrow.com", "www.novelcrow.com"]

    async def extract(self, session):
        timeout = get_settings()["request_timeout_seconds"]
        response = await session.get(self.url, timeout=timeout)
        if response.status_code != 200:
            raise RuntimeError(f"Failed to fetch page: HTTP {response.status_code}")
        soup = BeautifulSoup(response.text, "html.parser")
        images = soup.select(".page-break img")
        if images:
            breadcrumbs = soup.select("ol.breadcrumb li")
            self.title = breadcrumbs[-1].get_text(strip=True) if breadcrumbs else "Unknown Comic"
            parent_link = breadcrumbs[-2].find("a") if len(breadcrumbs) >= 2 else None
            if parent_link and parent_link.get("href"):
                try:
                    parent = await session.get(parent_link["href"], timeout=timeout)
                    thumbnail = BeautifulSoup(parent.text, "html.parser").select_one(".summary_image img")
                    if thumbnail:
                        self.thumbnail = thumbnail.get("data-src") or thumbnail.get("src")
                except Exception:
                    pass
            return [
                {"url": source.strip(), "referer": self.url}
                for image in images
                if (source := image.get("data-src") or image.get("src"))
            ]

        title = soup.find("h1")
        self.title = title.get_text(strip=True) if title else "Unknown Comic"
        thumbnail = soup.select_one(".summary_image img")
        if thumbnail:
            self.thumbnail = thumbnail.get("data-src") or thumbnail.get("src")
        chapters = []
        for item in reversed(soup.select("li.wp-manga-chapter")):
            link = item.find("a")
            if link and link.get("href") and link.get_text(strip=True):
                chapters.append((link.get_text(strip=True), link["href"]))
        if not chapters:
            raise RuntimeError("No chapters found")

        async def fetch_chapter(chapter):
            chapter_title, chapter_url = chapter
            response = await session.get(chapter_url, timeout=timeout)
            if response.status_code != 200:
                raise RuntimeError(f"Chapter returned HTTP {response.status_code}: {chapter_url}")
            chapter_soup = BeautifulSoup(response.text, "html.parser")
            folder = re.sub(r'[\\/*?:"<>|]', "", chapter_title).strip() or "Chapter"
            result = []
            for image in chapter_soup.select(".page-break img"):
                source = image.get("data-src") or image.get("src")
                if source:
                    item = {"url": source.strip(), "referer": chapter_url}
                    if len(chapters) > 1:
                        item["folder"] = folder
                    result.append(item)
            return result

        async def progress(completed, total, errors):
            await self.report_progress(
                phase="extracting", completed=completed, total=total, errors=errors
            )

        results = await bounded_map(
            chapters,
            fetch_chapter,
            limit=6,
            runtime_limited=True,
            return_exceptions=True,
            on_progress=progress,
        )
        self.extraction_errors = [str(result) for result in results if isinstance(result, Exception)]
        media = [item for result in results if isinstance(result, list) for item in result]
        if not media:
            raise RuntimeError("Every chapter failed extraction")
        return media
