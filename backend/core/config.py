import json
import os
import threading
from pathlib import Path

from backend.core.paths import PROJECT_ROOT


SETTINGS_FILE = PROJECT_ROOT / "settings.json"
DEFAULT_SETTINGS = {
    "download_dir": str(PROJECT_ROOT / "downloads"),
    "dark_mode": True,
    "use_playwright": True,
    "max_concurrent_tasks": 3,
    "max_concurrent_items": 5,
    "max_global_items": 15,
    "max_concurrent_per_host": 6,
    "max_extract_concurrency": 8,
    "request_timeout_seconds": 30,
    "ui_scale": 1.0,
}
_settings_lock = threading.RLock()


def _validated(settings):
    result = DEFAULT_SETTINGS.copy()
    result.update(settings or {})
    result["max_concurrent_tasks"] = max(1, min(int(result["max_concurrent_tasks"] or 1), 20))
    result["max_concurrent_items"] = max(1, min(int(result["max_concurrent_items"] or 1), 50))
    result["max_global_items"] = max(1, min(int(result["max_global_items"] or 1), 200))
    result["max_concurrent_per_host"] = max(1, min(int(result["max_concurrent_per_host"] or 1), 50))
    result["max_extract_concurrency"] = max(1, min(int(result["max_extract_concurrency"] or 1), 50))
    result["request_timeout_seconds"] = max(5, min(int(result["request_timeout_seconds"] or 30), 300))
    result["ui_scale"] = max(0.5, min(float(result["ui_scale"] or 1.0), 1.5))
    result["download_dir"] = str(
        Path(result["download_dir"] or DEFAULT_SETTINGS["download_dir"])
        .expanduser()
        .resolve(strict=False)
    )
    return result


def get_settings():
    with _settings_lock:
        if not SETTINGS_FILE.exists():
            return DEFAULT_SETTINGS.copy()
        try:
            with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                return _validated(json.load(f))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    validated = _validated(settings)
    temporary = SETTINGS_FILE.with_suffix(".json.tmp")
    with _settings_lock:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8") as f:
            json.dump(validated, f, indent=4)
        os.replace(temporary, SETTINGS_FILE)
    return validated
