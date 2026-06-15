from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os
import asyncio
from backend.api.router import router as api_router
from backend.core.downloader import DownloadManager

app = FastAPI(title="Universal Media Downloader API")

@app.on_event("startup")
async def startup_event():
    manager = DownloadManager.get_instance()
    asyncio.create_task(manager._dispatcher())

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api")

# Mount frontend
frontend_dist = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")

if os.path.exists(frontend_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dist, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        index_path = os.path.join(frontend_dist, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
        return {"error": "Frontend build not found"}
else:
    @app.get("/")
    def read_root():
        return {"status": "ok", "message": "API running, but frontend/dist not found."}
