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
from backend.core.downloader import DownloadManager
from backend.core.limits import ResizableLimiter
from backend.core.paths import resolve_within, sanitize_component
from backend.plugins.manager import PluginManager
from backend.plugins.utils import bounded_map, resize_runtime_extraction_limit
from plugins.hitomi import HitomiExtractor


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


class PathTests(unittest.TestCase):
    def test_default_download_directory_is_absolute(self):
        self.assertTrue(Path(DEFAULT_SETTINGS["download_dir"]).is_absolute())

    def test_sanitize_rejects_dot_components(self):
        self.assertEqual(sanitize_component("..", fallback="safe"), "safe")

    def test_resolve_within_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                resolve_within(directory, "..", "outside")


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


if __name__ == "__main__":
    unittest.main()
