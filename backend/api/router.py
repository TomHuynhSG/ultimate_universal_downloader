import asyncio
import datetime
import ipaddress
import json
import os
import re
import shutil
import socket
import time
import urllib.parse
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from curl_cffi.requests import AsyncSession
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import case
from sqlalchemy.orm import Session

from backend.core.config import get_settings, save_settings
from backend.core.downloader import DownloadManager
from backend.core.paths import PROJECT_ROOT, resolve_within, sanitize_component, task_output_path
from backend.database.models import DownloadTask, SessionLocal


router = APIRouter()
ACTIVE_STATUSES = {"pending", "extracting", "downloading", "paused"}
FINISHED_STATUSES = {"completed", "completed_with_errors", "error", "failed"}
LOG_ID_PATTERN = re.compile(r"[A-Fa-f0-9-]{8,36}")
PROXY_CACHE_MAX_ITEMS = 128
PROXY_CACHE_TTL_SECONDS = 3600
PROXY_MAX_BYTES = 20 * 1024 * 1024
_proxy_cache = OrderedDict()
_proxy_cache_lock = asyncio.Lock()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _parse_details(raw):
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _serialize_task(task):
    return {
        "id": task.id,
        "url": task.url,
        "title": task.title,
        "thumbnail": task.thumbnail,
        "status": task.status,
        "progress": task.progress,
        "details": _parse_details(task.details),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _safe_output_path(task):
    settings = get_settings()
    base = Path(settings["download_dir"]).expanduser().resolve(strict=False)
    if task.output_path:
        candidate = Path(task.output_path).expanduser().resolve(strict=False)
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Stored output path is unsafe") from exc
        return base, candidate
    return base, task_output_path(str(base), task.title or "Unknown Album")


def _open_directory(path):
    if os.name == "nt":
        os.startfile(str(path))
        return

    import subprocess
    import sys

    command = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([command, str(path)])


def _remove_tree_with_retries(path, *, attempts=5, initial_delay=0.05):
    last_error = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
        except OSError as exc:
            last_error = exc
        if not path.exists():
            return
        if attempt + 1 < attempts:
            time.sleep(initial_delay * (2**attempt))
    raise last_error or OSError(f"Could not remove {path}")


async def _validate_proxy_url(raw_url):
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise HTTPException(status_code=400, detail="Only public HTTP(S) image URLs are allowed")
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise HTTPException(status_code=400, detail="Local proxy targets are not allowed")
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
    except socket.gaierror as exc:
        raise HTTPException(status_code=502, detail="Proxy target could not be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise HTTPException(status_code=400, detail="Private proxy targets are not allowed")
    return parsed.geturl()


async def _fetch_proxy_image(url, referer):
    headers = {"Referer": referer} if referer else {}
    current = url
    async with AsyncSession(impersonate="chrome", timeout=15) as session:
        for _ in range(4):
            current = await _validate_proxy_url(current)
            async with session.stream(
                "GET", current, headers=headers, allow_redirects=False, timeout=15
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise HTTPException(status_code=502, detail="Invalid proxy redirect")
                    current = urllib.parse.urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Remote image returned HTTP {response.status_code}",
                    )
                content_type = response.headers.get("content-type", "image/jpeg")
                if not content_type.lower().startswith("image/"):
                    raise HTTPException(status_code=415, detail="Proxy target is not an image")
                chunks = []
                total = 0
                async for chunk in response.aiter_content(chunk_size=128 * 1024):
                    total += len(chunk)
                    if total > PROXY_MAX_BYTES:
                        raise HTTPException(status_code=413, detail="Remote image is too large")
                    chunks.append(chunk)
                return b"".join(chunks), content_type
    raise HTTPException(status_code=502, detail="Too many proxy redirects")


@router.get("/proxy")
async def proxy_image(url: str, referer: Optional[str] = None):
    cache_key = (url, referer or "")
    now = time.monotonic()
    async with _proxy_cache_lock:
        cached = _proxy_cache.get(cache_key)
        if cached and now - cached[0] < PROXY_CACHE_TTL_SECONDS:
            _proxy_cache.move_to_end(cache_key)
            return Response(
                content=cached[1],
                media_type=cached[2],
                headers={"Cache-Control": "public, max-age=3600"},
            )
        if cached:
            _proxy_cache.pop(cache_key, None)

    content, content_type = await _fetch_proxy_image(url, referer)
    async with _proxy_cache_lock:
        _proxy_cache[cache_key] = (now, content, content_type)
        _proxy_cache.move_to_end(cache_key)
        while len(_proxy_cache) > PROXY_CACHE_MAX_ITEMS:
            _proxy_cache.popitem(last=False)
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


class DownloadRequest(BaseModel):
    url: HttpUrl


class DownloadResponse(BaseModel):
    id: str
    url: str
    title: Optional[str]
    thumbnail: Optional[str] = None
    status: str
    progress: float
    details: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime.datetime
    updated_at: Optional[datetime.datetime] = None


@router.post("/downloads", response_model=DownloadResponse)
def add_download(req: DownloadRequest, db: Session = Depends(get_db)):
    task_id = str(uuid.uuid4())
    new_task = DownloadTask(
        id=task_id,
        url=str(req.url),
        title="Fetching metadata...",
        status="pending",
        progress=0.0,
        details="{}",
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)
    DownloadManager.get_instance().enqueue(new_task.id, new_task.url)
    return _serialize_task(new_task)


@router.get("/downloads", response_model=List[DownloadResponse])
def get_downloads(
    limit: int = Query(250, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    active_only: bool = False,
    db: Session = Depends(get_db),
):
    query = db.query(DownloadTask).filter(DownloadTask.status != "deleted")
    if active_only:
        query = query.filter(DownloadTask.status.in_(ACTIVE_STATUSES))
    active_rank = case((DownloadTask.status.in_(ACTIVE_STATUSES), 0), else_=1)
    tasks = (
        query.order_by(active_rank, DownloadTask.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [_serialize_task(task) for task in tasks]


@router.post("/downloads/{task_id}/pause")
def pause_task(task_id: str, db: Session = Depends(get_db)):
    task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
    if task and task.status == "downloading":
        task.status = "paused"
        db.commit()
        DownloadManager.get_instance().set_task_paused(task_id, True)
    return {"status": "ok"}


@router.post("/downloads/{task_id}/resume")
def resume_task(task_id: str, db: Session = Depends(get_db)):
    task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
    if task and task.status == "paused":
        manager = DownloadManager.get_instance()
        if task_id in manager.task_events:
            task.status = "downloading"
            manager.set_task_paused(task_id, False)
        else:
            task.status = "pending"
            manager.enqueue(task.id, task.url)
        db.commit()
    return {"status": "ok"}


@router.post("/downloads/{task_id}/restart")
def restart_task(task_id: str, db: Session = Depends(get_db)):
    task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
    if task and task.status in FINISHED_STATUSES:
        details = _parse_details(task.details)
        cancelled = details.get("_cancelled", [])
        task.status = "pending"
        task.progress = 0.0
        task.details = json.dumps({"_cancelled": cancelled})
        db.commit()
        DownloadManager.get_instance().enqueue(task.id, task.url)
    return {"status": "ok"}


@router.post("/downloads/pause-all")
def pause_all_tasks(db: Session = Depends(get_db)):
    tasks = db.query(DownloadTask).filter(DownloadTask.status == "downloading").all()
    manager = DownloadManager.get_instance()
    for task in tasks:
        task.status = "paused"
        manager.set_task_paused(task.id, True)
    db.commit()
    return {"status": "ok"}


@router.post("/downloads/resume-all")
def resume_all_tasks(db: Session = Depends(get_db)):
    tasks = db.query(DownloadTask).filter(DownloadTask.status == "paused").all()
    manager = DownloadManager.get_instance()
    for task in tasks:
        if task.id in manager.task_events:
            task.status = "downloading"
            manager.set_task_paused(task.id, False)
        else:
            task.status = "pending"
            manager.enqueue(task.id, task.url)
    db.commit()
    return {"status": "ok"}


@router.post("/downloads/clear-all")
def clear_all_tasks(db: Session = Depends(get_db)):
    manager = DownloadManager.get_instance()
    tasks = db.query(DownloadTask.id).all()
    for (task_id,) in tasks:
        manager.cancel_task(task_id)
    db.query(DownloadTask).delete(synchronize_session=False)
    db.commit()
    manager.clear_controls()

    logs_dir = PROJECT_ROOT / "task_logs"
    if logs_dir.exists():
        for log_path in logs_dir.glob("*.log"):
            try:
                log_path.unlink()
            except OSError:
                pass
    return {"status": "ok"}


@router.get("/downloads/{task_id}/logs")
def get_task_logs(task_id: str):
    if not LOG_ID_PATTERN.fullmatch(task_id):
        raise HTTPException(status_code=400, detail="Invalid task ID")
    log_path = PROJECT_ROOT / "task_logs" / f"{task_id}.log"
    if not log_path.exists():
        return {"logs": "No logs found for this task yet."}
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 1024 * 1024))
            return {"logs": handle.read().decode("utf-8", errors="replace")}
    except OSError as exc:
        return {"logs": f"Error reading logs: {exc}"}


@router.post("/downloads/{task_id}/open")
def open_task_folder(task_id: str, db: Session = Depends(get_db)):
    task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _base, output_path = _safe_output_path(task)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Output folder does not exist")
    _open_directory(output_path)
    return {"status": "ok"}


@router.delete("/downloads/{task_id}")
async def delete_task(
    task_id: str,
    delete_files: bool = False,
    db: Session = Depends(get_db),
):
    task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
    if not task:
        return {"status": "ok"}
    manager = DownloadManager.get_instance()
    base, output_path = _safe_output_path(task)
    if delete_files and output_path == base:
        raise HTTPException(
            status_code=409,
            detail="This task used the shared download directory; automatic directory deletion is unsafe.",
        )

    stopped = await manager.cancel_task_and_wait(task_id, timeout=30)
    if not stopped:
        raise HTTPException(
            status_code=409,
            detail=(
                "The task is still closing active files. "
                "Wait a moment and try deleting it again."
            ),
        )

    if delete_files and output_path.exists():
        try:
            await asyncio.to_thread(_remove_tree_with_retries, output_path)
        except OSError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"The task stopped, but its files could not be removed: {exc}",
            ) from exc

    log_path = PROJECT_ROOT / "task_logs" / f"{task.id}.log"
    try:
        log_path.unlink(missing_ok=True)
    except OSError:
        pass
    db.delete(task)
    db.commit()
    return {"status": "ok"}


@router.delete("/downloads/{task_id}/items/{chapter_name:path}")
def delete_task_item(task_id: str, chapter_name: str, db: Session = Depends(get_db)):
    task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    safe_chapter = sanitize_component(chapter_name, fallback="Media")
    if safe_chapter != chapter_name:
        raise HTTPException(status_code=400, detail="Invalid chapter path")

    manager = DownloadManager.get_instance()
    manager.cancel_chapter(task_id, chapter_name)
    details = _parse_details(task.details)
    details.pop(chapter_name, None)
    cancelled = details.setdefault("_cancelled", [])
    if chapter_name not in cancelled:
        cancelled.append(chapter_name)
    task.details = json.dumps(details)
    db.commit()

    _base, output_path = _safe_output_path(task)
    if output_path.exists():
        if chapter_name == "Media":
            for child in output_path.iterdir():
                if child.is_file():
                    child.unlink()
        else:
            target = resolve_within(output_path, chapter_name)
            if target.is_dir():
                shutil.rmtree(target)
    return {"status": "ok"}


@router.get("/plugins")
def get_plugins():
    manager = DownloadManager.get_instance()
    return {
        "plugins": [
            {"name": plugin.__name__, "urls": plugin.URLS}
            for plugin in manager.plugin_manager.registry
        ]
    }


class SettingsUpdate(BaseModel):
    download_dir: str = Field(min_length=1)
    dark_mode: bool
    use_playwright: bool
    max_concurrent_tasks: int = Field(default=3, ge=1, le=20)
    max_concurrent_items: int = Field(default=5, ge=1, le=50)
    max_global_items: int = Field(default=15, ge=1, le=200)
    max_concurrent_per_host: int = Field(default=6, ge=1, le=50)
    max_extract_concurrency: int = Field(default=8, ge=1, le=50)
    request_timeout_seconds: int = Field(default=30, ge=5, le=300)
    ui_scale: float = Field(default=1.0, ge=0.5, le=1.5)


class OpenDownloadDirectoryRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)


@router.post("/settings/open-download-directory")
def open_download_directory(req: OpenDownloadDirectoryRequest):
    try:
        directory = (
            Path(os.path.expandvars(req.directory))
            .expanduser()
            .resolve(strict=False)
        )
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise OSError("Path is not a directory")
        _open_directory(directory)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not open download directory: {exc}",
        ) from exc
    return {"status": "ok", "directory": str(directory)}


@router.get("/settings")
def api_get_settings():
    return get_settings()


@router.post("/settings")
async def api_update_settings(req: SettingsUpdate):
    settings = save_settings(req.model_dump())
    runtime = await DownloadManager.get_instance().apply_runtime_settings(settings)
    return {"status": "ok", "settings": settings, "runtime": runtime}
