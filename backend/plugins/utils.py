import inspect


async def bounded_map(items, worker, *, limit, return_exceptions=False, on_progress=None):
    """Map an async worker over items with a fixed worker count and stable ordering."""
    values = list(items)
    if not values:
        return []
    iterator = iter(enumerate(values))
    results = [None] * len(values)
    completed = 0
    errors = []

    async def run_worker():
        nonlocal completed
        while True:
            try:
                index, value = next(iterator)
            except StopIteration:
                return
            try:
                results[index] = await worker(value)
            except Exception as exc:
                results[index] = exc
                errors.append(exc)
            completed += 1
            if on_progress:
                callback_result = on_progress(completed, len(values), len(errors))
                if inspect.isawaitable(callback_result):
                    await callback_result

    worker_count = max(1, min(int(limit), len(values)))
    import asyncio

    await asyncio.gather(*(run_worker() for _ in range(worker_count)))
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
