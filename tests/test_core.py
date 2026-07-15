import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

from backend.core.downloader import DownloadManager
from backend.core.paths import resolve_within, sanitize_component
from backend.plugins.manager import PluginManager
from backend.plugins.utils import bounded_map


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
    def test_sanitize_rejects_dot_components(self):
        self.assertEqual(sanitize_component("..", fallback="safe"), "safe")

    def test_resolve_within_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                resolve_within(directory, "..", "outside")


class PluginManagerTests(unittest.TestCase):
    def test_domain_matching_is_hostname_aware(self):
        self.assertTrue(PluginManager._matches("https://hitomi.la/galleries/1.html", "hitomi.la"))
        self.assertFalse(PluginManager._matches("https://evil.example/?next=hitomi.la", "hitomi.la"))
        self.assertFalse(PluginManager._matches("file:///tmp/hitomi.la", "hitomi.la"))


class AsyncPipelineTests(unittest.IsolatedAsyncioTestCase):
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
