import asyncio
import json
import re
import struct
import time
import urllib.parse

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from backend.core.config import get_settings
from backend.core.paths import sanitize_component
from backend.plugins.base import BaseExtractor
from backend.plugins.utils import bounded_map


class HitomiExtractor(BaseExtractor):
    URLS = ["hitomi.la"]
    ASSET_ORIGIN = "https://ltn.gold-usergeneratedcontent.net"
    IMAGE_DOMAIN = "gold-usergeneratedcontent.net"
    _resolver = None
    _resolver_loaded_at = 0.0
    _resolver_lock = None

    @staticmethod
    def _gallery_id(url):
        match = re.search(r"-(\d+)\.html|/galleries/(\d+)\.html|/reader/(\d+)\.html", url)
        return next((group for group in match.groups() if group), None) if match else None

    @classmethod
    async def _load_resolver(cls, session, *, force=False):
        if cls._resolver_lock is None:
            cls._resolver_lock = asyncio.Lock()
        if cls._resolver and not force and time.monotonic() - cls._resolver_loaded_at < 600:
            return cls._resolver

        async with cls._resolver_lock:
            if cls._resolver and not force and time.monotonic() - cls._resolver_loaded_at < 600:
                return cls._resolver
            timeout = get_settings()["request_timeout_seconds"]
            response = await session.get(f"{cls.ASSET_ORIGIN}/gg.js", timeout=timeout)
            if response.status_code != 200:
                raise RuntimeError(f"Hitomi resolver returned HTTP {response.status_code}")

            mapping = {}
            block_pattern = r"((?:\s*case\s+\d+\s*:\s*)+)o\s*=\s*(\d+)\s*;\s*break\s*;"
            for cases, value in re.findall(block_pattern, response.text):
                for number in re.findall(r"case\s+(\d+)", cases):
                    mapping[int(number)] = int(value)
            base_match = re.search(r"b:\s*'([^']+)'", response.text)
            if not base_match or not mapping:
                raise RuntimeError("Hitomi resolver format was not recognized")

            cls._resolver = (mapping, base_match.group(1))
            cls._resolver_loaded_at = time.monotonic()
            return cls._resolver

    @classmethod
    def _url_from_hash(cls, image_hash, resolver):
        mapping, base_path = resolver
        if not re.fullmatch(r"[0-9a-f]{64}", image_hash or ""):
            raise ValueError("Invalid Hitomi image hash")
        rotated = int(image_hash[-1] + image_hash[-3:-1], 16)
        shard = 1 + mapping.get(rotated, 1)
        return (
            f"https://w{shard}.{cls.IMAGE_DOMAIN}/"
            f"{base_path}{rotated}/{image_hash}.webp"
        )

    async def _get(self, session, url, *, attempts=3):
        timeout = get_settings()["request_timeout_seconds"]
        last_error = None
        for attempt in range(attempts):
            try:
                response = await session.get(url, timeout=timeout)
                if response.status_code == 200:
                    return response
                last_error = RuntimeError(f"HTTP {response.status_code} for {url}")
            except Exception as exc:
                last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(0.5 * (2**attempt))
        raise last_error or RuntimeError(f"Failed to fetch {url}")

    async def _fetch_gallery(self, session, gallery_id, resolver, foldered):
        response = await self._get(
            session, f"{self.ASSET_ORIGIN}/galleries/{gallery_id}.js"
        )
        match = re.search(r"var\s+galleryinfo\s*=\s*(\{.*\})\s*;?\s*$", response.text, re.S)
        if not match:
            raise RuntimeError(f"Gallery {gallery_id} metadata was malformed")
        data = json.loads(match.group(1))
        if data.get("blocked"):
            raise RuntimeError(f"Gallery {gallery_id} is blocked")

        raw_title = data.get("title") or data.get("japanese_title") or f"Hitomi Gallery {gallery_id}"
        artists = [entry.get("artist") for entry in data.get("artists") or [] if entry.get("artist")]
        if not artists:
            artists = [entry.get("group") for entry in data.get("groups") or [] if entry.get("group")]
        artist_suffix = f" by {', '.join(name.title() for name in artists)}" if artists else ""
        gallery_title = sanitize_component(
            f"{raw_title}{artist_suffix} ({gallery_id})",
            fallback=f"Hitomi Gallery {gallery_id}",
        )

        items = []
        for file_data in data.get("files", []):
            image_hash = file_data.get("hash")
            name = file_data.get("name") or f"{len(items) + 1:03d}.webp"
            filename = re.sub(r"\.[^/.]+$", ".webp", name)
            item = {
                "url": self._url_from_hash(image_hash, resolver),
                "filename": filename,
                "referer": "https://hitomi.la/",
            }
            if foldered:
                item["folder"] = gallery_title
            items.append(item)
        if not items:
            raise RuntimeError(f"Gallery {gallery_id} contained no downloadable files")
        return gallery_title, items

    async def _collection_ids(self, session):
        parsed = urllib.parse.urlparse(self.url)
        path = parsed.path.lstrip("/")
        if not path.endswith(".html"):
            raise RuntimeError("Unsupported Hitomi collection URL")
        nozomi_path = f"{path[:-5]}.nozomi"
        response = await self._get(session, f"{self.ASSET_ORIGIN}/{nozomi_path}")
        if not response.content or len(response.content) % 4:
            raise RuntimeError("Hitomi collection index was malformed")
        gallery_ids = [str(item[0]) for item in struct.iter_unpack(">I", response.content)]
        return list(dict.fromkeys(gallery_ids))

    @staticmethod
    def _collection_slug_title(url):
        slug = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
        slug = re.sub(r"\.html$", "", slug, flags=re.I)
        slug = re.sub(
            r"-(?:all|english|japanese|chinese|korean|spanish|portuguese|"
            r"russian|german|french|italian|polish|thai|vietnamese|"
            r"indonesian|dutch|hungarian)$",
            "",
            slug,
            flags=re.I,
        )
        return sanitize_component(
            slug.replace("-", " ").replace("_", " ").title(),
            fallback="Hitomi Collection",
        )

    async def _collection_title(self, session):
        parsed = urllib.parse.urlparse(self.url)
        collection_type = parsed.path.strip("/").split("/", 1)[0].lower()
        slug_title = self._collection_slug_title(self.url)

        # Identity collection URLs already contain the most useful parent-folder name.
        # Avoid another HTML navigation and keep galleries grouped by artist/group/etc.
        if collection_type in {"artist", "group", "series", "character"}:
            return slug_title

        try:
            response = await self._get(session, self.url, attempts=2)
            soup = BeautifulSoup(response.text, "html.parser")
            if not soup.title:
                return slug_title
            raw = soup.title.get_text(" ", strip=True).replace("| Hitomi.la", "").strip()
            raw = re.sub(r"(?i)\s*\([^)]*\)$", "", raw).strip()
            if raw.casefold() in {"", "hitomi.la", "hitomi collection"}:
                return slug_title
            return sanitize_component(raw.title(), fallback=slug_title)
        except Exception:
            return slug_title

    async def _extract_direct(self, session):
        resolver = await self._load_resolver(session)
        single_id = self._gallery_id(self.url)
        if single_id:
            title, items = await self._fetch_gallery(session, single_id, resolver, False)
            self.title = title
            self.thumbnail = items[0]["url"]
            await self.report_progress(phase="extracting", completed=1, total=1, errors=0)
            return items

        gallery_ids = await self._collection_ids(session)
        if not gallery_ids:
            raise RuntimeError("Hitomi collection contained no galleries")
        self.title = await self._collection_title(session)

        concurrency = get_settings()["max_extract_concurrency"]

        async def fetch(index_and_id):
            index, gallery_id = index_and_id
            title, items = await self._fetch_gallery(session, gallery_id, resolver, True)
            return index, title, items

        async def progress(completed, total, errors):
            await self.report_progress(
                phase="extracting", completed=completed, total=total, errors=errors
            )

        raw_results = await bounded_map(
            list(enumerate(gallery_ids)),
            fetch,
            limit=concurrency,
            return_exceptions=True,
            on_progress=progress,
        )
        errors = [str(result) for result in raw_results if isinstance(result, Exception)]
        results = [result for result in raw_results if isinstance(result, tuple)]
        results.sort(key=lambda result: result[0])
        self.extraction_errors = errors
        items = [item for _index, _title, gallery_items in results for item in gallery_items]
        if not items:
            raise RuntimeError("Every gallery in the Hitomi collection failed extraction")
        self.thumbnail = items[0]["url"] if items else None
        return items

    async def _extract_with_playwright(self):
        """Compatibility fallback used only if Hitomi changes its direct endpoints."""
        gallery_id = self._gallery_id(self.url)
        if not gallery_id:
            raise RuntimeError("Playwright fallback supports individual galleries only")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "stylesheet", "font", "media"}
                else route.continue_(),
            )
            try:
                await page.goto(
                    f"https://hitomi.la/reader/{gallery_id}.html#1",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                await page.wait_for_function("typeof galleryinfo !== 'undefined'", timeout=15_000)
                payload = await page.evaluate(
                    r"""
                    galleryId => ({
                      title: galleryinfo.title || galleryinfo.japanese_title,
                      artists: (galleryinfo.artists || []).map(value => value.artist),
                      groups: (galleryinfo.groups || []).map(value => value.group),
                      items: galleryinfo.files.map(file => ({
                        url: url_from_url_from_hash(galleryId, file, 'webp'),
                        filename: file.name.replace(/\.[^/.]+$/, '.webp'),
                        referer: 'https://hitomi.la/'
                      }))
                    })
                    """,
                    gallery_id,
                )
            finally:
                await browser.close()
        artists = payload.get("artists") or payload.get("groups") or []
        suffix = f" by {', '.join(name.title() for name in artists)}" if artists else ""
        self.title = sanitize_component(
            f"{payload.get('title') or 'Hitomi Gallery'}{suffix} ({gallery_id})",
            fallback=f"Hitomi Gallery {gallery_id}",
        )
        self.thumbnail = payload["items"][0]["url"] if payload.get("items") else None
        return payload.get("items") or []

    async def extract(self, session):
        self.extraction_errors = []
        try:
            return await self._extract_direct(session)
        except Exception:
            if not get_settings().get("use_playwright", True):
                raise
            return await self._extract_with_playwright()
