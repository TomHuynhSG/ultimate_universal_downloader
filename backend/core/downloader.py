from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

import aiofiles
from curl_cffi.requests import AsyncSession

from backend.core.config import get_settings
from backend.core.limits import ResizableLimiter
from backend.core.media import MediaProcessor
from backend.core.paths import (
    PROJECT_ROOT,
    resolve_within,
    sanitize_component,
    sanitize_filename,
    task_output_path,
    unique_filename,
)
from backend.database.models import DownloadTask, SessionLocal
from backend.plugins.manager import PluginManager
from backend.plugins.utils import resize_runtime_extraction_limit


RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
MAX_AUTO_RETRIES = 2
KNOWN_EXTENSIONS = {
    ".avif",
    ".gif",
    ".jpeg",
    ".jpg",
    ".m4v",
    ".mkv",
    ".mp4",
    ".pdf",
    ".png",
    ".webm",
    ".webp",
    ".zip",
}
# curl_cffi only aborts a streamed transfer below 1 byte/s, and X's video CDN
# can leave a connection silent for minutes while a fresh one runs at full
# speed. These bound how long one connection may idle or crawl before it is
# dropped and the download resumes from the partial file on a new one.
STALL_TIMEOUT_SECONDS = 20
LOW_SPEED_WINDOW_SECONDS = 30
LOW_SPEED_BYTES_PER_SECOND = 16 * 1024
# Reconnects that moved data forward are free; a stalled connection that
# delivered nothing counts as an ordinary failed attempt instead.
MAX_FREE_RECONNECTS = 10


class StalledTransfer(RuntimeError):
    """A streamed download stopped making progress on its connection."""

    def __init__(self, message, received):
        super().__init__(message)
        self.received = received


class TransferInterrupted(RuntimeError):
    """The owning task was paused or cancelled while a transfer was in flight."""


def _abort_stream(response):
    """Tear down a curl_cffi stream without waiting for its transfer to end.

    Leaving `session.stream()` awaits the background transfer task, which on a
    silent connection only returns once curl gives up on it. Cancelling that
    task and detaching it releases the handle immediately instead.
    """
    quit_now = getattr(response, "quit_now", None)
    if quit_now is not None:
        quit_now.set()
    stream_task = getattr(response, "astream_task", None)
    if stream_task is not None and not stream_task.done():
        response.astream_task = None
        stream_task.cancel()


@asynccontextmanager
async def _open_stream(session, *args, **kwargs):
    """`session.stream()` that never waits on a transfer abandoned mid-body."""
    async with session.stream(*args, **kwargs) as response:
        try:
            yield response
        finally:
            _abort_stream(response)


async def _watched_chunks(
    response,
    stall_timeout,
    window_seconds,
    min_rate,
    interrupted=None,
    poll_seconds=0.5,
):
    """Yield body chunks, raising StalledTransfer when the connection idles or crawls.

    While waiting for data it also polls `interrupted`, so pausing or deleting
    a task takes effect at once instead of after the stall timeout.
    """
    chunks = response.aiter_content()
    received = 0
    window_started = time.monotonic()
    window_bytes = 0
    too_slow = None
    while True:
        next_chunk = asyncio.ensure_future(anext(chunks))
        try:
            idle_started = time.monotonic()
            while not next_chunk.done():
                idle = time.monotonic() - idle_started
                if idle >= stall_timeout:
                    raise StalledTransfer(f"no data for {stall_timeout:g}s", received)
                await asyncio.wait(
                    {next_chunk}, timeout=min(poll_seconds, stall_timeout - idle)
                )
                if not next_chunk.done() and interrupted and interrupted():
                    raise TransferInterrupted(f"after {received} bytes on this connection")
        finally:
            if not next_chunk.done():
                next_chunk.cancel()
        try:
            chunk = next_chunk.result()
        except StopAsyncIteration:
            return
        if too_slow:
            # Only give up once more body is actually pending: a crawl that
            # just delivered the final chunk must still complete, or the resume
            # would ask for a range past the end of the file.
            raise StalledTransfer(too_slow, received)
        received += len(chunk)
        window_bytes += len(chunk)
        elapsed = time.monotonic() - window_started
        if elapsed >= window_seconds:
            if window_bytes / max(elapsed, 1e-9) < min_rate:
                too_slow = f"below {min_rate / 1024:g} KiB/s for {elapsed:.0f}s"
            else:
                window_started = time.monotonic()
                window_bytes = 0
        yield chunk


def _utcnow():
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def _item_filename(index, item):
    """Resolve the on-disk filename the downloader will use for one media item."""
    image_url = item if isinstance(item, str) else item.get("url") or ""
    media_type = "" if isinstance(item, str) else item.get("type", "")
    explicit_filename = None if isinstance(item, str) else item.get("filename")
    url_path = urllib.parse.unquote(urllib.parse.urlparse(image_url).path)
    url_name = Path(url_path).name
    if explicit_filename:
        filename = sanitize_filename(explicit_filename, fallback=f"{index + 1:03d}.bin")
    elif Path(url_name).suffix.lower() in KNOWN_EXTENSIONS:
        filename = sanitize_filename(url_name, fallback=f"{index + 1:03d}.bin")
    else:
        filename = f"{index + 1:03d}.jpg"
    is_hls = media_type == "hls" or url_path.lower().endswith(".m3u8")
    if is_hls and Path(filename).suffix.lower() != ".mp4":
        filename = f"{Path(filename).stem}.mp4"
    return filename


def _assign_unique_filenames(items):
    """Pin an explicit, collision-free filename on every item of one task.

    Long titles are truncated to fit the filename budget, so unrelated items
    can sanitize down to the same name. Without this pass they would share one
    `.part` file, download over each other, and silently produce a single
    output file for several distinct media URLs.
    """
    taken_by_folder = {}
    for index, item in enumerate(items):
        folder = "" if isinstance(item, str) else item.get("folder", "")
        taken = taken_by_folder.setdefault(folder, set())
        resolved = unique_filename(_item_filename(index, item), taken)
        taken.add(resolved.lower())
        if isinstance(item, str):
            items[index] = {"url": item, "filename": resolved}
        else:
            item["filename"] = resolved
    return items


def _task_snapshot(task_id):
    db = SessionLocal()
    try:
        task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
        if not task:
            return None
        return {
            "id": task.id,
            "url": task.url,
            "title": task.title,
            "thumbnail": task.thumbnail,
            "status": task.status,
            "progress": task.progress,
            "details": task.details or "{}",
            "output_path": task.output_path,
        }
    finally:
        db.close()


def _update_task(task_id, **fields):
    db = SessionLocal()
    try:
        task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
        if not task:
            return None
        for key, value in fields.items():
            setattr(task, key, value)
        task.updated_at = _utcnow()
        db.commit()
        return task.status
    finally:
        db.close()


def _set_statuses(task_ids, status, only_from):
    """Move each listed task to `status` if it is currently in `only_from`."""
    db = SessionLocal()
    try:
        tasks = (
            db.query(DownloadTask)
            .filter(DownloadTask.id.in_(list(task_ids)))
            .filter(DownloadTask.status.in_(list(only_from)))
            .all()
        )
        for task in tasks:
            task.status = status
            task.updated_at = _utcnow()
        db.commit()
    finally:
        db.close()


def _recover_interrupted_tasks():
    db = SessionLocal()
    try:
        tasks = (
            db.query(DownloadTask)
            .filter(DownloadTask.status.in_(["pending", "extracting", "downloading"]))
            .all()
        )
        recovered = []
        for task in tasks:
            task.status = "pending"
            task.updated_at = _utcnow()
            recovered.append((task.id, task.url))
        db.commit()
        return recovered
    finally:
        db.close()


class TaskLogger:
    def __init__(self, task_id):
        self.task_id = task_id
        self.path = PROJECT_ROOT / "task_logs" / f"{task_id}.log"
        self.queue = asyncio.Queue(maxsize=4096)
        self.worker = None
        self.dropped = 0

    async def start(self):
        await asyncio.to_thread(self.path.parent.mkdir, parents=True, exist_ok=True)
        self.worker = asyncio.create_task(self._run())

    def log(self, message):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        if not message.startswith("SUCCESS:"):
            print(f"[Task {self.task_id}] {message}")
        try:
            self.queue.put_nowait(line)
        except asyncio.QueueFull:
            self.dropped += 1

    async def _run(self):
        async with aiofiles.open(self.path, "a", encoding="utf-8") as handle:
            dirty = False
            while True:
                try:
                    line = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except TimeoutError:
                    if dirty:
                        await handle.flush()
                        dirty = False
                    continue
                try:
                    if line is None:
                        if self.dropped:
                            await handle.write(f"[logger] Dropped {self.dropped} repetitive messages.\n")
                        await handle.flush()
                        return
                    await handle.write(line)
                    dirty = True
                finally:
                    self.queue.task_done()

    async def close(self):
        if not self.worker:
            return
        await self.queue.put(None)
        await self.worker


class DownloadManager:
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.queue = asyncio.Queue()
        self.plugin_manager = PluginManager()
        self.plugin_manager.load_plugins()
        self.cancelled_chapters = {}
        self._cancel_lock = threading.RLock()
        self._pending_before_start = []
        self._pending_lock = threading.RLock()
        self.loop = None
        self.dispatcher_task = None
        self.running_tasks = set()
        self.task_handles = {}
        self.task_item_limiters = {}
        self.item_limit = 1
        self.task_events = {}
        self.cancelled_tasks = set()
        self.global_item_semaphore = None
        self.task_semaphore = None
        self.host_semaphores = {}
        self.host_limit = 1
        self.stall_timeout = STALL_TIMEOUT_SECONDS
        self.low_speed_window = LOW_SPEED_WINDOW_SECONDS
        self.low_speed_rate = LOW_SPEED_BYTES_PER_SECOND
        # A prioritized task runs outside the task limit while the dispatcher
        # holds every queued task back and the tasks it paused wait for it.
        self.priority_task_id = None
        self._priority_idle = asyncio.Event()
        self._priority_idle.set()
        self._priority_paused = set()
        self._launched_out_of_band = set()

    async def start(self):
        if self.dispatcher_task and not self.dispatcher_task.done():
            return
        self.loop = asyncio.get_running_loop()
        settings = get_settings()
        self.task_semaphore = ResizableLimiter(settings["max_concurrent_tasks"])
        self.global_item_semaphore = ResizableLimiter(settings["max_global_items"])
        self.item_limit = settings["max_concurrent_items"]
        self.host_limit = settings["max_concurrent_per_host"]
        self.host_semaphores = {}
        self.dispatcher_task = asyncio.create_task(self._dispatcher(), name="download-dispatcher")

        recovered = await asyncio.to_thread(_recover_interrupted_tasks)
        with self._pending_lock:
            recovered.extend(self._pending_before_start)
            self._pending_before_start.clear()
        for item in recovered:
            self.queue.put_nowait(item)

    async def apply_runtime_settings(self, settings):
        running_loop = asyncio.get_running_loop()
        if self.loop and self.loop.is_running() and self.loop is not running_loop:
            future = asyncio.run_coroutine_threadsafe(
                self._apply_runtime_settings_local(settings), self.loop
            )
            return await asyncio.wrap_future(future)
        return await self._apply_runtime_settings_local(settings)

    async def _apply_runtime_settings_local(self, settings):
        self.item_limit = settings["max_concurrent_items"]
        self.host_limit = settings["max_concurrent_per_host"]
        resize_operations = [
            resize_runtime_extraction_limit(settings["max_extract_concurrency"])
        ]
        if self.task_semaphore:
            resize_operations.append(
                self.task_semaphore.resize(settings["max_concurrent_tasks"])
            )
        if self.global_item_semaphore:
            resize_operations.append(
                self.global_item_semaphore.resize(settings["max_global_items"])
            )
        resize_operations.extend(
            limiter.resize(self.host_limit)
            for limiter in list(self.host_semaphores.values())
        )
        resize_operations.extend(
            limiter.resize(self.item_limit)
            for limiter in list(self.task_item_limiters.values())
        )
        results = await asyncio.gather(*resize_operations)
        extraction = results[0]

        task_active = self.task_semaphore.active if self.task_semaphore else 0
        global_active = (
            self.global_item_semaphore.active if self.global_item_semaphore else 0
        )
        draining = (
            bool(self.task_semaphore and self.task_semaphore.draining)
            or bool(self.global_item_semaphore and self.global_item_semaphore.draining)
            or any(limiter.draining for limiter in self.host_semaphores.values())
            or any(limiter.draining for limiter in self.task_item_limiters.values())
            or extraction["draining"]
        )
        return {
            "applied_live": True,
            "draining": draining,
            "active_tasks": task_active,
            "active_downloads": global_active,
            "active_extractions": extraction["active"],
        }

    async def stop(self):
        if self.dispatcher_task:
            self.dispatcher_task.cancel()
        for task in list(self.running_tasks):
            task.cancel()
        await asyncio.gather(*self.running_tasks, return_exceptions=True)
        if self.dispatcher_task:
            await asyncio.gather(self.dispatcher_task, return_exceptions=True)
        self.running_tasks.clear()
        self.task_handles.clear()
        self.task_item_limiters.clear()
        self.dispatcher_task = None

    def enqueue(self, task_id, url):
        item = (task_id, url)
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._enqueue_item, item)
        else:
            with self._pending_lock:
                self._pending_before_start.append(item)

    def _enqueue_item(self, item):
        task_id, _url = item
        self._forget_task(task_id)
        self.queue.put_nowait(item)

    def _schedule_control(self, callback, *args):
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(callback, *args)

    def set_task_paused(self, task_id, paused):
        self._schedule_control(self._set_task_paused, task_id, paused)

    def _set_task_paused(self, task_id, paused):
        event = self.task_events.setdefault(task_id, asyncio.Event())
        if paused:
            event.clear()
        else:
            event.set()
        # A manual pause or resume overrides whatever prioritizing decided.
        self._priority_paused.discard(task_id)
        if paused and task_id == self.priority_task_id:
            self._release_priority()

    def is_task_paused(self, task_id):
        event = self.task_events.get(task_id)
        return event is not None and not event.is_set()

    async def prioritize_task(self, task_id):
        running_loop = asyncio.get_running_loop()
        if self.loop and self.loop.is_running() and self.loop is not running_loop:
            future = asyncio.run_coroutine_threadsafe(
                self._prioritize_task_local(task_id), self.loop
            )
            return await asyncio.wrap_future(future)
        return await self._prioritize_task_local(task_id)

    async def _prioritize_task_local(self, task_id):
        snapshot = await asyncio.to_thread(_task_snapshot, task_id)
        if not snapshot or snapshot["status"] not in {
            "pending",
            "extracting",
            "downloading",
            "paused",
        }:
            return False

        self.priority_task_id = task_id
        self._priority_idle.clear()
        self._priority_paused.discard(task_id)

        # Only runnable tasks are recorded, so a task the user paused by hand
        # stays paused once the prioritized task finishes.
        paused_now = []
        for other_id in list(self.task_handles):
            if other_id == task_id or self.is_task_paused(other_id):
                continue
            self.task_events.setdefault(other_id, asyncio.Event()).clear()
            self._priority_paused.add(other_id)
            paused_now.append(other_id)
        if paused_now:
            await asyncio.to_thread(
                _set_statuses, paused_now, "paused", only_from={"downloading"}
            )

        if task_id in self.task_handles:
            self.task_events.setdefault(task_id, asyncio.Event()).set()
            await asyncio.to_thread(
                _set_statuses, [task_id], "downloading", only_from={"paused"}
            )
        else:
            # Pending or paused-before-start: it is either waiting in the
            # queue or not queued at all, so start it directly and let the
            # dispatcher drop its queue entry when it reaches it.
            self._forget_task(task_id)
            await asyncio.to_thread(
                _set_statuses, [task_id], "pending", only_from={"paused"}
            )
            if snapshot["status"] == "pending":
                self._launched_out_of_band.add(task_id)
            self._launch(task_id, snapshot["url"], holds_slot=False)
        return True

    def _release_priority(self):
        self.priority_task_id = None
        self._priority_idle.set()
        resumed = list(self._priority_paused)
        self._priority_paused.clear()
        for other_id in resumed:
            event = self.task_events.get(other_id)
            if event is not None:
                event.set()
        if resumed:
            task = asyncio.create_task(
                asyncio.to_thread(
                    _set_statuses, resumed, "downloading", only_from={"paused"}
                )
            )
            self.running_tasks.add(task)
            task.add_done_callback(self.running_tasks.discard)

    def cancel_task(self, task_id):
        self._schedule_control(self._cancel_task, task_id)

    def _cancel_task(self, task_id):
        self.cancelled_tasks.add(task_id)
        self.task_events.setdefault(task_id, asyncio.Event()).set()

    async def cancel_task_and_wait(self, task_id, timeout=30):
        running_loop = asyncio.get_running_loop()
        if self.loop and self.loop.is_running() and self.loop is not running_loop:
            future = asyncio.run_coroutine_threadsafe(
                self._cancel_task_and_wait_local(task_id, timeout), self.loop
            )
            return await asyncio.wrap_future(future)
        return await self._cancel_task_and_wait_local(task_id, timeout)

    async def _cancel_task_and_wait_local(self, task_id, timeout):
        self._cancel_task(task_id)
        task = self.task_handles.get(task_id)
        if not task:
            return True
        done, _pending = await asyncio.wait({task}, timeout=max(0, timeout))
        return task in done

    async def wait_until_runnable(self, task_id):
        event = self.task_events.setdefault(task_id, asyncio.Event())
        if task_id not in self.cancelled_tasks and not event.is_set():
            snapshot = await asyncio.to_thread(_task_snapshot, task_id)
            if snapshot and snapshot["status"] != "paused":
                event.set()
        await event.wait()

    def is_task_cancelled(self, task_id):
        return task_id in self.cancelled_tasks

    def cancel_chapter(self, task_id, chapter_name):
        with self._cancel_lock:
            self.cancelled_chapters.setdefault(task_id, set()).add(chapter_name)

    def load_cancelled_chapters(self, task_id, chapters):
        with self._cancel_lock:
            self.cancelled_chapters.setdefault(task_id, set()).update(chapters)

    def get_cancelled_chapters(self, task_id):
        with self._cancel_lock:
            return set(self.cancelled_chapters.get(task_id, set()))

    def is_chapter_cancelled(self, task_id, chapter_name):
        with self._cancel_lock:
            return chapter_name in self.cancelled_chapters.get(task_id, set())

    def forget_task(self, task_id):
        with self._cancel_lock:
            self.cancelled_chapters.pop(task_id, None)
        self._schedule_control(self._forget_task, task_id)

    def _forget_task(self, task_id):
        self.task_events.pop(task_id, None)
        self.cancelled_tasks.discard(task_id)

    def clear_controls(self):
        with self._cancel_lock:
            self.cancelled_chapters.clear()
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._clear_controls)

    def _clear_controls(self):
        self.cancelled_tasks.update(self.task_events)
        for event in self.task_events.values():
            event.set()

    @asynccontextmanager
    async def download_slot(self, url):
        hostname = (urllib.parse.urlparse(url).hostname or "unknown").lower()
        host_semaphore = self.host_semaphores.setdefault(
            hostname, ResizableLimiter(self.host_limit)
        )
        async with host_semaphore:
            async with self.global_item_semaphore:
                yield

    async def _dispatcher(self):
        print("Download dispatcher started.")
        while True:
            task_id, url = await self.queue.get()
            if task_id in self._launched_out_of_band or task_id in self.task_handles:
                # Already started by prioritizing, or still running.
                self._launched_out_of_band.discard(task_id)
                self.queue.task_done()
                continue
            while True:
                await self._priority_idle.wait()
                await self.task_semaphore.acquire()
                if self._priority_idle.is_set():
                    break
                # A task was prioritized while this one waited for a slot.
                await self.task_semaphore.release()
            if task_id in self._launched_out_of_band or task_id in self.task_handles:
                self._launched_out_of_band.discard(task_id)
                await self.task_semaphore.release()
                self.queue.task_done()
                continue
            self._launch(task_id, url, holds_slot=True)

    def _launch(self, task_id, url, holds_slot):
        task = asyncio.create_task(self._run_queued_task(task_id, url, holds_slot))
        self.running_tasks.add(task)
        self.task_handles[task_id] = task
        task.add_done_callback(
            lambda completed, current_id=task_id: self._task_finished(
                current_id, completed
            )
        )

    def _task_finished(self, task_id, task):
        self.running_tasks.discard(task)
        if self.task_handles.get(task_id) is task:
            self.task_handles.pop(task_id, None)
        self._priority_paused.discard(task_id)
        if task_id == self.priority_task_id:
            self._release_priority()

    async def _run_queued_task(self, task_id, url, holds_slot=True):
        try:
            # The pipeline asks for an automatic retry by returning True. The
            # retry runs here while the task still holds its slot, so it starts
            # right away instead of behind everything waiting in the queue.
            while await self._process_single_task(task_id, url):
                if task_id != self.priority_task_id:
                    # Like a queued task, hold back while another one is
                    # prioritized.
                    await self._priority_idle.wait()
        finally:
            if holds_slot:
                await self.task_semaphore.release()
                self.queue.task_done()

    async def _process_single_task(self, task_id, url):
        logger = TaskLogger(task_id)
        await logger.start()
        pipeline_started = time.monotonic()
        try:
            logger.log(f"Initialized processing for URL: {url}")
            snapshot = await asyncio.to_thread(_task_snapshot, task_id)
            if (
                self.is_task_cancelled(task_id)
                or not snapshot
                or snapshot["status"] in {"deleted", "cancelled"}
            ):
                logger.log("Task was deleted while waiting in the queue. Skipping.")
                return

            try:
                existing_details = json.loads(snapshot["details"] or "{}")
            except (TypeError, json.JSONDecodeError):
                existing_details = {}
            cancelled = existing_details.get("_cancelled", [])
            self.load_cancelled_chapters(task_id, cancelled)
            # Counts the automatic retries this task has used; a manual
            # restart resets the details and with them this count.
            try:
                auto_retries = int(existing_details.get("_auto_retries", 0))
            except (TypeError, ValueError):
                auto_retries = 0

            event = self.task_events.setdefault(task_id, asyncio.Event())
            event.set()

            await asyncio.to_thread(
                _update_task,
                task_id,
                status="extracting",
                progress=0.0,
                details=json.dumps(
                    {
                        "_cancelled": sorted(self.get_cancelled_chapters(task_id)),
                        "_auto_retries": auto_retries,
                        "_meta": {"phase": "extracting", "completed": 0, "total": 0},
                    }
                ),
            )
            logger.log("Starting plugin extraction...")

            extraction_started = time.monotonic()
            extraction_write_lock = asyncio.Lock()
            last_extraction_write = 0.0

            async def extraction_progress(payload):
                nonlocal last_extraction_write
                now = time.monotonic()
                is_final = payload.get("total") and payload.get("completed") == payload.get("total")
                if not is_final and now - last_extraction_write < 0.5:
                    return
                async with extraction_write_lock:
                    now = time.monotonic()
                    if not is_final and now - last_extraction_write < 0.5:
                        return
                    last_extraction_write = now
                    details = {
                        "_cancelled": sorted(self.get_cancelled_chapters(task_id)),
                        "_auto_retries": auto_retries,
                        "_meta": {**payload, "phase": "extracting"},
                    }
                    await asyncio.to_thread(
                        _update_task,
                        task_id,
                        details=json.dumps(details),
                    )

            extractor = self.plugin_manager.get_extractor(url, progress_callback=extraction_progress)
            if not extractor:
                raise RuntimeError(f"No supported plugin found for {url}")
            logger.log(f"Matched plugin: {extractor.__class__.__name__}")

            timeout = get_settings()["request_timeout_seconds"]
            async with AsyncSession(impersonate="chrome", timeout=timeout) as session:
                extracted_urls = await extractor.extract(session)
                extraction_seconds = time.monotonic() - extraction_started
                logger.log(
                    f"Extraction finished in {extraction_seconds:.2f}s. Title: {extractor.title}"
                )

                snapshot = await asyncio.to_thread(_task_snapshot, task_id)
                if not snapshot or snapshot["status"] in {"deleted", "cancelled"}:
                    logger.log("Task was deleted during extraction. Aborting before download.")
                    return
                if not isinstance(extracted_urls, list):
                    extracted_urls = list(extracted_urls or [])

                for item in extracted_urls:
                    if isinstance(item, dict) and item.get("folder"):
                        item["folder"] = sanitize_component(
                            item["folder"], fallback="Media", max_length=175
                        )
                _assign_unique_filenames(extracted_urls)

                total = len(extracted_urls)
                if total == 0:
                    raise RuntimeError("Plugin ran successfully but returned no media items")

                progress_details = {
                    "_cancelled": sorted(self.get_cancelled_chapters(task_id)),
                    "_auto_retries": auto_retries,
                    "_meta": {
                        "phase": "downloading",
                        "extraction_seconds": int(extraction_seconds),
                    },
                }
                for item in extracted_urls:
                    folder = "" if isinstance(item, str) else item.get("folder", "")
                    folder_name = folder or "Media"
                    if self.is_chapter_cancelled(task_id, folder_name):
                        continue
                    progress_details.setdefault(
                        folder_name, {"total": 0, "done": 0, "failed": 0, "size_bytes": 0}
                    )["total"] += 1

                settings = get_settings()
                output_path = task_output_path(
                    settings["download_dir"],
                    extractor.title,
                    flat_directory=getattr(extractor, "flat_directory", False),
                )
                await asyncio.to_thread(output_path.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(
                    _update_task,
                    task_id,
                    # Another task may have been prioritized during extraction.
                    status="paused" if self.is_task_paused(task_id) else "downloading",
                    title=extractor.title,
                    thumbnail=getattr(extractor, "thumbnail", None),
                    output_path=str(output_path),
                    details=json.dumps(progress_details),
                )

                logger.log(f"Extracted {total} files. Target directory: {output_path}")
                item_limiter = ResizableLimiter(self.item_limit)
                self.task_item_limiters[task_id] = item_limiter
                worker_count = min(total, 50)
                logger.log(
                    f"Starting {worker_count} queue workers "
                    f"({item_limiter.limit} active per task, "
                    f"{self.global_item_semaphore.limit} global, "
                    f"{self.host_limit} per host)."
                )

                state_lock = asyncio.Lock()
                state = {
                    "processed": 0,
                    "failed": 0,
                    "successful": 0,
                    "size_bytes": 0,
                    "transferred": 0,
                }
                # Bytes received so far for each item still downloading, so a
                # task holding one large video reports progress before it ends.
                in_flight = {}
                download_started = time.monotonic()
                finished = asyncio.Event()
                work_queue = asyncio.Queue(maxsize=max(2, worker_count * 2))

                def byte_reporter(index):
                    def report(received, expected):
                        previous = in_flight.get(index, (0, None))[0]
                        # A resumed connection restarts from the partial size,
                        # so only forward movement counts as transferred.
                        state["transferred"] += max(0, received - previous)
                        in_flight[index] = (received, expected)

                    return report

                async def record_result(index, folder_name, result, file_size):
                    async with state_lock:
                        in_flight.pop(index, None)
                        state["processed"] += 1
                        if result in {"success", "skipped"}:
                            state["successful"] += 1
                            if folder_name in progress_details:
                                progress_details[folder_name]["done"] += 1
                                progress_details[folder_name]["size_bytes"] += file_size
                            state["size_bytes"] += file_size
                        elif result == "failed":
                            state["failed"] += 1
                            if folder_name in progress_details:
                                progress_details[folder_name]["failed"] += 1

                async def producer():
                    for index, item in enumerate(extracted_urls):
                        if self.is_task_cancelled(task_id):
                            break
                        await work_queue.put((index, item))
                    for _ in range(worker_count):
                        await work_queue.put(None)

                async def worker():
                    while True:
                        entry = await work_queue.get()
                        try:
                            if entry is None:
                                return
                            index, item = entry
                            folder = "" if isinstance(item, str) else item.get("folder", "")
                            folder_name = folder or "Media"
                            try:
                                async with item_limiter:
                                    result, file_size = await self._download_item(
                                        session,
                                        task_id,
                                        url,
                                        output_path,
                                        index,
                                        item,
                                        logger,
                                        on_progress=byte_reporter(index),
                                    )
                            except Exception as exc:
                                logger.log(f"ERROR: Worker failed for item {index + 1}: {exc}")
                                result, file_size = "failed", 0
                            await record_result(index, folder_name, result, file_size)
                        finally:
                            work_queue.task_done()

                async def progress_updater():
                    speed = 0.0
                    last_transferred = 0
                    last_tick = time.monotonic()
                    while not finished.is_set():
                        await asyncio.sleep(0.5)
                        cancelled_now = self.get_cancelled_chapters(task_id)
                        async with state_lock:
                            for chapter in cancelled_now:
                                progress_details.pop(chapter, None)
                            progress_details["_cancelled"] = sorted(cancelled_now)
                            now = time.monotonic()
                            elapsed = now - download_started
                            # Items still in flight count by the fraction of
                            # their bytes received, when the size is known.
                            partial = sum(
                                min(1.0, received / expected)
                                for received, expected in in_flight.values()
                                if expected
                            )
                            units = state["processed"] + partial
                            rate = units / elapsed if elapsed else 0
                            remaining = max(0.0, total - units)
                            tick = now - last_tick
                            if tick > 0:
                                sample = (state["transferred"] - last_transferred) / tick
                                speed = sample if not speed else 0.7 * speed + 0.3 * sample
                            last_transferred = state["transferred"]
                            last_tick = now
                            progress_details["_meta"] = {
                                "phase": "downloading",
                                "eta_seconds": int(remaining / rate) if rate else 0,
                                "elapsed_seconds": int(elapsed),
                                "extraction_seconds": int(extraction_seconds),
                                "failed_items": state["failed"],
                                "downloaded_bytes": state["transferred"],
                                "speed_bps": int(speed),
                            }
                            details_json = json.dumps(progress_details)
                            progress = float(int((units / total) * 100))
                        status = await asyncio.to_thread(
                            _update_task,
                            task_id,
                            progress=progress,
                            details=details_json,
                        )
                        if status in {"deleted", "cancelled"}:
                            self._cancel_task(task_id)
                            return

                producer_task = asyncio.create_task(producer())
                workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
                updater = asyncio.create_task(progress_updater())
                await producer_task
                await work_queue.join()
                await asyncio.gather(*workers)
                finished.set()
                await updater

                snapshot = await asyncio.to_thread(_task_snapshot, task_id)
                if (
                    self.is_task_cancelled(task_id)
                    or not snapshot
                    or snapshot["status"] in {"deleted", "cancelled"}
                ):
                    logger.log("Task was cancelled or deleted during download.")
                    return

                extraction_errors = list(getattr(extractor, "extraction_errors", []))
                final_status = (
                    "completed_with_errors"
                    if state["failed"] or extraction_errors
                    else "completed"
                )
                cancelled_now = self.get_cancelled_chapters(task_id)
                if (
                    final_status == "completed_with_errors"
                    and auto_retries < MAX_AUTO_RETRIES
                ):
                    # Files that already downloaded are skipped on the next
                    # pass, so it only repeats what failed.
                    logger.log(
                        f"Finished with {state['failed']} failed items and "
                        f"{len(extraction_errors)} extraction errors. "
                        f"Retrying automatically (attempt {auto_retries + 1} "
                        f"of {MAX_AUTO_RETRIES})..."
                    )
                    await asyncio.to_thread(
                        _update_task,
                        task_id,
                        status="pending",
                        progress=0.0,
                        details=json.dumps(
                            {
                                "_cancelled": sorted(cancelled_now),
                                "_auto_retries": auto_retries + 1,
                            }
                        ),
                    )
                    return True
                for chapter in cancelled_now:
                    progress_details.pop(chapter, None)
                progress_details["_cancelled"] = sorted(cancelled_now)
                progress_details["_meta"] = {
                    "phase": "completed",
                    "total_time_seconds": int(time.monotonic() - pipeline_started),
                    "extraction_seconds": int(extraction_seconds),
                    "download_seconds": int(time.monotonic() - download_started),
                    "failed_items": state["failed"],
                    "extraction_errors": len(extraction_errors),
                }
                await asyncio.to_thread(
                    _update_task,
                    task_id,
                    status=final_status,
                    progress=100.0,
                    details=json.dumps(progress_details),
                )
                logger.log(
                    f"Task finished with status {final_status}: "
                    f"{state['successful']} successful, {state['failed']} failed."
                )
        except asyncio.CancelledError:
            logger.log("Task cancelled during application shutdown.")
            raise
        except Exception as exc:
            logger.log(f"CRITICAL ERROR: Pipeline crashed: {exc}")
            snapshot = await asyncio.to_thread(_task_snapshot, task_id)
            if snapshot and snapshot["status"] not in {"deleted", "cancelled"}:
                try:
                    details = json.loads(snapshot["details"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    details = {}
                details["_meta"] = {
                    "phase": "error",
                    "message": str(exc),
                    "total_time_seconds": int(time.monotonic() - pipeline_started),
                }
                await asyncio.to_thread(
                    _update_task,
                    task_id,
                    status="error",
                    details=json.dumps(details),
                )
        finally:
            self.task_item_limiters.pop(task_id, None)
            self._forget_task(task_id)
            await logger.close()

    async def _download_item(
        self,
        session,
        task_id,
        task_url,
        output_path,
        index,
        item,
        logger,
        on_progress=None,
    ):
        await self.wait_until_runnable(task_id)
        if self.is_task_cancelled(task_id):
            return "cancelled", 0

        image_url = item if isinstance(item, str) else item.get("url")
        if not image_url or urllib.parse.urlparse(image_url).scheme not in {"http", "https"}:
            logger.log(f"ERROR: Item {index + 1} has an invalid media URL")
            return "failed", 0

        subfolder = "" if isinstance(item, str) else item.get("folder", "")
        folder_name = subfolder or "Media"
        if self.is_chapter_cancelled(task_id, folder_name):
            return "cancelled", 0

        media_type = "" if isinstance(item, str) else item.get("type", "")
        convert_to = "" if isinstance(item, str) else item.get("convert_to", "")
        fallback_url = None if isinstance(item, str) else item.get("fallback_url")
        url_path = urllib.parse.unquote(urllib.parse.urlparse(image_url).path)
        filename = _item_filename(index, item)
        is_hls = media_type == "hls" or url_path.lower().endswith(".m3u8")

        target_directory = resolve_within(output_path, subfolder) if subfolder else Path(output_path)
        file_path = resolve_within(target_directory, filename)
        await asyncio.to_thread(file_path.parent.mkdir, parents=True, exist_ok=True)

        if await asyncio.to_thread(file_path.is_file):
            size = await asyncio.to_thread(file_path.stat)
            if size.st_size > 0:
                logger.log(f"SKIPPED: {filename} already exists")
                return "skipped", size.st_size

        referer = item.get("referer") if isinstance(item, dict) else None
        headers = {"Referer": referer or task_url}
        timeout = get_settings()["request_timeout_seconds"]

        part_path = (
            file_path.with_name(f"{file_path.stem}.part{file_path.suffix}")
            if is_hls
            else file_path.with_name(f"{file_path.name}.part")
        )
        if not is_hls:
            # Resume only bytes written by this invocation. A partial file left
            # by an older run may refer to different content at the same URL.
            await asyncio.to_thread(part_path.unlink, missing_ok=True)

        attempt = 0
        free_reconnects = 0
        while attempt < 3:
            if self.is_task_cancelled(task_id) or self.is_chapter_cancelled(
                task_id, folder_name
            ):
                await asyncio.to_thread(part_path.unlink, missing_ok=True)
                return "cancelled", 0
            try:
                if is_hls:
                    async with self.download_slot(image_url):
                        await asyncio.to_thread(
                            MediaProcessor.download_video,
                            image_url,
                            str(part_path),
                            headers,
                            self.host_limit,
                        )
                    if self.is_task_cancelled(task_id) or self.is_chapter_cancelled(task_id, folder_name):
                        await asyncio.to_thread(part_path.unlink, missing_ok=True)
                        return "cancelled", 0
                    await asyncio.to_thread(os.replace, part_path, file_path)
                    size = (await asyncio.to_thread(file_path.stat)).st_size
                    logger.log(f"SUCCESS: Downloaded {filename} ({size} bytes)")
                    return "success", size

                request_headers = dict(headers)
                resume_from = 0
                # Any partial file here was written by an earlier connection of
                # this call, since a stale one was removed before the loop.
                if await asyncio.to_thread(part_path.is_file):
                    partial_stat = await asyncio.to_thread(part_path.stat)
                    resume_from = partial_stat.st_size
                    if resume_from:
                        request_headers["Range"] = f"bytes={resume_from}-"

                async with self.download_slot(image_url):
                    async with _open_stream(
                        session,
                        "GET",
                        image_url,
                        headers=request_headers,
                        timeout=timeout,
                        allow_redirects=True,
                    ) as response:
                        if response.status_code == 416 and resume_from:
                            await asyncio.to_thread(part_path.unlink, missing_ok=True)
                            raise RuntimeError(
                                f"retry:0:Server rejected resume for {filename}"
                            )
                        if response.status_code in RETRYABLE_STATUSES:
                            retry_after = response.headers.get("retry-after")
                            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                            raise RuntimeError(
                                f"retry:{delay}:HTTP {response.status_code} for {filename}"
                            )
                        if response.status_code not in {200, 206}:
                            await asyncio.to_thread(part_path.unlink, missing_ok=True)
                            logger.log(
                                f"ERROR: HTTP {response.status_code} while downloading {image_url}"
                            )
                            return "failed", 0
                        write_mode = "wb"
                        initial_size = 0
                        if resume_from and response.status_code == 206:
                            content_range = response.headers.get("content-range", "")
                            range_match = re.match(
                                r"bytes\s+(\d+)-\d+/(?:\d+|\*)$", content_range
                            )
                            if not range_match or int(range_match.group(1)) != resume_from:
                                await asyncio.to_thread(part_path.unlink, missing_ok=True)
                                raise RuntimeError(
                                    f"retry:0:Invalid resume response for {filename}"
                                )
                            write_mode = "ab"
                            initial_size = resume_from
                            logger.log(f"RESUMING: {filename} from {resume_from} bytes")
                        content_type = response.headers.get("content-type", "").lower()
                        if content_type.startswith("text/html") or content_type.startswith(
                            "application/json"
                        ):
                            await asyncio.to_thread(part_path.unlink, missing_ok=True)
                            logger.log(
                                f"ERROR: Unexpected {content_type} response for {filename}"
                            )
                            return "failed", 0
                        expected = response.headers.get("content-length")
                        expected_size = (int(expected) if expected and expected.isdigit() and not response.headers.get("content-encoding") else None)
                        response_written = 0
                        cancelled_during_transfer = False
                        expected_total = (
                            initial_size + expected_size if expected_size is not None else None
                        )
                        async with aiofiles.open(part_path, write_mode) as handle:
                            async for chunk in _watched_chunks(
                                response,
                                self.stall_timeout,
                                self.low_speed_window,
                                self.low_speed_rate,
                                interrupted=lambda: self.is_task_paused(task_id)
                                or self.is_task_cancelled(task_id)
                                or self.is_chapter_cancelled(task_id, folder_name),
                            ):
                                if self.is_task_cancelled(task_id) or self.is_chapter_cancelled(task_id, folder_name):
                                    cancelled_during_transfer = True
                                    break
                                if chunk:
                                    await handle.write(chunk)
                                    response_written += len(chunk)
                                    if on_progress:
                                        on_progress(initial_size + response_written, expected_total)
                                if self.is_task_paused(task_id):
                                    raise TransferInterrupted(
                                        f"paused at {initial_size + response_written} bytes"
                                    )
                        if cancelled_during_transfer:
                            await asyncio.to_thread(part_path.unlink, missing_ok=True)
                            return "cancelled", 0
                        if expected_size is not None and response_written != expected_size:
                            raise RuntimeError(
                                f"retry:{2**attempt}:Incomplete {filename}: "
                                f"expected {expected_size}, received {response_written}"
                            )
                        written = initial_size + response_written
                        if written <= 0:
                            raise RuntimeError(f"retry:{2**attempt}:Empty response for {filename}")

                if convert_to:
                    converted_path = file_path.with_name(
                        f"{file_path.stem}.converted.part{file_path.suffix}"
                    )
                    await asyncio.to_thread(converted_path.unlink, missing_ok=True)
                    try:
                        if convert_to != "gif":
                            raise RuntimeError(
                                f"Unsupported conversion target: {convert_to}"
                            )
                        await asyncio.to_thread(
                            MediaProcessor.convert_to_gif,
                            part_path,
                            converted_path,
                        )
                        if self.is_task_cancelled(
                            task_id
                        ) or self.is_chapter_cancelled(task_id, folder_name):
                            await asyncio.to_thread(part_path.unlink, missing_ok=True)
                            await asyncio.to_thread(
                                converted_path.unlink, missing_ok=True
                            )
                            return "cancelled", 0
                        await asyncio.to_thread(part_path.unlink, missing_ok=True)
                        await asyncio.to_thread(
                            os.replace, converted_path, file_path
                        )
                        converted_size = (
                            await asyncio.to_thread(file_path.stat)
                        ).st_size
                        logger.log(
                            f"SUCCESS: Downloaded and converted {filename} "
                            f"({converted_size} bytes)"
                        )
                        return "success", converted_size
                    except Exception as conversion_error:
                        await asyncio.to_thread(part_path.unlink, missing_ok=True)
                        await asyncio.to_thread(
                            converted_path.unlink, missing_ok=True
                        )
                        if fallback_url:
                            logger.log(
                                f"WARN: Could not convert {filename}: "
                                f"{conversion_error}. Downloading original GIF..."
                            )
                            fallback_item = dict(item)
                            fallback_item["url"] = fallback_url
                            fallback_item.pop("convert_to", None)
                            fallback_item.pop("fallback_url", None)
                            return await self._download_item(
                                session,
                                task_id,
                                task_url,
                                output_path,
                                index,
                                fallback_item,
                                logger,
                                on_progress=on_progress,
                            )
                        raise RuntimeError(
                            f"Media conversion failed: {conversion_error}"
                        ) from conversion_error

                await asyncio.to_thread(os.replace, part_path, file_path)
                logger.log(f"SUCCESS: Downloaded {filename} ({written} bytes)")
                return "success", written
            except TransferInterrupted as exc:
                # Drop the connection so a paused task stops using bandwidth,
                # then pick the partial file up again with a range request. A
                # cancellation is handled by the check at the top of the loop.
                if not (
                    self.is_task_cancelled(task_id)
                    or self.is_chapter_cancelled(task_id, folder_name)
                ):
                    logger.log(f"PAUSED: {filename} {exc}")
                    await self.wait_until_runnable(task_id)
                continue
            except StalledTransfer as exc:
                if exc.received and free_reconnects < MAX_FREE_RECONNECTS:
                    free_reconnects += 1
                    logger.log(
                        f"WARN: {filename} stalled ({exc}). Reconnecting to resume "
                        f"({free_reconnects}/{MAX_FREE_RECONNECTS})..."
                    )
                    continue
                attempt = await self._handle_failed_attempt(
                    exc, attempt, is_hls, part_path, filename, image_url, logger
                )
            except Exception as exc:
                attempt = await self._handle_failed_attempt(
                    exc, attempt, is_hls, part_path, filename, image_url, logger
                )
        return "failed", 0

    async def _handle_failed_attempt(
        self, exc, attempt, is_hls, part_path, filename, image_url, logger
    ):
        """Log a failed attempt, back off before the next one, and return its number."""
        if (
            is_hls or attempt >= 2
        ) and await asyncio.to_thread(part_path.exists):
            await asyncio.to_thread(part_path.unlink, missing_ok=True)
        message = str(exc)
        delay = 2**attempt
        if message.startswith("retry:"):
            _, raw_delay, message = message.split(":", 2)
            try:
                delay = min(float(raw_delay), 30)
            except ValueError:
                pass
        if attempt < 2:
            logger.log(
                f"WARN: {message}. Retrying {filename} in {delay:.1f}s..."
            )
            await asyncio.sleep(delay)
        else:
            logger.log(f"ERROR: Failed to download {image_url}: {message}")
        return attempt + 1
