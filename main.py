import threading
import uvicorn
import webview
import time
from backend.app import app
from backend.plugins.manager import PluginManager
from backend.database.models import Base, engine

def run_api():
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")

if __name__ == '__main__':
    # Initialize DB
    Base.metadata.create_all(bind=engine)
    
    # Load Plugins
    plugin_manager = PluginManager()
    plugin_manager.load_plugins()
    
    # Start the FastAPI server in a background thread
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()

    # Give the API a moment to start up
    time.sleep(1.5)

    # Launch PyWebview pointing to the FastAPI static file server
    webview.create_window('Ultimate Universal Downloader (UUD)', 'http://127.0.0.1:8000/', width=1200, height=800)
    webview.start()
