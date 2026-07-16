import asyncio


class ResizableLimiter:
    """Async capacity limiter whose limit can change without cancelling holders."""

    def __init__(self, limit):
        self._limit = max(1, int(limit))
        self._active = 0
        self._condition = asyncio.Condition()

    @property
    def limit(self):
        return self._limit

    @property
    def active(self):
        return self._active

    @property
    def draining(self):
        return self._active > self._limit

    async def resize(self, limit):
        async with self._condition:
            self._limit = max(1, int(limit))
            self._condition.notify_all()

    async def acquire(self):
        async with self._condition:
            await self._condition.wait_for(lambda: self._active < self._limit)
            self._active += 1

    async def release(self):
        async with self._condition:
            if self._active <= 0:
                raise RuntimeError("ResizableLimiter released without an active holder")
            self._active -= 1
            self._condition.notify_all()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        await self.release()
