import os
import json

SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {
    "download_dir": os.path.join(os.getcwd(), "downloads"),
    "dark_mode": True,
    "use_playwright": True,
    "max_concurrent_tasks": 3,
    "max_concurrent_items": 5
}

def get_settings():
    if not os.path.exists(SETTINGS_FILE):
        return DEFAULT_SETTINGS.copy()
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
            # Merge with defaults in case of missing keys
            settings = DEFAULT_SETTINGS.copy()
            settings.update(data)
            return settings
    except Exception:
        return DEFAULT_SETTINGS.copy()

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)
