from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import uuid

from backend.database.models import SessionLocal, DownloadTask
from backend.core.downloader import DownloadManager

router = APIRouter()

@router.get("/proxy")
def proxy_image(url: str, referer: Optional[str] = None):
    from curl_cffi import requests as cffi_requests
    from fastapi.responses import Response
    headers = {}
    if referer:
        headers['Referer'] = referer
    try:
        resp = cffi_requests.get(url, headers=headers, impersonate="chrome110", timeout=10)
        if resp.status_code == 200:
            return Response(content=resp.content, media_type=resp.headers.get("content-type", "image/jpeg"))
        else:
            raise HTTPException(status_code=resp.status_code, detail="Failed to fetch image")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class DownloadRequest(BaseModel):
    url: str

class DownloadResponse(BaseModel):
    id: str
    url: str
    title: Optional[str]
    thumbnail: Optional[str] = None
    status: str
    progress: float
    details: Optional[str] = "{}"
    created_at: datetime

@router.post("/downloads", response_model=DownloadResponse)
def add_download(req: DownloadRequest, db: Session = Depends(get_db)):
    task_id = str(uuid.uuid4())[:8]
    new_task = DownloadTask(
        id=task_id,
        url=req.url,
        title="Fetching metadata...",
        status="pending",
        progress=0.0
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)
    
    # Queue the task to be processed asynchronously
    DownloadManager.get_instance().enqueue(new_task.id, new_task.url)
    
    return new_task

@router.get("/downloads", response_model=List[DownloadResponse])
def get_downloads(db: Session = Depends(get_db)):
    tasks = db.query(DownloadTask).filter(DownloadTask.status != "deleted").order_by(DownloadTask.created_at.desc()).all()
    return tasks

import shutil
import os

@router.post("/downloads/{task_id}/pause")
def pause_task(task_id: str, db: Session = Depends(get_db)):
    task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
    if task and task.status == "downloading":
        task.status = "paused"
        db.commit()
    return {"status": "ok"}

@router.post("/downloads/{task_id}/resume")
def resume_task(task_id: str, db: Session = Depends(get_db)):
    task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
    if task and task.status in ["paused", "failed"]:
        task.status = "downloading"
        db.commit()
    return {"status": "ok"}

@router.post("/downloads/{task_id}/restart")
def restart_task(task_id: str, db: Session = Depends(get_db)):
    task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
    if task and task.status in ["completed", "error", "failed"]:
        task.status = "pending"
        task.progress = 0.0
        
        import json
        try:
            details = json.loads(task.details)
            cancelled = details.get("_cancelled", [])
        except Exception:
            cancelled = []
            
        task.details = json.dumps({"_cancelled": cancelled})
        db.commit()
        # Requeue the task
        DownloadManager.get_instance().enqueue(task.id, task.url)
    return {"status": "ok"}

@router.post("/downloads/pause-all")
def pause_all_tasks(db: Session = Depends(get_db)):
    tasks = db.query(DownloadTask).filter(DownloadTask.status == "downloading").all()
    for task in tasks:
        task.status = "paused"
    db.commit()
    return {"status": "ok"}

@router.post("/downloads/resume-all")
def resume_all_tasks(db: Session = Depends(get_db)):
    # Only resume tasks that are paused or failed. Do not resume "done" tasks.
    tasks = db.query(DownloadTask).filter(DownloadTask.status.in_(["paused", "failed"])).all()
    for task in tasks:
        task.status = "downloading"
    db.commit()
    return {"status": "ok"}

@router.post("/downloads/clear-all")
def clear_all_tasks(db: Session = Depends(get_db)):
    import os
    import shutil
    logs_dir = os.path.join(os.getcwd(), "task_logs")
    
    # Aggressively delete all log files in the task_logs directory
    if os.path.exists(logs_dir):
        for filename in os.listdir(logs_dir):
            if filename.endswith(".log"):
                file_path = os.path.join(logs_dir, filename)
                try:
                    os.remove(file_path)
                except:
                    pass

    # Soft delete all tasks
    tasks = db.query(DownloadTask).filter(DownloadTask.status != "deleted").all()
    for task in tasks:
        task.status = "deleted"
    db.commit()
    
    manager = DownloadManager.get_instance()
    if hasattr(manager, 'cancelled_chapters'):
        manager.cancelled_chapters.clear()
        
    return {"status": "ok"}

@router.get("/downloads/{task_id}/logs")
def get_task_logs(task_id: str):
    import os
    logs_dir = os.path.join(os.getcwd(), "task_logs")
    log_path = os.path.join(logs_dir, f"{task_id}.log")
    
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                return {"logs": f.read()}
        except Exception as e:
            return {"logs": f"Error reading logs: {e}"}
    return {"logs": "No logs found for this task yet."}

@router.post("/downloads/{task_id}/open")
def open_task_folder(task_id: str, db: Session = Depends(get_db)):
    task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
    if task and task.title:
        import re
        from backend.core.config import get_settings
        settings = get_settings()
        base_dir = settings.get("download_dir", os.path.join(os.getcwd(), "downloads"))
        safe_title = re.sub(r'[\\/*?:"<>|]', "", task.title).strip()
        download_dir = os.path.join(base_dir, safe_title)
        
        # If the plugin used flat_directory = True, the folder won't exist. Fall back to base_dir.
        target_dir = download_dir if os.path.exists(download_dir) else base_dir
        
        if os.path.exists(target_dir):
            if os.name == 'nt':
                os.startfile(target_dir)
            else:
                import subprocess
                subprocess.Popen(['xdg-open', target_dir])
                
    return {"status": "ok"}

@router.delete("/downloads/{task_id}")
def delete_task(task_id: str, delete_files: bool = False, db: Session = Depends(get_db)):
    task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
    if task:
        task.status = "deleted"
        
        # Clean up log file
        import os
        logs_dir = os.path.join(os.getcwd(), "task_logs")
        log_path = os.path.join(logs_dir, f"{task.id}.log")
        if os.path.exists(log_path):
            try:
                os.remove(log_path)
            except:
                pass
        
        # If files need to be deleted
        if delete_files and task.title:
            import re
            from backend.core.config import get_settings
            settings = get_settings()
            base_dir = settings.get("download_dir", os.path.join(os.getcwd(), "downloads"))
            safe_title = re.sub(r'[\\/*?:"<>|]', "", task.title).strip()
            download_dir = os.path.join(base_dir, safe_title)
            
            if os.path.exists(download_dir):
                shutil.rmtree(download_dir, ignore_errors=True)
                
        db.commit()
        
    manager = DownloadManager.get_instance()
    if hasattr(manager, 'cancelled_chapters') and task_id in manager.cancelled_chapters:
        del manager.cancelled_chapters[task_id]
        
    return {"status": "ok"}

@router.delete("/downloads/{task_id}/items/{chapter_name:path}")
def delete_task_item(task_id: str, chapter_name: str, db: Session = Depends(get_db)):
    task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    DownloadManager.get_instance().cancel_chapter(task_id, chapter_name)
    
    if task.details:
        import json
        try:
            details = json.loads(task.details)
            if chapter_name in details:
                del details[chapter_name]
                
            if "_cancelled" not in details:
                details["_cancelled"] = []
            if chapter_name not in details["_cancelled"]:
                details["_cancelled"].append(chapter_name)
                
            task.details = json.dumps(details)
            db.commit()
        except Exception:
            pass

    import os, shutil, re
    from backend.core.config import get_settings
    settings = get_settings()
    base_dir = settings.get("download_dir", os.path.join(os.getcwd(), "downloads"))
    safe_title = re.sub(r'[\\/*?:"<>|]', "", task.title).strip() if task.title else ""
    
    if safe_title:
        if chapter_name == "Media":
            target_dir_explicit = os.path.join(base_dir, safe_title, "Media")
            if os.path.exists(target_dir_explicit) and os.path.isdir(target_dir_explicit):
                shutil.rmtree(target_dir_explicit, ignore_errors=True)
                
            target_dir_implicit = os.path.join(base_dir, safe_title)
            if os.path.exists(target_dir_implicit):
                for item in os.listdir(target_dir_implicit):
                    item_path = os.path.join(target_dir_implicit, item)
                    if os.path.isfile(item_path):
                        try:
                            os.remove(item_path)
                        except:
                            pass
        else:
            target_dir = os.path.join(base_dir, safe_title, chapter_name)
            if os.path.exists(target_dir) and os.path.isdir(target_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
                
    return {"status": "ok"}

@router.get("/plugins")
def get_plugins():
    manager = DownloadManager.get_instance()
    plugins = []
    if manager and manager.plugin_manager:
        for plugin in manager.plugin_manager.registry:
            plugins.append({
                "name": plugin.__name__,
                "urls": plugin.URLS
            })
    return {"plugins": plugins}

from backend.core.config import get_settings, save_settings
from pydantic import BaseModel

class SettingsUpdate(BaseModel):
    download_dir: str
    dark_mode: bool
    use_playwright: bool
    max_concurrent_tasks: Optional[int] = 3
    max_concurrent_items: Optional[int] = 5
    ui_scale: Optional[float] = 1.0

@router.get("/settings")
def api_get_settings():
    return get_settings()

@router.post("/settings")
def api_update_settings(req: SettingsUpdate):
    settings = {
        "download_dir": req.download_dir,
        "dark_mode": req.dark_mode,
        "use_playwright": req.use_playwright,
        "max_concurrent_tasks": req.max_concurrent_tasks,
        "max_concurrent_items": req.max_concurrent_items,
        "ui_scale": req.ui_scale
    }
    save_settings(settings)
    return {"status": "ok"}
