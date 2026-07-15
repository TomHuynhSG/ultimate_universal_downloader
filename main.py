import socket
import threading
import time

import uvicorn
import webview

from backend.app import app


server = None


def run_api():
    global server
    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="info")
    server = uvicorn.Server(config)
    server.run()


def wait_for_api(timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", 8000), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("The local API did not become ready within 15 seconds")


if __name__ == "__main__":
    api_thread = threading.Thread(target=run_api, daemon=True, name="uud-api")
    api_thread.start()
    wait_for_api()

    webview.create_window(
        "Ultimate Universal Downloader (UUD)",
        "http://127.0.0.1:8000/",
        width=1200,
        height=800,
    )
    try:
        webview.start()
    finally:
        if server:
            server.should_exit = True
        api_thread.join(timeout=5)
