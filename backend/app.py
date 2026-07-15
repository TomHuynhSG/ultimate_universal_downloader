import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.router import router as api_router
from backend.core.downloader import DownloadManager
from backend.core.paths import PROJECT_ROOT, resolve_within


ALLOWED_ORIGINS = {
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
}


@asynccontextmanager
async def lifespan(_app):
    manager = DownloadManager.get_instance()
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(title="Universal Media Downloader API", lifespan=lifespan)


@app.middleware("http")
async def reject_cross_origin_mutations(request: Request, call_next):
    origin = request.headers.get("origin")
    if (
        request.url.path.startswith("/api/")
        and request.method not in {"GET", "HEAD", "OPTIONS"}
        and origin
        and origin not in ALLOWED_ORIGINS
    ):
        return JSONResponse(status_code=403, content={"detail": "Origin is not allowed"})
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_ORIGINS),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)

app.include_router(api_router, prefix="/api")

frontend_dist = PROJECT_ROOT / "frontend" / "dist"
index_path = frontend_dist / "index.html"

if frontend_dist.exists():
    assets_path = frontend_dist / "assets"
    if assets_path.exists():
        app.mount("/assets", StaticFiles(directory=assets_path), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        try:
            requested = resolve_within(frontend_dist, full_path)
        except ValueError:
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        if full_path and requested.is_file():
            return FileResponse(requested)
        if index_path.exists():
            return FileResponse(index_path)
        return JSONResponse(
            status_code=503,
            content={"error": "Frontend build not found. Run npm run build in frontend/."},
        )
else:
    @app.get("/")
    def read_root():
        return JSONResponse(
            status_code=503,
            content={"error": "Frontend build not found. Run npm run build in frontend/."},
        )
