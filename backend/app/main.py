"""
FastAPI main application with Telegram MTProto client lifecycle.
"""
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
import os
from urllib.parse import urlparse

import logging
logging.getLogger("pyrogram").setLevel(logging.INFO)

from .config import get_settings
from .database import init_db
from .telegram import start_telegram_client, stop_telegram_client
from .cache import close_cache
from .routers import files_router, folders_router, streaming_router, auth_router, tv_router, trash_router
from .recycle_bin import recycle_bin_cleanup_loop
from .rate_limit import limiter
from .migration_runner import run_pending_migrations
from .media_cache import media_cache
from .database import engine
from sqlalchemy import text

# Import bot to register handlers
from . import bot  # noqa

settings = get_settings()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - start/stop Telegram client and init DB."""
    logger.info("Starting TelePlay Backend...")
    await init_db()
    logger.info("Database initialized")
    applied_migrations = await run_pending_migrations()
    if applied_migrations:
        logger.info("Applied database migrations: %s", ", ".join(applied_migrations))
    else:
        logger.info("No pending database migrations")
    await start_telegram_client()
    logger.info("Telegram client started")
    await media_cache.start()
    recycle_cleanup_task = asyncio.create_task(recycle_bin_cleanup_loop())
    
    yield
    
    logger.info("Shutting down...")
    recycle_cleanup_task.cancel()
    try:
        await recycle_cleanup_task
    except asyncio.CancelledError:
        pass
    await media_cache.stop()
    logger.info("Managed media cache stopped")
    await stop_telegram_client()
    logger.info("Telegram client stopped")
    await close_cache()
    logger.info("Optional Redis cache closed")


app = FastAPI(
    title="TelePlay API",
    description="Stream files from Telegram to Android TV and Web",
    version="1.0.0",
    lifespan=lifespan,
)

# Add rate limiter to app state
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS middleware - settings.allowed_cors_origins is derived from WEB_BASE_URL plus local/dev fallbacks.
allowed_origins = settings.allowed_cors_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Range", "X-TelePlay-CSRF"],
    expose_headers=[
        "Content-Range", "Accept-Ranges", "Content-Length", "Content-Type",
        "Content-Disposition", "X-TelePlay-Origin-Cache", "X-TelePlay-Edge-Cache",
    ],
)


def _origin_from_header(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return value.strip().rstrip("/") or None


@app.middleware("http")
async def csrf_cookie_guard(request: Request, call_next):
    """Protect HttpOnly-cookie browser sessions from CSRF.

    Bearer-token Android/TV clients are not affected. Browser requests that use
    TelePlay's session cookies must send the custom X-TelePlay-CSRF header for
    state-changing API calls. Cross-origin browser requests are also restricted
    to the configured credentialed CORS origins.
    """
    unsafe_method = request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    api_path = request.url.path.startswith("/api/")
    has_session_cookie = (
        settings.session_access_cookie_name in request.cookies
        or settings.session_refresh_cookie_name in request.cookies
    )
    has_bearer = request.headers.get("authorization", "").lower().startswith("bearer ")

    if settings.session_csrf_enabled and unsafe_method and api_path and has_session_cookie and not has_bearer:
        if request.headers.get("x-teleplay-csrf") != "1":
            return JSONResponse({"detail": "Missing CSRF header"}, status_code=403)

        origin = _origin_from_header(request.headers.get("origin"))
        if not origin:
            origin = _origin_from_header(request.headers.get("referer"))

        if origin and origin not in set(settings.allowed_cors_origins):
            return JSONResponse({"detail": "CSRF origin check failed"}, status_code=403)

    return await call_next(request)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    
    # Prevent MIME type sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"
    
    # Prevent clickjacking (allow framing only for same origin)
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    
    # XSS protection (legacy; CSP is the modern control below)
    response.headers["X-XSS-Protection"] = "1; mode=block"

    # Referrer policy - don't leak full URLs/codes to other origins.
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    # Modern browser hardening. Keep CSP connect-src separate from CORS.
    # CORS includes localhost dev fallbacks, but production CSP should not
    # allow a page to connect to a visitor's localhost services.
    connect_sources = " ".join(
        origin for origin in ["'self'", settings.web_origin] if origin
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https:; "
        "media-src 'self' blob: https:; "
        "font-src 'self' data:; "
        f"connect-src {connect_sources};"
    )

    return response


# Include routers
app.include_router(auth_router, prefix="/api")
app.include_router(files_router, prefix="/api")
app.include_router(folders_router, prefix="/api")
app.include_router(streaming_router, prefix="/api")
app.include_router(tv_router, prefix="/api")
app.include_router(trash_router, prefix="/api")





@app.get("/health")
@app.get("/health/live")
async def health():
    """Process liveness check. External cache failures do not fail liveness."""
    return {"status": "healthy"}


@app.get("/health/ready")
async def readiness():
    """Readiness check for database-backed request handling."""
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("Readiness database check failed: %s", exc)
        return JSONResponse({"status": "not_ready", "database": False}, status_code=503)
    return {
        "status": "ready",
        "database": True,
        "cache_mode": settings.normalized_cache_mode,
    }


@app.head("/")
async def root_head():
    """Render and other platforms may use HEAD / for health checks."""
    return Response(status_code=200)


# ... imports ...

# Mount static files (assets) - checking if directory exists first to avoid dev errors
static_dir = settings.static_dir.rstrip("/")
static_assets_dir = os.path.join(static_dir, "assets")
if os.path.exists(static_assets_dir):
    app.mount("/assets", StaticFiles(directory=static_assets_dir), name="assets")

# ... (API routers are included above) ...

@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    """Serve the React SPA for any non-API routes."""
    # API routes are already handled by routers above
    if full_path == "api" or full_path.startswith("api/"):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="API Endpoint not found")

    # Check if the file exists in static directory (e.g. logo.png, favicon.ico)
    static_file_path = os.path.join(static_dir, full_path)
    if os.path.exists(static_file_path) and os.path.isfile(static_file_path):
        return FileResponse(static_file_path)

    # Serve index.html for generic SPA routes
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)

    return {"message": "Backend running. Frontend not built/mounted (dev mode)."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.server_host,
        port=settings.server_port,
        reload=True
    )
