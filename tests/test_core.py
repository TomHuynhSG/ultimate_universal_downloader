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
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    async def get(self, url, **kwargs):
        self.urls.append(url)
        payload = self.payload

        class Response:
            status_code = 200

            def json(self):
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

    def _payload(self, video_count, text=""):
        return {
            "user_screen_name": "tranvietanh0810",
            "text": text,
            "media_extended": [
                {
                    "type": "video",
                    "url": f"https://video.twimg.com/amplify_video/{index}/vid.mp4",
                    "thumbnail_url": f"https://pbs.twimg.com/{index}.jpg",
                }
                for index in range(video_count)
            ],
        }

    async def test_every_video_of_a_post_keeps_its_own_file(self):
        session = TwitterSession(self._payload(2, self.LONG_TEXT))
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
        session = TwitterSession(self._payload(1, "short caption"))
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
        session = TwitterSession(self._payload(1))
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
        payload["media_extended"] = [{"type": "image", "url": "https://x/y.jpg"}]
        extractor = TwitterExtractor(
            "https://x.com/tranvietanh0810/status/2070393711335489613"
        )

        with self.assertRaisesRegex(Exception, "No video or gif"):
            await extractor.extract(TwitterSession(payload))


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
