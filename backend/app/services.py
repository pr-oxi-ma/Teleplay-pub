"""
Shared business logic and database queries.
"""
import re
from urllib.parse import quote
from typing import Dict, List, Optional, Sequence
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from .models import File, WatchProgress, Folder
from .edge_cache import build_edge_stream_url
from .media_types import resolve_media_type


def build_public_stream_path(file: File) -> str | None:
    """Return a readable public URL while retaining the secure share token.

    The filename is cosmetic and URL-encoded; the database lookup continues to
    use public_hash so duplicate names and later renames cannot expose another
    user's file. Legacy hash-only routes remain supported by the streaming
    router.
    """
    if not file.public_hash:
        return None
    display_name = sanitize_filename(file.file_name or "file")
    encoded_name = quote(display_name, safe="", encoding="utf-8", errors="strict")
    return f"/api/stream/s/{file.public_hash}/{encoded_name}"


IMAGE_FILE_EXTENSIONS = {
    "jpg", "jpeg", "png", "webp", "gif", "bmp", "avif", "svg", "ico",
    "tif", "tiff", "heic", "heif",
}


def is_image_file_record(file: File) -> bool:
    """Match image records even when Telegram stored them as documents."""
    mime_type = (file.mime_type or "").lower()
    extension = (
        file.file_name.rsplit(".", 1)[-1].lower()
        if file.file_name and "." in file.file_name
        else ""
    )
    return (
        file.file_type == "image"
        or mime_type.startswith("image/")
        or extension in IMAGE_FILE_EXTENSIONS
    )


def escape_like(value: str) -> str:
    """Escape special LIKE/ILIKE characters to prevent SQL injection."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def sanitize_filename(name: str) -> str:
    """
    Sanitize filename to prevent path traversal and XSS attacks.
    """
    if not name:
        return "unnamed_file"
    
    # Remove null bytes and path separators
    name = name.replace("\x00", "").replace("/", "_").replace("\\", "_")
    
    # Remove dangerous characters
    name = re.sub(r'[<>:"|?*\x00-\x1f]', '_', name)
    
    # Remove leading/trailing dots and spaces
    name = name.strip(". ")
    
    # Limit length
    if len(name) > 255:
        if "." in name:
            ext = name.rsplit(".", 1)[-1][:10]
            name = name[:255 - len(ext) - 1] + "." + ext
        else:
            name = name[:255]
    
    return name if name else "unnamed_file"

def add_urls_to_file(file: File, last_pos: Optional[int] = None) -> dict:
    """Add stream URLs with a direct-origin fallback plus progress data."""
    fallback_stream_url = f"/api/stream/{file.id}"
    edge_stream_url = build_edge_stream_url(file)
    data = {
        "id": file.id,
        "user_id": file.user_id,
        "folder_id": file.folder_id,
        "file_id": file.file_id,
        "file_unique_id": file.file_unique_id,
        "channel_message_id": file.channel_message_id,
        "thumbnail_file_id": file.thumbnail_file_id,
        "file_name": file.file_name,
        "file_size": file.file_size,
        "mime_type": resolve_media_type(file.file_name, file.mime_type, file.file_type),
        "file_type": file.file_type,
        "duration": file.duration,
        "width": file.width,
        "height": file.height,
        "created_at": file.created_at,
        "updated_at": file.updated_at,
        "deleted_at": file.deleted_at,
        "purge_after": file.purge_after,
        "stream_url": edge_stream_url or fallback_stream_url,
        "fallback_stream_url": fallback_stream_url if edge_stream_url else None,
        "download_url": f"/api/stream/{file.id}?download=1",
        "thumbnail_url": (
            f"/api/stream/{file.id}/thumbnail"
            if file.thumbnail_file_id or is_image_file_record(file)
            else None
        ),
        "last_pos": int(last_pos or 0),
        # Android legacy clients read progress/progress_updated. The web client
        # uses last_pos, so keep both names in sync.
        "progress": int(last_pos or 0),
        "progress_updated": None,
    }
    
    if file.public_hash:
        data["public_hash"] = file.public_hash
        data["public_stream_url"] = build_public_stream_path(file)
        
    return data

async def fetch_progress_positions(
    db: AsyncSession,
    user_id: int,
    file_ids: Sequence[int],
) -> Dict[int, int]:
    """Fetch active resume positions only for displayed files.

    This is lighter than selectinloading full WatchProgress rows for every file card.
    """
    if not file_ids:
        return {}

    result = await db.execute(
        select(WatchProgress.file_id, WatchProgress.position)
        .where(
            WatchProgress.user_id == user_id,
            WatchProgress.file_id.in_(file_ids),
            WatchProgress.position > 0,
            WatchProgress.completed == False,
        )
    )
    return {int(file_id): int(position) for file_id, position in result.all()}

async def fetch_recent_files(db: AsyncSession, user_id: int, limit: int) -> List[File]:
    """Get recently added files across all folders."""
    query = (
        select(File)
        .where(File.user_id == user_id)
        .order_by(desc(File.created_at))
        .limit(limit)
    )
    result = await db.execute(query)
    return result.scalars().all()

async def fetch_continue_watching_files(db: AsyncSession, user_id: int, limit: int) -> List[File]:
    """Get files with watch progress (not completed)."""
    query = (
        select(File)
        .join(WatchProgress, File.id == WatchProgress.file_id)
        .where(
            File.user_id == user_id,
            WatchProgress.user_id == user_id,
            WatchProgress.position > 0,
            WatchProgress.completed == False
        )
        .order_by(desc(WatchProgress.updated_at))
        .limit(limit)
    )
    result = await db.execute(query)
    return result.scalars().unique().all()
