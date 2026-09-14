import asyncio
import shutil
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from backend.api.router import (
    OpenDownloadDirectoryRequest,
    _remove_tree_with_retries,
    open_download_directory,
)
from backend.core.config import DEFAULT_SETTINGS
from backend.core.downloader import DownloadManager, _assign_unique_filenames
from backend.core.limits import ResizableLimiter
from backend.core.media import MediaProcessor
from backend.core.paths import (
    MAX_FILENAME_LENGTH,
    resolve_within,
    sanitize_component,
    unique_filename,
)
from backend.plugins.manager import PluginManager
from backend.plugins.utils import bounded_map, resize_runtime_extraction_limit
from plugins.hitomi import HitomiExtractor
from plugins.luscious import LusciousExtractor
from plugins.twitter import TwitterExtractor


class DummyLogger:
    def __init__(self):
        self.messages = []

    def log(self, message):
        self.messages.append(message)


class FakeResponse:
    status_code = 200
    headers = {"content-type": "image/jpeg", "content-length": "6"}

    async def aiter_content(self, chunk_size=None):
        yield b"abc"
        yield b"def"


class FakeSession:
    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        yield FakeResponse()


class UrlRecordingSession:
    def __init__(self):
        self.urls = []

    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        self.urls.append(args[1])
        yield FakeResponse()


class StallingResponse:
    def __init__(self, status_code, headers, chunks, error=None):
        self.status_code = status_code
        self.headers = headers
        self.chunks = chunks
        self.error = error

    async def aiter_content(self, chunk_size=None):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error


class ResumingSession:
    def __init__(self):
        self.requests = []
        self.responses = iter(
            [
                StallingResponse(
                    200,
                    {"content-type": "image/jpeg", "content-length": "6"},
                    [b"abc"],
                    RuntimeError("retry:0:connection stalled"),
                ),
                StallingResponse(
                    206,
                    {
                        "content-type": "image/jpeg",
                        "content-length": "3",
                        "content-range": "bytes 3-5/6",
                    },
                    [b"def"],
                ),
            ]
        )

    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        self.requests.append(kwargs)
        yield next(self.responses)


class LusciousSession:
    def __init__(self):
        self.head_urls = []

    async def get(self, *args, **kwargs):
        class Response:
            status_code = 200
            text = (
                '<html><h1>Fast album</h1><img '
                'src="https://ah-img.luscious.net/a/1/item.315x0.jpg"></html>'
            )

        return Response()

    async def head(self, url, **kwargs):
        self.head_urls.append(url)

        class Response:
            status_code = 200

        return Response()


class TwitterSession:
    """Serves one canned response per mirror host and records the call order."""

    def __init__(self, fx=None, vx=None):
        self.responses = {"fxtwitter": fx, "vxtwitter": vx}
        self.urls = []

    async def get(self, url, **kwargs):
        self.urls.append(url)
        host = "fxtwitter" if "fxtwitter" in url else "vxtwitter"
        status, payload = self.responses.get(host) or (403, None)

        class Response:
            status_code = status

            def json(self):
                if payload is None:
                    raise ValueError("mirror returned a challenge page, not JSON")
                return payload

        return Response()


class PathTests(unittest.TestCase):
    def test_default_download_directory_is_absolute(self):
        self.assertTrue(Path(DEFAULT_SETTINGS["download_dir"]).is_absolute())

    def test_sanitize_rejects_dot_components(self):
        self.assertEqual(sanitize_component("..", fallback="safe"), "safe")

    def test_resolve_within_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                resolve_within(directory, "..", "outside")

    def test_unique_filename_keeps_counter_inside_budget(self):
        long_name = f"{'a' * MAX_FILENAME_LENGTH}.mp4"
        first = unique_filename(long_name, set())
        second = unique_filename(first, {first.lower()})

        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith("_2.mp4"))
        self.assertLessEqual(len(second), MAX_FILENAME_LENGTH)

    def test_unique_filename_comparison_is_case_insensitive(self):
        self.assertEqual(unique_filename("Clip.mp4", {"clip.mp4"}), "Clip_2.mp4")


class UniqueTargetTests(unittest.TestCase):
    def test_truncated_names_are_separated_per_folder(self):
        stem = "b" * MAX_FILENAME_LENGTH
        items = [
            {"url": "https://cdn.example.com/one.mp4", "filename": f"{stem}_1.mp4"},
            {"url": "https://cdn.example.com/two.mp4", "filename": f"{stem}_2.mp4"},
            {
                "url": "https://cdn.example.com/three.mp4",
                "filename": f"{stem}_3.mp4",
                "folder": "Chapter 2",
            },
        ]

        _assign_unique_filenames(items)
        names = [item["filename"] for item in items]

        # The first two share a folder and truncate to the same head, so the
        # second must be renamed rather than overwrite the first.
        self.assertEqual(len(set(names[:2])), 2)
        # A different folder is a different destination, so it keeps the name.
        self.assertEqual(names[0], names[2])
        for name in names:
            self.assertLessEqual(len(name), MAX_FILENAME_LENGTH)

    def test_colliding_plain_urls_become_distinct_items(self):
        items = ["https://a.example.com/clip.mp4", "https://b.example.com/clip.mp4"]

        _assign_unique_filenames(items)

        self.assertEqual(
            items,
            [
                {"url": "https://a.example.com/clip.mp4", "filename": "clip.mp4"},
                {"url": "https://b.example.com/clip.mp4", "filename": "clip_2.mp4"},
            ],
        )

    def test_distinct_names_keep_their_own_destination(self):
        items = [
            {"url": "https://cdn.example.com/a.jpg"},
            {"url": "https://cdn.example.com/b.jpg"},
        ]

        _assign_unique_filenames(items)

        self.assertEqual(items[0]["filename"], "a.jpg")
        self.assertEqual(items[1]["filename"], "b.jpg")

    def test_hls_items_are_compared_by_their_converted_extension(self):
        items = [
            {
                "url": "https://cdn.example.com/stream.m3u8",
                "type": "hls",
                "filename": "clip.m3u8",
            },
            {"url": "https://cdn.example.com/clip.mp4"},
        ]

        _assign_unique_filenames(items)

        self.assertEqual(items[0]["filename"], "clip.mp4")
        self.assertEqual(items[1]["filename"], "clip_2.mp4")


class SettingsTests(unittest.TestCase):
    @patch("backend.api.router._open_directory")
    def test_open_download_directory_creates_and_opens_draft_path(self, open_directory):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "new-downloads"
            request = OpenDownloadDirectoryRequest(directory=str(target))
            result = open_download_directory(request)

            self.assertTrue(target.is_dir())
            open_directory.assert_called_once_with(target.resolve())
            self.assertEqual(result["directory"], str(target.resolve()))

    def test_remove_tree_retries_when_worker_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "task"
            target.mkdir()
            partial = target / "image.webp.part"
            partial.write_bytes(b"partial")
            real_rmtree = shutil.rmtree
            calls = 0

            def racing_rmtree(path):
                nonlocal calls
                calls += 1
                if calls == 1:
                    partial.unlink()
                    raise FileNotFoundError(partial)
                real_rmtree(path)

            with patch("backend.api.router.shutil.rmtree", side_effect=racing_rmtree):
                _remove_tree_with_retries(target, initial_delay=0)

            self.assertFalse(target.exists())
            self.assertEqual(calls, 2)


class PluginManagerTests(unittest.TestCase):
    def test_domain_matching_is_hostname_aware(self):
        self.assertTrue(PluginManager._matches("https://hitomi.la/galleries/1.html", "hitomi.la"))
        self.assertFalse(PluginManager._matches("https://evil.example/?next=hitomi.la", "hitomi.la"))
        self.assertFalse(PluginManager._matches("file:///tmp/hitomi.la", "hitomi.la"))


class HitomiTests(unittest.IsolatedAsyncioTestCase):
    def test_resolver_uses_default_and_listed_shards(self):
        resolver = HitomiExtractor._parse_resolver(
            "var o = 0; switch (g) { case 3243: o = 1; break; } b: '123/'"
        )
        ordinary_hash = "0" * 64
        listed_hash = "0" * 61 + "abc"

        self.assertIn("https://w1.", HitomiExtractor._url_from_hash(ordinary_hash, resolver))
        self.assertIn("https://w2.", HitomiExtractor._url_from_hash(listed_hash, resolver))

    async def test_artist_collection_uses_artist_as_parent_folder(self):
        extractor = HitomiExtractor("https://hitomi.la/artist/liyoosa-english.html")
        self.assertEqual(await extractor._collection_title(None), "Liyoosa")


class LusciousTests(unittest.IsolatedAsyncioTestCase):
    @patch("plugins.luscious.MediaProcessor.can_convert_to_gif", return_value=True)
    async def test_uses_smaller_mp4_transport_but_outputs_gif(self, _converter):
        session = LusciousSession()
        extractor = LusciousExtractor("https://www.luscious.net/albums/fast_1/")

        media = await extractor.extract(session)

        gif_url = "https://ah-img.luscious.net/a/1/item.gif"
        mp4_url = "https://ah-img.luscious.net/a/1/item.mp4"
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["url"], mp4_url)
        self.assertEqual(media[0]["filename"], "001.gif")
        self.assertEqual(media[0]["convert_to"], "gif")
        self.assertEqual(media[0]["fallback_url"], gif_url)
        self.assertEqual(session.head_urls, [gif_url, mp4_url])


class TwitterTests(unittest.IsolatedAsyncioTestCase):
    LONG_TEXT = (
        "Collab with a student of HCMUE ... Vibe hiền lành dễ "
        "thương mà cái vibe ấy kéo dài "
        "thêm rất nhiều chữ nữa cho đủ dài"
    )

    def _media(self, video_count):
        return [
            {
                "type": "video",
                "url": f"https://video.twimg.com/amplify_video/{index}/vid.mp4",
                "thumbnail_url": f"https://pbs.twimg.com/{index}.jpg",
            }
            for index in range(video_count)
        ]

    def _payload(self, video_count, text="", facet_links=()):
        """The nested FixTweet schema served by the primary mirror."""
        tweet = {
            "author": {"screen_name": "tranvietanh0810"},
            "text": text,
        }
        if video_count:
            tweet["media"] = {"all": self._media(video_count)}
        if facet_links:
            tweet["raw_text"] = {
                "facets": [
                    {"type": "url", "replacement": link} for link in facet_links
                ]
            }
        return {"code": 200, "message": "OK", "tweet": tweet}

    def _vx_payload(self, video_count, text=""):
        """The flat schema served by the fallback mirror."""
        return {
            "user_screen_name": "tranvietanh0810",
            "text": text,
            "media_extended": self._media(video_count),
        }

    async def test_every_video_of_a_post_keeps_its_own_file(self):
        session = TwitterSession(fx=(200, self._payload(2, self.LONG_TEXT)))
        extractor = TwitterExtractor(
            "https://x.com/tranvietanh0810/status/2070393711335489613"
        )

        media = await extractor.extract(session)
        names = [item["filename"] for item in media]

        self.assertEqual(len(media), 2)
        for name in names:
            self.assertLessEqual(len(name), MAX_FILENAME_LENGTH)
        self.assertTrue(names[0].endswith(" - 2070393711335489613_1.mp4"))
        self.assertTrue(names[1].endswith(" - 2070393711335489613_2.mp4"))

        # The engine must not have to rename anything: the plugin already fits
        # the filename budget without losing the per-video discriminator.
        _assign_unique_filenames(media)
        self.assertEqual([item["filename"] for item in media], names)

    async def test_single_video_has_no_positional_suffix(self):
        session = TwitterSession(fx=(200, self._payload(1, "short caption")))
        extractor = TwitterExtractor(
            "https://x.com/tranvietanh0810/status/2070393711335489613"
        )

        media = await extractor.extract(session)

        self.assertEqual(
            media[0]["filename"],
            "tranvietanh0810 - short caption - 2070393711335489613.mp4",
        )
        self.assertEqual(extractor.title, "X.com - tranvietanh0810")

    async def test_post_without_description_still_names_the_file(self):
        session = TwitterSession(fx=(200, self._payload(1)))
        extractor = TwitterExtractor(
            "https://x.com/tranvietanh0810/status/2070393711335489613"
        )

        media = await extractor.extract(session)

        self.assertEqual(
            media[0]["filename"],
            "tranvietanh0810 - 2070393711335489613.mp4",
        )

    async def test_post_without_video_reports_a_clear_failure(self):
        payload = self._payload(0)
        payload["tweet"]["media"] = {"all": [{"type": "image", "url": "https://x/y.jpg"}]}
        extractor = TwitterExtractor(
            "https://x.com/tranvietanh0810/status/2070393711335489613"
        )

        with self.assertRaisesRegex(Exception, "No video, gif, or broadcast"):
            await extractor.extract(TwitterSession(fx=(200, payload)))

    async def test_text_only_post_is_not_mistaken_for_a_mirror_outage(self):
        # A post that merely links a broadcast carries no "media" key at all,
        # which must read as an empty post rather than an unparseable payload.
        extractor = TwitterExtractor(
            "https://x.com/tranvietanh0810/status/2070393711335489613"
        )
        session = TwitterSession(fx=(200, self._payload(0)))

        with self.assertRaisesRegex(Exception, "No video, gif, or broadcast"):
            await extractor.extract(session)

        self.assertEqual(len(session.urls), 1)

    async def test_broadcast_link_is_downloaded_as_an_hls_item(self):
        # A broadcast replay never appears in the media array; the only trace
        # of it is the expanded /i/broadcasts/ link in the post body.
        payload = self._payload(
            0,
            "Kèo SOme sớm CN",
            facet_links=["https://x.com/i/broadcasts/1qxvveqnVMzxB"],
        )
        extractor = TwitterExtractor(
            "https://x.com/tranvietanh0810/status/2070393711335489613"
        )

        media = await extractor.extract(TwitterSession(fx=(200, payload)))

        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["type"], "hls")
        self.assertEqual(
            media[0]["url"], "https://x.com/i/broadcasts/1qxvveqnVMzxB"
        )
        # yt-dlp writes an mp4, and the name must carry no positional suffix.
        self.assertTrue(media[0]["filename"].endswith(" - 2070393711335489613.mp4"))

    async def test_attached_video_wins_over_a_broadcast_link(self):
        payload = self._payload(
            1,
            "short caption",
            facet_links=["https://x.com/i/broadcasts/1qxvveqnVMzxB"],
        )
        extractor = TwitterExtractor(
            "https://x.com/tranvietanh0810/status/2070393711335489613"
        )

        media = await extractor.extract(TwitterSession(fx=(200, payload)))

        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["type"], "video")
        self.assertIn("video.twimg.com", media[0]["url"])

    async def test_broadcast_link_in_flat_mirror_text_is_found(self):
        payload = self._vx_payload(0, "watch https://x.com/i/broadcasts/1qxvveqnVMzxB")
        extractor = TwitterExtractor(
            "https://x.com/tranvietanh0810/status/2070393711335489613"
        )

        media = await extractor.extract(
            TwitterSession(fx=(403, None), vx=(200, payload))
        )

        self.assertEqual(media[0]["type"], "hls")

    async def test_blocked_primary_mirror_falls_back_to_the_second(self):
        session = TwitterSession(
            fx=(403, None), vx=(200, self._vx_payload(1, "short caption"))
        )
        extractor = TwitterExtractor(
            "https://x.com/tranvietanh0810/status/2070393711335489613"
        )

        media = await extractor.extract(session)

        self.assertEqual(
            media[0]["filename"],
            "tranvietanh0810 - short caption - 2070393711335489613.mp4",
        )
        self.assertEqual(len(session.urls), 2)
        self.assertIn("fxtwitter", session.urls[0])
        self.assertIn("vxtwitter", session.urls[1])

    async def test_every_mirror_failing_names_each_reason(self):
        session = TwitterSession(fx=(403, None), vx=(500, None))
        extractor = TwitterExtractor(
            "https://x.com/tranvietanh0810/status/2070393711335489613"
        )

        with self.assertRaises(Exception) as caught:
            await extractor.extract(session)

        message = str(caught.exception)
        self.assertIn("fxtwitter: HTTP 403", message)
        self.assertIn("vxtwitter: HTTP 500", message)


class HlsFragmentTests(unittest.IsolatedAsyncioTestCase):
    """An HLS replay is latency-bound, so fragment concurrency must be tunable."""

    def _captured_options(self, fragment_concurrency=None):
        captured = {}

        class FakeYoutubeDL:
            def __init__(self, options):
                captured.update(options)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def download(self, urls):
                captured["_urls"] = urls

        with patch("backend.core.media.yt_dlp.YoutubeDL", FakeYoutubeDL):
            if fragment_concurrency is None:
                MediaProcessor.download_video("https://x/y.m3u8", "out.mp4")
            else:
                MediaProcessor.download_video(
                    "https://x/y.m3u8", "out.mp4", None, fragment_concurrency
                )
        return captured

    def test_fragment_concurrency_reaches_yt_dlp(self):
        options = self._captured_options(8)

        self.assertEqual(options["concurrent_fragment_downloads"], 8)

    def test_fragment_concurrency_defaults_to_serial(self):
        options = self._captured_options()

        self.assertEqual(options["concurrent_fragment_downloads"], 1)

    def test_fragment_concurrency_never_drops_below_one(self):
        # A zero would stall the download outright rather than merely slow it.
        options = self._captured_options(0)

        self.assertEqual(options["concurrent_fragment_downloads"], 1)

    async def test_hls_item_uses_the_per_host_limit_for_fragments(self):
        manager = DownloadManager.__new__(DownloadManager)
        manager.host_limit = 6
        manager.host_semaphores = {}
        manager.global_item_semaphore = ResizableLimiter(2)
        manager.wait_until_runnable = lambda task_id: asyncio.sleep(0)
        manager.is_task_cancelled = lambda task_id: False
        manager.is_chapter_cancelled = lambda task_id, folder: False

        recorded = {}

        def fake_download_video(url, output_path, headers=None, fragment_concurrency=1):
            recorded["fragment_concurrency"] = fragment_concurrency
            Path(output_path).write_bytes(b"video")

        item = {
            "url": "https://x.com/i/broadcasts/1qxvveqnVMzxB",
            "type": "hls",
            "filename": "broadcast.mp4",
        }

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "backend.core.downloader.MediaProcessor.download_video",
                fake_download_video,
            ):
                status, size = await manager._download_item(
                    None,
                    "task",
                    "https://x.com/u/status/1",
                    directory,
                    0,
                    item,
                    DummyLogger(),
                )
            written = Path(directory) / "broadcast.mp4"
            self.assertTrue(written.is_file())

        self.assertEqual(status, "success")
        self.assertEqual(size, 5)
        # The engine must hand yt-dlp the per-host budget, not leave it serial.
        self.assertEqual(recorded["fragment_concurrency"], 6)


class AsyncPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_resizable_limiter_drains_and_wakes_waiters(self):
        limiter = ResizableLimiter(2)
        await limiter.acquire()
        await limiter.acquire()
        waiter = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        await limiter.resize(1)
        self.assertTrue(limiter.draining)
        await limiter.release()
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        await limiter.resize(2)
        await asyncio.wait_for(waiter, timeout=1)
        self.assertEqual(limiter.active, 2)
        await limiter.release()
        await limiter.release()

    async def test_runtime_extraction_limit_resizes_active_pool(self):
        await resize_runtime_extraction_limit(1)
        gate = asyncio.Event()
        active = 0
        maximum = 0

        async def worker(value):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await gate.wait()
            active -= 1
            return value

        task = asyncio.create_task(
            bounded_map(range(4), worker, limit=4, runtime_limited=True)
        )
        for _ in range(100):
            if active == 1:
                break
            await asyncio.sleep(0)
        self.assertEqual(active, 1)

        await resize_runtime_extraction_limit(2)
        for _ in range(100):
            if active == 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(active, 2)
        gate.set()
        self.assertEqual(await task, [0, 1, 2, 3])
        self.assertEqual(maximum, 2)

    async def test_nested_runtime_limited_maps_do_not_deadlock(self):
        await resize_runtime_extraction_limit(1)

        async def outer(value):
            return await bounded_map(
                [value, value + 1],
                lambda inner: asyncio.sleep(0, result=inner),
                limit=2,
                runtime_limited=True,
            )

        result = await asyncio.wait_for(
            bounded_map([1], outer, limit=1, runtime_limited=True),
            timeout=1,
        )
        self.assertEqual(result, [[1, 2]])

    async def test_manager_applies_all_runtime_limits(self):
        manager = DownloadManager()
        manager.loop = asyncio.get_running_loop()
        manager.task_semaphore = ResizableLimiter(3)
        manager.global_item_semaphore = ResizableLimiter(6)
        manager.host_semaphores["example.com"] = ResizableLimiter(3)
        manager.task_item_limiters["task"] = ResizableLimiter(3)
        settings = {
            **DEFAULT_SETTINGS,
            "max_concurrent_tasks": 1,
            "max_concurrent_items": 2,
            "max_global_items": 2,
            "max_concurrent_per_host": 1,
            "max_extract_concurrency": 1,
        }

        runtime = await manager.apply_runtime_settings(settings)

        self.assertEqual(manager.task_semaphore.limit, 1)
        self.assertEqual(manager.global_item_semaphore.limit, 2)
        self.assertEqual(manager.host_semaphores["example.com"].limit, 1)
        self.assertEqual(manager.task_item_limiters["task"].limit, 2)
        self.assertTrue(runtime["applied_live"])

    async def test_cancel_task_waits_for_pipeline_cleanup(self):
        manager = DownloadManager()
        manager.loop = asyncio.get_running_loop()
        task_id = "active-delete"

        async def pipeline():
            while not manager.is_task_cancelled(task_id):
                await asyncio.sleep(0)
            await asyncio.sleep(0.01)

        task = asyncio.create_task(pipeline())
        manager.running_tasks.add(task)
        manager.task_handles[task_id] = task
        task.add_done_callback(
            lambda completed: manager._task_finished(task_id, completed)
        )

        self.assertTrue(await manager.cancel_task_and_wait(task_id, timeout=1))
        self.assertTrue(task.done())

    async def test_bounded_map_limits_active_workers(self):
        active = 0
        maximum = 0

        async def worker(value):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return value * 2

        result = await bounded_map(range(20), worker, limit=3)
        self.assertEqual(result, [value * 2 for value in range(20)])
        self.assertEqual(maximum, 3)

    async def test_download_stream_is_atomic(self):
        manager = DownloadManager()
        manager.loop = asyncio.get_running_loop()
        manager.global_item_semaphore = asyncio.Semaphore(2)
        manager.host_limit = 2
        manager.host_semaphores = {}
        manager.task_events["test-task"] = asyncio.Event()
        manager.task_events["test-task"].set()
        logger = DummyLogger()

        with tempfile.TemporaryDirectory() as directory:
            result, size = await manager._download_item(
                FakeSession(),
                "test-task",
                "https://example.com/gallery",
                Path(directory),
                0,
                {"url": "https://cdn.example.com/image.jpg", "filename": "image.jpg"},
                logger,
            )
            target = Path(directory) / "image.jpg"
            self.assertEqual(result, "success")
            self.assertEqual(size, 6)
            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertFalse((Path(directory) / "image.jpg.part").exists())

    async def test_download_converts_transport_to_gif_atomically(self):
        manager = DownloadManager()
        manager.loop = asyncio.get_running_loop()
        manager.global_item_semaphore = asyncio.Semaphore(2)
        manager.host_limit = 2
        manager.host_semaphores = {}
        manager.task_events["test-task"] = asyncio.Event()
        manager.task_events["test-task"].set()
        logger = DummyLogger()

        def fake_convert(source, target):
            self.assertEqual(Path(source).read_bytes(), b"abcdef")
            Path(target).write_bytes(b"GIF89a")

        with tempfile.TemporaryDirectory() as directory, patch(
            "backend.core.downloader.MediaProcessor.convert_to_gif",
            side_effect=fake_convert,
        ):
            result, size = await manager._download_item(
                FakeSession(),
                "test-task",
                "https://example.com/gallery",
                Path(directory),
                0,
                {
                    "url": "https://cdn.example.com/image.mp4",
                    "filename": "image.gif",
                    "convert_to": "gif",
                },
                logger,
            )

            target = Path(directory) / "image.gif"
            self.assertEqual(result, "success")
            self.assertEqual(size, 6)
            self.assertEqual(target.read_bytes(), b"GIF89a")
            self.assertFalse((Path(directory) / "image.gif.part").exists())
            self.assertFalse(
                (Path(directory) / "image.converted.part.gif").exists()
            )

    async def test_failed_conversion_falls_back_to_original_gif(self):
        manager = DownloadManager()
        manager.loop = asyncio.get_running_loop()
        manager.global_item_semaphore = asyncio.Semaphore(2)
        manager.host_limit = 2
        manager.host_semaphores = {}
        manager.task_events["test-task"] = asyncio.Event()
        manager.task_events["test-task"].set()
        logger = DummyLogger()
        session = UrlRecordingSession()
        gif_url = "https://cdn.example.com/image.gif"

        with tempfile.TemporaryDirectory() as directory, patch(
            "backend.core.downloader.MediaProcessor.convert_to_gif",
            side_effect=RuntimeError("converter unavailable"),
        ):
            result, size = await manager._download_item(
                session,
                "test-task",
                "https://example.com/gallery",
                Path(directory),
                0,
                {
                    "url": "https://cdn.example.com/image.mp4",
                    "filename": "image.gif",
                    "convert_to": "gif",
                    "fallback_url": gif_url,
                },
                logger,
            )

            self.assertEqual(result, "success")
            self.assertEqual(size, 6)
            self.assertEqual(
                session.urls,
                ["https://cdn.example.com/image.mp4", gif_url],
            )
            self.assertEqual(
                (Path(directory) / "image.gif").read_bytes(), b"abcdef"
            )
            self.assertTrue(
                any("Downloading original GIF" in message for message in logger.messages)
            )

    async def test_download_retry_resumes_partial_stream(self):
        manager = DownloadManager()
        manager.loop = asyncio.get_running_loop()
        manager.global_item_semaphore = asyncio.Semaphore(2)
        manager.host_limit = 2
        manager.host_semaphores = {}
        manager.task_events["test-task"] = asyncio.Event()
        manager.task_events["test-task"].set()
        logger = DummyLogger()
        session = ResumingSession()

        with tempfile.TemporaryDirectory() as directory:
            result, size = await manager._download_item(
                session,
                "test-task",
                "https://example.com/gallery",
                Path(directory),
                0,
                {
                    "url": "https://cdn.example.com/image.jpg",
                    "filename": "image.jpg",
                },
                logger,
            )

            target = Path(directory) / "image.jpg"
            self.assertEqual(result, "success")
            self.assertEqual(size, 6)
            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertNotIn("Range", session.requests[0]["headers"])
            self.assertEqual(
                session.requests[1]["headers"]["Range"], "bytes=3-"
            )
            self.assertTrue(
                any(message.startswith("RESUMING:") for message in logger.messages)
            )


if __name__ == "__main__":
    unittest.main()
