import asyncio
import inspect

from backend.core.config import get_settings
from backend.core.limits import ResizableLimiter


_runtime_extraction_limit = None
_runtime_extraction_loop = None
_active_extraction_limiters = {}


def _ensure_runtime_extraction_state():
    global _runtime_extraction_limit, _runtime_extraction_loop
    loop = asyncio.get_running_loop()
    if _runtime_extraction_loop is not loop:
        _runtime_extraction_loop = loop
        _runtime_extraction_limit = get_settings()["max_extract_concurrency"]
        _active_extraction_limiters.clear()
    elif _runtime_extraction_limit is None:
        _runtime_extraction_limit = get_settings()["max_extract_concurrency"]


async def resize_runtime_extraction_limit(limit):
    global _runtime_extraction_limit
    _ensure_runtime_extraction_state()
    _runtime_extraction_limit = max(1, int(limit))
    active_limiters = list(_active_extraction_limiters.items())
    await asyncio.gather(
        *(
            limiter.resize(min(site_cap, _runtime_extraction_limit))
            for limiter, site_cap in active_limiters
        )
    )
    return {
        "limit": _runtime_extraction_limit,
        "active": sum(limiter.active for limiter, _site_cap in active_limiters),
        "draining": any(limiter.draining for limiter, _site_cap in active_limiters),
    }


async def bounded_map(
    items,
    worker,
    *,
    limit,
    return_exceptions=False,
    on_progress=None,
    runtime_limited=False,
):
    """Map an async worker with stable ordering and bounded worker creation."""
    values = list(items)
    if not values:
        return []
    iterator = iter(enumerate(values))
    results = [None] * len(values)
    completed = 0
    errors = []
    site_cap = max(1, int(limit))
    runtime_limiter = None
    if runtime_limited:
        _ensure_runtime_extraction_state()
        runtime_limiter = ResizableLimiter(min(site_cap, _runtime_extraction_limit))
        _active_extraction_limiters[runtime_limiter] = site_cap

    async def run_worker():
        nonlocal completed
        while True:
            try:
                index, value = next(iterator)
            except StopIteration:
                return
            try:
                if runtime_limiter:
                    async with runtime_limiter:
                        results[index] = await worker(value)
                else:
                    results[index] = await worker(value)
            except Exception as exc:
                results[index] = exc
                errors.append(exc)
            completed += 1
            if on_progress:
                callback_result = on_progress(completed, len(values), len(errors))
                if inspect.isawaitable(callback_result):
                    await callback_result

    worker_count = max(1, min(site_cap, len(values)))
    try:
        await asyncio.gather(*(run_worker() for _ in range(worker_count)))
    finally:
        if runtime_limiter:
            _active_extraction_limiters.pop(runtime_limiter, None)
    if errors and not return_exceptions:
        raise errors[0]
    return results


def deduplicate(values, key=None):
    seen = set()
    result = []
    for value in values:
        marker = key(value) if key else value
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result
