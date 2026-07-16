import re

from bs4 import BeautifulSoup

from backend.core.config import get_settings
from backend.core.media import MediaProcessor
from backend.plugins.base import BaseExtractor
from backend.plugins.utils import bounded_map, deduplicate


class LusciousExtractor(BaseExtractor):
    URLS = ["luscious.net", "www.luscious.net", "members.luscious.net"]
    # Use MP4 as an efficient transport for GIF animations when FFmpeg is
    # available, but always finalize those items with a .gif filename.
    OTHER_MEDIA_EXTENSIONS = ("mp4", "jpg", "png")

    @staticmethod
    def thumbnails_from_html(page_html):
        soup = BeautifulSoup(page_html, "html.parser")
        return [
            source
            for image in soup.find_all("img")
            if (source := image.get("src", ""))
            and "ah-img.luscious.net" in source
            and "avatar" not in source.lower()
        ]

    async def extract(self, session):
        settings = get_settings()
        timeout = settings["request_timeout_seconds"]
        response = await session.get(self.url, timeout=timeout)
        if response.status_code != 200:
            raise RuntimeError(f"Luscious returned HTTP {response.status_code}")
        soup = BeautifulSoup(response.text, "html.parser")
        heading = soup.find("h1")
        title = heading.get_text(strip=True) if heading else "Unknown Album"
        if match := re.search(r"_(\d+)(?:/|$)", self.url):
            title = f"{title} ({match.group(1)})"
        self.title = title
        if image := soup.find("meta", {"property": "og:image"}):
            self.thumbnail = image.get("content")

        max_page = max(
            [1]
            + [
                int(link.get_text(strip=True))
                for link in soup.select(".o-pagination-item")
                if link.get_text(strip=True).isdigit()
            ]
        )

        async def fetch_page(page_number):
            separator = "&" if "?" in self.url else "?"
            page = await session.get(
                f"{self.url}{separator}page={page_number}", timeout=timeout
            )
            if page.status_code != 200:
                raise RuntimeError(f"Luscious page {page_number} returned HTTP {page.status_code}")
            return self.thumbnails_from_html(page.text)

        page_results = await bounded_map(
            range(2, max_page + 1),
            fetch_page,
            limit=6,
            runtime_limited=True,
            return_exceptions=True,
        )
        self.extraction_errors = [
            str(result) for result in page_results if isinstance(result, Exception)
        ]
        thumbnails = self.thumbnails_from_html(response.text)
        thumbnails.extend(
            value for result in page_results if isinstance(result, list) for value in result
        )
        thumbnails = deduplicate(thumbnails)
        if not thumbnails:
            raise RuntimeError("No Luscious media thumbnails were found")

        conversion_available = MediaProcessor.can_convert_to_gif()

        async def probe(index_and_url):
            index, thumbnail = index_and_url
            base = re.sub(r"\.\d+x\d+\.jpg$", "", thumbnail)
            gif_candidate = f"{base}.gif"
            try:
                gif_head = await session.head(gif_candidate, timeout=timeout)
            except Exception:
                gif_head = None

            if gif_head and gif_head.status_code == 200:
                if conversion_available:
                    mp4_candidate = f"{base}.mp4"
                    try:
                        mp4_head = await session.head(mp4_candidate, timeout=timeout)
                    except Exception:
                        mp4_head = None
                    if mp4_head and mp4_head.status_code == 200:
                        return {
                            "url": mp4_candidate,
                            "filename": f"{index + 1:03d}.gif",
                            "referer": self.url,
                            "convert_to": "gif",
                            "fallback_url": gif_candidate,
                        }
                return {
                    "url": gif_candidate,
                    "filename": f"{index + 1:03d}.gif",
                    "referer": self.url,
                }

            for extension in self.OTHER_MEDIA_EXTENSIONS:
                candidate = f"{base}.{extension}"
                try:
                    head = await session.head(candidate, timeout=timeout)
                    if head.status_code == 200:
                        return {
                            "url": candidate,
                            "filename": f"{index + 1:03d}.{extension}",
                            "referer": self.url,
                        }
                except Exception:
                    continue
            return {
                "url": thumbnail,
                "filename": f"{index + 1:03d}_thumb.jpg",
                "referer": self.url,
            }

        async def progress(completed, total, errors):
            await self.report_progress(
                phase="probing", completed=completed, total=total, errors=errors
            )

        results = await bounded_map(
            list(enumerate(thumbnails)),
            probe,
            limit=20,
            runtime_limited=True,
            return_exceptions=True,
            on_progress=progress,
        )
        self.extraction_errors.extend(
            str(result) for result in results if isinstance(result, Exception)
        )
        media = [result for result in results if isinstance(result, dict)]
        if not media:
            raise RuntimeError("Every Luscious CDN probe failed")
        return media
