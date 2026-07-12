"""
Streaming API endpoints for media playback.
"""
import asyncio
import re
import logging
import unicodedata
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query, BackgroundTasks
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field

from ..database import get_db
from ..models import File, User, MediaCacheEntry, MediaCacheJob
from ..auth import get_current_user
from ..telegram import get_message_from_channel, tg_client
from ..streaming import stream_file as stream_file_generator, warm_stream_start_cache
from ..config import get_settings
from ..rate_limit import limiter
from ..services import is_image_file_record, sanitize_filename
from ..thumbnail_cache import generate_image_thumbnail, read_cached_thumbnail
from ..edge_cache import (
    build_edge_stream_url,
    cache_key_for_file,
    verify_edge_cache_key,
    verify_touch,
)
from ..google_drive_cache import DriveCacheError, DriveObjectMissing, DriveRateLimited
from ..media_cache import media_cache, ReadyCacheObject
from ..media_types import resolve_media_type, sniff_media_type, supports_inline_display

# Logger for internal debugging (not exposed to users)
logger = logging.getLogger(__name__)

# Rate limiter for public endpoints
settings = get_settings()

router = APIRouter(prefix="/stream", tags=["Streaming"])


class StreamPrefetchRequest(BaseModel):
    file_ids: list[int] = Field(default_factory=list, max_length=4)


class CacheTouchRequest(BaseModel):
    cache_key: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")


async def warm_file_cache_task(file_id: int, channel_message_id: int) -> None:
    if not settings.redis_prefetch_enabled or not settings.redis_url:
        return

    try:
        message = await get_message_from_channel(channel_message_id)
        warmed = await warm_stream_start_cache(message)
        if warmed:
            logger.debug("Prefetched %s chunks for file %s", warmed, file_id)
    except Exception as exc:
        # Prefetch is best-effort only. Playback must never fail because cache warmup failed.
        logger.debug("Prefetch skipped for file %s: %s", file_id, exc)


STREAM_BASE_HEADERS = {
    # Prevent reverse proxies from buffering the Telegram stream before the
    # browser receives it. This improves startup and lowers server RAM usage.
    "X-Accel-Buffering": "no",
    "Cache-Control": "private, max-age=3600, no-transform",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges, Content-Type, Content-Disposition",
    "Vary": "Origin",
    # Safe for credentialed Vercel/Render media tags. If a future frontend uses
    # COEP, this prevents cross-origin media from being blocked by CORP.
    "Cross-Origin-Resource-Policy": "cross-origin",
}


def parse_range_header(range_header: str | None, file_size: int) -> tuple[int, int]:
    """Strictly parse one RFC 7233 byte range. Invalid/multi-ranges are rejected."""
    if file_size <= 0:
        raise ValueError("empty file")
    if not range_header:
        return 0, file_size - 1
    value = range_header.strip()
    if "," in value:
        raise ValueError("multiple ranges are not supported")
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value, re.IGNORECASE)
    if not match:
        raise ValueError("malformed range")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ValueError("empty range")
    if not start_text:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("invalid suffix range")
        suffix_length = min(suffix_length, file_size)
        return file_size - suffix_length, file_size - 1
    start = int(start_text)
    end = int(end_text) if end_text else file_size - 1
    if start < 0 or start >= file_size or end < start:
        raise ValueError("range outside file")
    return start, min(end, file_size - 1)


def get_mime_type(file: File) -> str:
    """Return a browser-safe MIME type for current and legacy DB rows."""
    return resolve_media_type(file.file_name, file.mime_type, file.file_type)


def is_image_record(file: File) -> bool:
    return is_image_file_record(file)


def has_message_media(message) -> bool:
    return bool(
        getattr(message, "document", None)
        or getattr(message, "video", None)
        or getattr(message, "audio", None)
        or getattr(message, "photo", None)
    )


def _ascii_filename_fallback(filename: str) -> str:
    """Build a conservative quoted ``filename=`` fallback for older clients."""
    normalized = unicodedata.normalize("NFKD", filename)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", ascii_name)
    ascii_name = re.sub(r"[^A-Za-z0-9._ ()\[\]-]", "_", ascii_name)
    ascii_name = re.sub(r"\s+", " ", ascii_name).strip(" .")
    if not ascii_name:
        ascii_name = "download"
    # Keep the header bounded and escape quoted-string delimiters.
    return ascii_name[:180].replace("\\", "_").replace('"', "_")


def content_disposition(file: File, mime_type: str, download: int) -> str:
    disposition = "attachment" if download else (
        "inline" if supports_inline_display(mime_type) else "attachment"
    )

    # Sanitize response-time legacy records too, then send both RFC-compatible
    # forms: ``filename=`` for old Android/download managers and ``filename*=``
    # for the exact UTF-8 name in modern browsers.
    filename = sanitize_filename(file.file_name or "download")
    ascii_fallback = _ascii_filename_fallback(filename)
    encoded_utf8 = quote(filename, safe="", encoding="utf-8", errors="strict")
    return (
        f'{disposition}; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{encoded_utf8}"
    )


async def download_image_bytes(file: File) -> bytes:
    """
    Download image/photo bytes directly using Telegram file_id.

    This avoids the video range streamer for Telegram photos. Photos often have a
    reusable file_id/thumbnail_file_id even when the copied storage-channel
    message is not streamable through stream_media, which caused 200 OK responses
    with zero bytes and browser image preview failures.
    """
    candidate_refs = []
    if file.file_id:
        candidate_refs.append(file.file_id)
    if file.thumbnail_file_id and file.thumbnail_file_id not in candidate_refs:
        candidate_refs.append(file.thumbnail_file_id)

    # Fallback to the stored channel message media object when available.
    try:
        message = await get_message_from_channel(file.channel_message_id)
        if message:
            if getattr(message, "photo", None):
                photo = message.photo[-1] if isinstance(message.photo, (list, tuple)) else message.photo
                if photo and getattr(photo, "file_id", None):
                    candidate_refs.append(photo.file_id)
            elif getattr(message, "document", None) and getattr(message.document, "file_id", None):
                candidate_refs.append(message.document.file_id)
    except Exception as exc:
        logger.warning("Could not fetch image message %s: %s", file.channel_message_id, exc)

    last_error = None
    for media_ref in candidate_refs:
        try:
            downloaded = await tg_client.download_media(media_ref, in_memory=True)
            if downloaded is None:
                continue
            if hasattr(downloaded, "getvalue"):
                content = downloaded.getvalue()
            elif isinstance(downloaded, (bytes, bytearray)):
                content = bytes(downloaded)
            else:
                try:
                    downloaded.seek(0)
                    content = downloaded.read()
                except Exception:
                    continue

            if content:
                return content
        except Exception as exc:
            last_error = exc
            logger.warning("Image download failed for file %s using ref: %s", file.id, exc)

    if last_error:
        logger.error("Image download failed for file %s: %s", file.id, last_error)
    raise HTTPException(status_code=404, detail="Image media not found")


async def drive_stream_response(
    file: File,
    request: Request,
    download: int,
    ready: ReadyCacheObject,
    from_bytes: int,
    until_bytes: int,
    range_header: str | None,
) -> StreamingResponse:
    """Open Drive before returning headers so failures can fall back to Telegram."""
    drive_response = await media_cache.open_range(ready, from_bytes, until_bytes)
    req_length = until_bytes - from_bytes + 1
    mime_type = get_mime_type(file)

    async def drive_streamer():
        sent = 0
        disconnected = False
        last_lease_renewal = asyncio.get_running_loop().time()
        try:
            async for chunk in drive_response.aiter_bytes(256 * 1024):
                if await request.is_disconnected():
                    disconnected = True
                    break
                loop_now = asyncio.get_running_loop().time()
                if (
                    loop_now - last_lease_renewal
                    >= settings.google_drive_read_lease_renew_seconds
                ):
                    await media_cache.renew_drive_read(ready.cache_key)
                    last_lease_renewal = loop_now
                remaining = req_length - sent
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                if not chunk:
                    continue
                sent += len(chunk)
                yield chunk
        except asyncio.CancelledError:
            disconnected = True
            raise
        except Exception:
            if not await request.is_disconnected():
                logger.exception("Google Drive stream failed for file %s", file.id)
        finally:
            await drive_response.aclose()
            truncated = not disconnected and sent < req_length
            await media_cache.record_drive_read(
                ready.cache_key,
                sent,
                truncated=truncated,
            )
            if truncated:
                logger.error(
                    "Drive stream ended early for file %s: sent %s of %s bytes",
                    file.id, sent, req_length,
                )

    headers = {
        **STREAM_BASE_HEADERS,
        "Content-Type": mime_type,
        "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file.file_size}",
        "Content-Length": str(req_length),
        "Content-Disposition": content_disposition(file, mime_type, download),
        "Accept-Ranges": "bytes",
        "X-TelePlay-Origin-Cache": "GDRIVE",
    }
    return StreamingResponse(
        drive_streamer(),
        status_code=206 if range_header else 200,
        media_type=mime_type,
        headers=headers,
    )


async def image_response(
    file: File,
    request: Request,
    download: int,
    from_bytes: int,
    until_bytes: int,
    range_header: str | None,
) -> Response:
    declared_mime_type = get_mime_type(file)
    content = await download_image_bytes(file)
    # Bytes are authoritative when already available. This fixes legacy rows
    # where Telegram reported JPEG/PNG documents as text/plain or octet-stream.
    mime_type = sniff_media_type(content, declared_mime_type)
    # Drive cache relies on immutable metadata size. Mismatched Telegram photos
    # remain streamable but are deliberately not admitted into the L2 cache.
    if len(content) == int(file.file_size or 0):
        await media_cache.record_telegram_access(file, len(content))
    else:
        from_bytes, until_bytes = 0, len(content) - 1
        range_header = None

    selected = content[from_bytes : until_bytes + 1]
    headers = {
        **STREAM_BASE_HEADERS,
        "Content-Type": mime_type,
        "Content-Disposition": content_disposition(file, mime_type, download),
        "Content-Length": str(len(selected)),
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=86400, no-transform",
        "X-TelePlay-Origin-Cache": "TELEGRAM",
    }
    if range_header:
        headers["Content-Range"] = f"bytes {from_bytes}-{until_bytes}/{len(content)}"
    return Response(
        content=selected,
        status_code=206 if range_header else 200,
        media_type=mime_type,
        headers=headers,
    )


async def stream_file_response(file: File, request: Request, download: int) -> StreamingResponse | Response:
    """Serve one immutable asset through Drive when ready, otherwise Telegram."""
    file_size = int(file.file_size or 0)
    if file_size <= 0:
        raise HTTPException(status_code=404, detail="File has no streamable size")

    range_header = request.headers.get("range")
    try:
        from_bytes, until_bytes = parse_range_header(range_header, file_size)
    except (ValueError, OverflowError):
        return Response(
            status_code=416,
            content="416: Range not satisfiable",
            headers={
                **STREAM_BASE_HEADERS,
                "Content-Range": f"bytes */{file_size}",
                "Accept-Ranges": "bytes",
            },
        )

    ready = await media_cache.get_ready(file)
    if ready is not None:
        try:
            return await drive_stream_response(
                file, request, download, ready, from_bytes, until_bytes, range_header
            )
        except DriveObjectMissing:
            logger.info("Drive object disappeared for file %s; using Telegram", file.id)
        except (DriveRateLimited, DriveCacheError) as exc:
            logger.warning("Drive unavailable for file %s: %s", file.id, exc)

    if is_image_record(file):
        return await image_response(
            file, request, download, from_bytes, until_bytes, range_header
        )

    message = await get_message_from_channel(file.channel_message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found in channel")
    if not has_message_media(message):
        logger.error(
            "Storage message %s for file %s has no media",
            file.channel_message_id, file.id,
        )
        raise HTTPException(status_code=404, detail="Stored media not found")

    req_length = until_bytes - from_bytes + 1
    mime_type = get_mime_type(file)

    async def file_streamer():
        sent = 0
        disconnected = False
        try:
            async for chunk in stream_file_generator(
                tg_client,
                message,
                from_bytes,
                until_bytes,
                should_stop=request.is_disconnected,
            ):
                if await request.is_disconnected():
                    disconnected = True
                    break
                sent += len(chunk)
                yield chunk
        except asyncio.CancelledError:
            disconnected = True
            raise
        except Exception:
            if await request.is_disconnected():
                disconnected = True
                return
            logger.exception("Telegram stream failed for file %s", file.id)
            raise
        finally:
            if sent > 0:
                try:
                    await media_cache.record_telegram_access(file, sent)
                except Exception:
                    logger.exception("Could not record cache admission for file %s", file.id)
            if not disconnected and sent < req_length and not await request.is_disconnected():
                logger.error(
                    "Telegram stream ended early for file %s: sent %s of %s bytes",
                    file.id, sent, req_length,
                )

    headers = {
        **STREAM_BASE_HEADERS,
        "Content-Type": mime_type,
        "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
        "Content-Length": str(req_length),
        "Content-Disposition": content_disposition(file, mime_type, download),
        "Accept-Ranges": "bytes",
        "X-TelePlay-Origin-Cache": "TELEGRAM",
    }
    return StreamingResponse(
        file_streamer(),
        status_code=206 if range_header else 200,
        media_type=mime_type,
        headers=headers,
    )


def _thumbnail_bytes(downloaded) -> bytes:
    if downloaded is None:
        return b""
    if hasattr(downloaded, "getvalue"):
        return downloaded.getvalue()
    if isinstance(downloaded, (bytes, bytearray)):
        return bytes(downloaded)
    try:
        downloaded.seek(0)
        return downloaded.read()
    except Exception:
        return b""


def _thumbnail_media_type(content: bytes) -> str:
    detected = sniff_media_type(content, "image/jpeg")
    return detected if detected.startswith("image/") else "image/jpeg"


def _choose_lightweight_thumbnail(candidates: list) -> object | None:
    valid = [candidate for candidate in candidates if getattr(candidate, "file_id", None)]
    if not valid:
        return None

    def dimension(candidate) -> int:
        width = int(getattr(candidate, "width", 0) or 0)
        height = int(getattr(candidate, "height", 0) or 0)
        return max(width, height)

    valid.sort(key=dimension)
    # Around 160px is enough for cards while avoiding full-size image downloads.
    for candidate in valid:
        if dimension(candidate) >= 160:
            return candidate
    return valid[-1]


def _message_thumbnail_candidates(message) -> list:
    candidates: list = []
    for attribute in ("video", "document", "audio"):
        media = getattr(message, attribute, None)
        thumbs = getattr(media, "thumbs", None) if media is not None else None
        if thumbs:
            candidates.extend(list(thumbs))

    photo = getattr(message, "photo", None)
    if isinstance(photo, (list, tuple)):
        candidates.extend(list(photo))
    elif photo is not None:
        photo_thumbs = getattr(photo, "thumbs", None)
        if photo_thumbs:
            candidates.extend(list(photo_thumbs))
    return candidates


def _thumbnail_response(content: bytes, *, generated: bool = False) -> Response:
    return Response(
        content=content,
        media_type="image/webp" if generated else _thumbnail_media_type(content),
        headers={
            **STREAM_BASE_HEADERS,
            "Content-Length": str(len(content)),
            # Generated thumbnails are immutable for the lifetime of a file ID.
            "Cache-Control": (
                "private, max-age=2592000, immutable, no-transform"
                if generated
                else "private, max-age=86400, no-transform"
            ),
        },
    )


async def thumbnail_response(file: File) -> Response:
    """Return a small card thumbnail without retaining the original image.

    Telegram-provided thumbnails are preferred. When an image has no usable
    Telegram thumbnail, the original is downloaded once to a temporary file,
    resized to WebP, and deleted. Only that small generated WebP is cached.
    """
    try:
        # A previously generated fallback avoids another Telegram request.
        if is_image_record(file):
            cached = await read_cached_thumbnail(file)
            if cached:
                return _thumbnail_response(cached, generated=True)

        message = None
        try:
            message = await get_message_from_channel(file.channel_message_id)
        except Exception as exc:
            logger.debug(
                "Could not fetch storage message for thumbnail file %s: %s",
                file.id,
                exc,
            )

        candidate = (
            _choose_lightweight_thumbnail(_message_thumbnail_candidates(message))
            if message
            else None
        )
        media_ref = getattr(candidate, "file_id", None) if candidate is not None else None

        if not media_ref and file.thumbnail_file_id:
            is_original_image = (
                is_image_record(file)
                and file.thumbnail_file_id == file.file_id
            )
            if not is_original_image:
                media_ref = file.thumbnail_file_id

        if media_ref:
            try:
                downloaded = await tg_client.download_media(media_ref, in_memory=True)
                content = _thumbnail_bytes(downloaded)
                if content:
                    return _thumbnail_response(content)
            except Exception as exc:
                # A broken Telegram thumbnail should still fall back to generated
                # image thumbnails instead of leaving a permanent blank card.
                logger.warning(
                    "Telegram thumbnail failed for file %s, using generated fallback: %s",
                    file.id,
                    exc,
                )

        if is_image_record(file):
            generated = await generate_image_thumbnail(file, message)
            if generated:
                return _thumbnail_response(generated, generated=True)

        raise HTTPException(status_code=404, detail="Thumbnail not available")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Thumbnail error for file %s: %s", file.id, exc)
        raise HTTPException(status_code=500, detail="Failed to get thumbnail")


@router.post("/prefetch")
async def prefetch_stream_neighbors(
    payload: StreamPrefetchRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Best-effort warmup for next/previous media.

    Redis is optional. If REDIS_URL is missing, this endpoint simply returns
    disabled=true and playback still works normally. It never caches full videos.
    """
    unique_ids = []
    seen = set()
    for file_id in payload.file_ids:
        if isinstance(file_id, int) and file_id > 0 and file_id not in seen:
            seen.add(file_id)
            unique_ids.append(file_id)

    if not unique_ids:
        return {"enabled": bool(settings.redis_url and settings.redis_prefetch_enabled), "queued": 0}

    if not settings.redis_url or not settings.redis_prefetch_enabled:
        return {"enabled": False, "queued": 0}

    # The same player queue is used for active and Recycle Bin media. Include
    # user-owned soft-deleted rows so neighbour warmup behaves identically while
    # keeping the endpoint authenticated and owner-scoped.
    result = await db.execute(
        select(File)
        .where(File.user_id == current_user.id, File.id.in_(unique_ids))
        .execution_options(include_deleted=True)
    )
    files = result.scalars().all()

    for file in files:
        if file.file_type in {"video", "audio"} and file.channel_message_id:
            background_tasks.add_task(
                warm_file_cache_task,
                file.id,
                file.channel_message_id,
            )

    return {"enabled": True, "queued": len(files)}


@router.get("/public-resolve/{public_hash}", include_in_schema=False)
async def resolve_public_stream_for_worker(
    public_hash: str,
    request: Request,
    download: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    """Resolve an unguessable public link for the Cloudflare Worker.

    Only the Worker can call this endpoint. It returns immutable cache identity
    and the selected public-link mode, never Telegram, Drive or crypto secrets.
    Positive results are cached at the edge for only 30 seconds, keeping public
    link revocation reasonably quick without a Render metadata hit per range.
    """
    supplied = request.headers.get("x-teleplay-origin-secret", "")
    import hmac
    if (
        not settings.cloudflare_cache_enabled
        or not supplied
        or not hmac.compare_digest(supplied, settings.cloudflare_origin_secret)
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    result = await db.execute(select(File).where(File.public_hash == public_hash))
    file = result.scalar_one_or_none()
    if not file:
        raise HTTPException(status_code=404, detail="File not found or link revoked")

    mode = settings.normalized_public_stream_edge_mode
    mime_type = get_mime_type(file)
    edge_url = (
        build_edge_stream_url(file, force_download=bool(download))
        if mode == "redirect"
        else None
    )
    if mode == "redirect" and not edge_url:
        raise HTTPException(status_code=503, detail="Cloudflare edge cache is unavailable")

    return {
        "mode": mode,
        "version": settings.media_cache_key_version,
        "file_id": int(file.id),
        "size": int(file.file_size or 0),
        "cache_key": cache_key_for_file(file),
        "mime_type": mime_type,
        "content_disposition": content_disposition(file, mime_type, int(bool(download))),
        "edge_url": edge_url,
    }


@router.get("/origin/{file_id}", include_in_schema=False)
async def stream_worker_origin(
    file_id: int,
    request: Request,
    cache_key: str = Query(..., min_length=64, max_length=64),
    db: AsyncSession = Depends(get_db),
):
    """Private byte-range origin used only by the signed Cloudflare Worker."""
    supplied = request.headers.get("x-teleplay-origin-secret", "")
    import hmac
    if (
        not settings.cloudflare_cache_enabled
        or not supplied
        or not hmac.compare_digest(supplied, settings.cloudflare_origin_secret)
    ):
        raise HTTPException(status_code=403, detail="Forbidden")
    result = await db.execute(select(File).where(File.id == file_id))
    file = result.scalar_one_or_none()
    if not file or not verify_edge_cache_key(file, cache_key):
        raise HTTPException(status_code=404, detail="File not found")
    return await stream_file_response(file, request, 0)


@router.post("/cache-touch", include_in_schema=False)
async def edge_cache_touch(payload: CacheTouchRequest, request: Request):
    """Sampled Worker callback so edge-popular objects are not falsely evicted."""
    timestamp_text = request.headers.get("x-teleplay-touch-timestamp", "")
    signature = request.headers.get("x-teleplay-touch-signature", "")
    try:
        timestamp = int(timestamp_text)
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not signature or not verify_touch(payload.cache_key, timestamp, signature):
        raise HTTPException(status_code=403, detail="Forbidden")
    touched = await media_cache.record_edge_touch(payload.cache_key)
    return {"touched": touched}


@router.get("/cache-inspect/{file_id}", include_in_schema=False)
async def cache_inspect(
    file_id: int,
    request: Request,
    cache_key: str = Query(..., min_length=64, max_length=64),
    db: AsyncSession = Depends(get_db),
):
    """Return one file's cache state for production diagnostics.

    The endpoint is protected by the same internal origin secret used by the
    Worker. It deliberately excludes OAuth credentials, encryption keys and
    resumable upload URLs.
    """
    supplied = request.headers.get("x-teleplay-origin-secret", "")
    import hmac
    if not supplied or not hmac.compare_digest(supplied, settings.cloudflare_origin_secret):
        raise HTTPException(status_code=403, detail="Forbidden")

    file_result = await db.execute(select(File).where(File.id == file_id))
    file = file_result.scalar_one_or_none()
    if not file or not verify_edge_cache_key(file, cache_key):
        raise HTTPException(status_code=404, detail="File not found")

    entry_result = await db.execute(
        select(MediaCacheEntry).where(MediaCacheEntry.cache_key == cache_key)
    )
    entry = entry_result.scalar_one_or_none()
    job = None
    drive_metadata = None
    drive_error = None

    if entry is not None:
        job_result = await db.execute(
            select(MediaCacheJob).where(MediaCacheJob.cache_key == cache_key)
        )
        job = job_result.scalar_one_or_none()
        if entry.drive_file_id and media_cache.enabled:
            try:
                metadata = await media_cache.drive.get_metadata(str(entry.drive_file_id))
                drive_metadata = {
                    "id": metadata.get("id"),
                    "name": metadata.get("name"),
                    "size": int(metadata.get("size") or 0),
                    "mime_type": metadata.get("mimeType"),
                    "trashed": bool(metadata.get("trashed")),
                    "app_properties": metadata.get("appProperties") or {},
                }
            except Exception as exc:
                drive_error = f"{type(exc).__name__}: {str(exc)[:300]}"

    def iso(value):
        return value.isoformat() if value is not None else None

    file_size = int(file.file_size or 0)
    small_limit = int(settings.google_drive_admission_small_file_mb) * 1024**2
    bytes_limit = int(settings.google_drive_admission_bytes_mb) * 1024**2
    ratio_limit = max(1, int(file_size * float(settings.google_drive_admission_ratio)))
    admission_threshold = 1 if file_size <= small_limit else min(bytes_limit, ratio_limit)
    telegram_bytes = int(entry.telegram_bytes_served or 0) if entry is not None else 0

    return {
        "file": {
            "id": int(file.id),
            "name": file.file_name,
            "size": int(file.file_size or 0),
            "type": file.file_type,
            "mime_type": get_mime_type(file),
            "cache_key": cache_key,
        },
        "admission": {
            "small_file_limit_bytes": small_limit,
            "threshold_bytes": admission_threshold,
            "telegram_bytes_served": telegram_bytes,
            "met": telegram_bytes >= admission_threshold,
            "max_cache_file_bytes": int(settings.google_drive_max_cache_file_gb) * 1024**3,
        },
        "entry": None if entry is None else {
            "status": entry.status,
            "cache_version": int(entry.cache_version or 0),
            "drive_file_id": entry.drive_file_id,
            "legacy_drive_file_id": entry.legacy_drive_file_id,
            "encryption_version": int(entry.encryption_version or 0),
            "encrypted_size_bytes": int(entry.encrypted_size_bytes or 0),
            "size_bytes": int(entry.size_bytes or 0),
            "edge_hit_count": int(entry.edge_hit_count or 0),
            "drive_hit_count": int(entry.drive_hit_count or 0),
            "telegram_hit_count": int(entry.telegram_hit_count or 0),
            "telegram_bytes_served": int(entry.telegram_bytes_served or 0),
            "failure_count": int(entry.failure_count or 0),
            "truncated_read_count": int(entry.truncated_read_count or 0),
            "active_readers": int(entry.active_readers or 0),
            "next_retry_at": iso(entry.next_retry_at),
            "upload_started_at": iso(entry.upload_started_at),
            "upload_completed_at": iso(entry.upload_completed_at),
            "last_verified_at": iso(entry.last_verified_at),
            "last_error": entry.last_error,
        },
        "job": None if job is None else {
            "job_type": job.job_type,
            "status": job.status,
            "bytes_uploaded": int(job.bytes_uploaded or 0),
            "attempts": int(job.attempts or 0),
            "next_attempt_at": iso(job.next_attempt_at),
            "lease_expires_at": iso(job.lease_expires_at),
            "last_error": job.last_error,
        },
        "drive": {
            "metadata": drive_metadata,
            "error": drive_error,
        },
    }


@router.get("/cache-status", include_in_schema=False)
async def cache_status(request: Request):
    """Private operational snapshot for health dashboards and debugging."""
    supplied = request.headers.get("x-teleplay-origin-secret", "")
    import hmac
    if not supplied or not hmac.compare_digest(supplied, settings.cloudflare_origin_secret):
        raise HTTPException(status_code=403, detail="Forbidden")
    snapshot = await media_cache.status_snapshot()
    snapshot["public_stream_edge_mode"] = settings.normalized_public_stream_edge_mode
    return snapshot


@router.post("/cache-reconcile", include_in_schema=False)
async def cache_reconcile(request: Request):
    """Verify Drive pointers now and repair manually deleted cache objects."""
    supplied = request.headers.get("x-teleplay-origin-secret", "")
    import hmac
    if not supplied or not hmac.compare_digest(supplied, settings.cloudflare_origin_secret):
        raise HTTPException(status_code=403, detail="Forbidden")
    result = await media_cache.reconcile_now()
    return {"reconciliation": result, "status": await media_cache.status_snapshot()}


@router.get("/{file_id}")
async def stream_file(
    file_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    download: int = Query(0, description="Set to 1 to force download"),
):
    """Stream file from Telegram with range request support for seeking."""
    result = await db.execute(
        select(File).where(File.id == file_id, File.user_id == current_user.id)
    )
    file = result.scalar_one_or_none()

    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    return await stream_file_response(file, request, download)


@router.get("/{file_id}/thumbnail")
async def get_thumbnail(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a lightweight thumbnail, generating a small cached WebP when needed."""
    result = await db.execute(
        select(File).where(File.id == file_id, File.user_id == current_user.id)
    )
    file = result.scalar_one_or_none()

    if not file:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return await thumbnail_response(file)


@router.get("/s/{public_hash}/{filename}")
@router.get("/s/{public_hash}")
@limiter.limit("60/minute")
async def stream_public_file(
    public_hash: str,
    request: Request,
    filename: str | None = None,
    db: AsyncSession = Depends(get_db),
    download: int = Query(0, description="Set to 1 to force download"),
):
    """Stream a public file.

    ``filename`` is intentionally cosmetic. The unguessable/revocable
    ``public_hash`` remains the lookup key, while the readable filename makes
    copied links understandable. The legacy hash-only URL is kept compatible.
    """
    result = await db.execute(select(File).where(File.public_hash == public_hash))
    file = result.scalar_one_or_none()

    if not file:
        raise HTTPException(status_code=404, detail="File not found or link revoked")

    mode = settings.normalized_public_stream_edge_mode
    if mode == "redirect":
        edge_url = build_edge_stream_url(file, force_download=bool(download))
        if edge_url:
            return RedirectResponse(
                edge_url,
                status_code=307,
                headers={
                    "Cache-Control": "no-store",
                    "X-TelePlay-Public-Mode": "REDIRECT",
                },
            )
        logger.warning(
            "Public redirect requested for file %s but Cloudflare is unavailable; using origin",
            file.id,
        )

    response = await stream_file_response(file, request, download)
    response.headers["X-TelePlay-Public-Mode"] = (
        "PROXY-FALLBACK" if mode == "proxy" else "OFF"
    )
    return response
