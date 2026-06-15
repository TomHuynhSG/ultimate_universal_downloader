import asyncio
from curl_cffi.requests import AsyncSession
from backend.plugins.manager import PluginManager
from backend.database.models import SessionLocal, DownloadTask
import json
import os
import re
import datetime

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

    def cancel_chapter(self, task_id, chapter_name):
        if task_id not in self.cancelled_chapters:
            self.cancelled_chapters[task_id] = set()
        self.cancelled_chapters[task_id].add(chapter_name)

    async def _dispatcher(self):
        print("Download dispatcher started.")
        running_tasks = set()
        while True:
            task_id, url = await self.queue.get()
            
            from backend.core.config import get_settings
            settings = get_settings()
            max_tasks = settings.get("max_concurrent_tasks", 3)
            
            # Clean up finished tasks
            running_tasks = {t for t in running_tasks if not t.done()}
            
            while len(running_tasks) >= max_tasks:
                done, pending = await asyncio.wait(running_tasks, return_when=asyncio.FIRST_COMPLETED)
                running_tasks = pending
                settings = get_settings()
                max_tasks = settings.get("max_concurrent_tasks", 3)
                
            task = asyncio.create_task(self._process_single_task(task_id, url))
            running_tasks.add(task)

    async def _process_single_task(self, task_id, url):
        logs_dir = os.path.join(os.getcwd(), "task_logs")
        os.makedirs(logs_dir, exist_ok=True)
        log_path = os.path.join(logs_dir, f"{task_id}.log")
        
        def log_to_file(msg):
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[Task {task_id}] {msg}")
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"[{timestamp}] {msg}\n")
            except Exception:
                pass
                
        log_to_file(f"Initialized processing for URL: {url}")
        
        db = SessionLocal()
        async with AsyncSession(impersonate="chrome110") as session:
            try:
                task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
                if task:
                    task.status = "extracting"
                    db.commit()

                    log_to_file(f"Starting plugin extraction...")
                    extractor = self.plugin_manager.get_extractor(url)
                    if extractor:
                        log_to_file(f"Matched plugin: {extractor.__class__.__name__}")
                        extracted_urls = await extractor.extract(session)
                        task.status = "downloading"
                        task.title = extractor.title
                        task.thumbnail = getattr(extractor, "thumbnail", None)
                        db.commit()
                        
                        log_to_file(f"Extraction finished. Title: {task.title}")
                        
                        import json
                        try:
                            existing_details = json.loads(task.details)
                            db_cancelled = existing_details.get("_cancelled", [])
                        except Exception:
                            db_cancelled = []
                            
                        manager = DownloadManager.get_instance()
                        if task.id not in manager.cancelled_chapters:
                            manager.cancelled_chapters[task.id] = set()
                        for c in db_cancelled:
                            manager.cancelled_chapters[task.id].add(c)
                            
                        progress_details = {"_cancelled": list(manager.cancelled_chapters[task.id])}
                        for img_item in extracted_urls:
                            folder = "" if isinstance(img_item, str) else img_item.get("folder", "")
                            folder_name = folder if folder else "Media"
                            if folder_name in manager.cancelled_chapters[task.id]:
                                continue
                            if folder_name not in progress_details:
                                progress_details[folder_name] = {"total": 0, "done": 0, "size_bytes": 0}
                            progress_details[folder_name]["total"] += 1
                            
                        task.details = json.dumps(progress_details)
                        db.commit()
                        
                        total = len(extracted_urls)
                        if total == 0:
                            log_to_file("ERROR: Plugin successfully ran but returned 0 items. Marking as failed.")
                            task.status = "error"
                            task.title = task.title if getattr(task, "title", None) else "No media found"
                            task.progress = 0
                            task.details = "{}"
                            db.commit()
                            return
                            
                        from backend.core.config import get_settings
                        settings = get_settings()
                        base_dir = settings.get("download_dir", os.path.join(os.getcwd(), "downloads"))
                        max_items = settings.get("max_concurrent_items", 5)
                        sem = asyncio.Semaphore(max_items)
                        
                        if getattr(extractor, "flat_directory", False):
                            download_dir = base_dir
                        else:
                            safe_title = re.sub(r'[\\/*?:"<>|]', "", extractor.title).strip()
                            download_dir = os.path.join(base_dir, safe_title)
                        
                        os.makedirs(download_dir, exist_ok=True)
                        
                        log_to_file(f"Extracted {total} files. Target directory: {download_dir}")
                        log_to_file(f"Starting parallel download with {max_items} concurrency...")
                        
                        import time
                        start_time = time.time()
                        
                        completed_items = 0
                        db_lock = asyncio.Lock()
                        
                        async def download_item(i, img_item):
                            nonlocal completed_items
                            async with sem:
                                # Wait block for pausing
                                while True:
                                    async with db_lock:
                                        db.refresh(task)
                                        status = task.status
                                    if status == "paused":
                                        await asyncio.sleep(1)
                                    else:
                                        break
                                        
                                async with db_lock:
                                    if task.status in ["cancelled", "deleted"]:
                                        return False
                                
                                img_url = img_item if isinstance(img_item, str) else img_item.get("url")
                                subfolder = "" if isinstance(img_item, str) else img_item.get("folder", "")
                                folder_name = subfolder if subfolder else "Media"
                                
                                manager = DownloadManager.get_instance()
                                if task.id in manager.cancelled_chapters and folder_name in manager.cancelled_chapters[task.id]:
                                    async with db_lock:
                                        if folder_name in progress_details:
                                            del progress_details[folder_name]
                                        completed_items += 1
                                    return True
                                
                                try:
                                    filename = f"{i+1:03d}.jpg"
                                    if isinstance(img_item, dict) and img_item.get("filename"):
                                        filename = img_item.get("filename")
                                    else:
                                        url_no_query = img_url.split("?")[0] if "?" in img_url else img_url
                                        if url_no_query.split("/")[-1].endswith(('.jpg', '.png', '.jpeg', '.webp', '.avif', '.mp4', '.gif')):
                                            filename = url_no_query.split("/")[-1]
                                            
                                    target_dir = os.path.join(download_dir, subfolder)
                                    file_path = os.path.join(target_dir, filename)
                                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                                    
                                    # Skip if already downloaded
                                    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                                        file_size = os.path.getsize(file_path)
                                        log_to_file(f"SKIPPED: {filename} already exists")
                                        async with db_lock:
                                            folder_name = subfolder if subfolder else "Media"
                                            progress_details[folder_name]["done"] += 1
                                            progress_details[folder_name]["size_bytes"] += file_size
                                        async with db_lock:
                                            completed_items += 1
                                        return True
                                
                                    item_referer = img_item.get("referer") if isinstance(img_item, dict) else None
                                    headers = {"Referer": item_referer if item_referer else url}
                                    
                                    max_retries = 3
                                    for attempt in range(max_retries):
                                        try:
                                            img_resp = await session.get(img_url, headers=headers)
                                            if img_resp.status_code == 200:
                                                file_size = len(img_resp.content)
                                                with open(file_path, "wb") as f:
                                                    f.write(img_resp.content)
                                                    
                                                async with db_lock:
                                                    folder_name = subfolder if subfolder else "Media"
                                                    progress_details[folder_name]["done"] += 1
                                                    progress_details[folder_name]["size_bytes"] += file_size
                                                    
                                                log_to_file(f"SUCCESS: Downloaded {filename} ({file_size} bytes)")
                                                break
                                            elif img_resp.status_code in [503, 429]:
                                                if attempt < max_retries - 1:
                                                    log_to_file(f"WARN: HTTP {img_resp.status_code} for {filename}. Retrying in {2 * (attempt + 1)}s...")
                                                    await asyncio.sleep(2 * (attempt + 1))
                                                    continue
                                                else:
                                                    log_to_file(f"ERROR: HTTP {img_resp.status_code} while downloading {img_url} after retries")
                                                    break
                                            else:
                                                log_to_file(f"ERROR: HTTP {img_resp.status_code} while downloading {img_url}")
                                                break
                                        except Exception as img_e:
                                            if attempt < max_retries - 1:
                                                log_to_file(f"WARN: Exception for {filename}: {img_e}. Retrying in {2 * (attempt + 1)}s...")
                                                await asyncio.sleep(2 * (attempt + 1))
                                            else:
                                                log_to_file(f"ERROR: Exception downloading {img_url}: {img_e}")
                                                break
                                except Exception as general_e:
                                    log_to_file(f"ERROR: General exception for item {i}: {general_e}")
                                    
                                async with db_lock:
                                    completed_items += 1
                                return True
                                
                        async def progress_updater():
                            while completed_items < total:
                                async with db_lock:
                                    db.refresh(task)
                                    if task.status in ["cancelled", "deleted"]:
                                        break
                                        
                                    manager = DownloadManager.get_instance()
                                    if task.id in manager.cancelled_chapters:
                                        for cancelled_chapter in list(manager.cancelled_chapters[task.id]):
                                            if cancelled_chapter in progress_details:
                                                del progress_details[cancelled_chapter]
                                        progress_details["_cancelled"] = list(manager.cancelled_chapters[task.id])
                                        
                                    elapsed = time.time() - start_time
                                    items_per_sec = completed_items / elapsed if elapsed > 0 else 0
                                    remaining_items = total - completed_items
                                    eta_seconds = remaining_items / items_per_sec if items_per_sec > 0 else 0
                                    
                                    progress_details["_meta"] = {
                                        "eta_seconds": int(eta_seconds),
                                        "elapsed_seconds": int(elapsed)
                                    }
                                                
                                    task.progress = float(int((completed_items / total) * 100))
                                    task.details = json.dumps(progress_details)
                                    db.commit()
                                await asyncio.sleep(1)
                                
                        updater_task = asyncio.create_task(progress_updater())
                        
                        tasks_to_run = [download_item(i, item) for i, item in enumerate(extracted_urls)]
                        await asyncio.gather(*tasks_to_run)
                        
                        # Await the updater loop to stop
                        await updater_task
                        
                        async with db_lock:
                            db.refresh(task)
                            if task.status not in ["cancelled", "deleted"]:
                                task.status = "completed"
                                task.progress = 100.0
                                
                                manager = DownloadManager.get_instance()
                                if task.id in manager.cancelled_chapters:
                                    for cancelled_chapter in list(manager.cancelled_chapters[task.id]):
                                        if cancelled_chapter in progress_details:
                                            del progress_details[cancelled_chapter]
                                    progress_details["_cancelled"] = list(manager.cancelled_chapters[task.id])
                                    
                                elapsed = time.time() - start_time
                                progress_details["_meta"] = {
                                    "total_time_seconds": int(elapsed)
                                }
                                            
                                task.details = json.dumps(progress_details)
                                db.commit()
                                log_to_file(f"Task successfully completed: {task.title}")
                            else:
                                log_to_file(f"WARN: Task was cancelled or deleted mid-download.")
                    else:
                        log_to_file(f"ERROR: No supported plugin found for {url}")
                        task.status = "error"
                        task.title = "No plugin found"
                        db.commit()
            except Exception as e:
                log_to_file(f"CRITICAL ERROR: Pipeline crashed: {e}")
                try:
                    task = db.query(DownloadTask).filter(DownloadTask.id == task_id).first()
                    if task:
                        task.status = "error"
                        db.commit()
                except Exception:
                    pass
            finally:
                db.close()
                self.queue.task_done()

    def enqueue(self, task_id, url):
        self.queue.put_nowait((task_id, url))
