"""On-demand image thumbnail generation and small persistent cache.

The original Telegram image is downloaded only when Telegram does not provide a
usable thumbnail. It is written to a temporary file, resized, and immediately
removed. Only the generated WebP card thumbnail remains in the cache.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import get_settings
from .models import File
from .telegram import get_message_from_channel, tg_client

logger = logging.getLogger(__name__)
settings = get_settings()

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_CACHE_DIR = _BACKEND_DIR / "data" / "thumbnails"
_GENERATION_SEMAPHORE: asyncio.Semaphore | None = None
_GENERATION_LOCKS: list[asyncio.Lock] | None = None


def thumbnail_cache_dir() -> Path:
    configured = settings.thumbnail_cache_dir.strip()
    path = Path(configured).expanduser() if configured else _DEFAULT_CACHE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_key(file: File) -> str:
    stable_value = f"{file.user_id}:{file.id}:{file.file_unique_id}"
    return hashlib.sha256(stable_value.encode("utf-8")).hexdigest()


def thumbnail_cache_path(file: File) -> Path:
    key = _cache_key(file)
    return thumbnail_cache_dir() / str(file.user_id) / key[:2] / f"{key}.webp"


def _generation_semaphore() -> asyncio.Semaphore:
    global _GENERATION_SEMAPHORE
    if _GENERATION_SEMAPHORE is None:
        _GENERATION_SEMAPHORE = asyncio.Semaphore(
            max(1, settings.thumbnail_generation_concurrency)
        )
    return _GENERATION_SEMAPHORE


def _generation_lock(file: File) -> asyncio.Lock:
    global _GENERATION_LOCKS
    if _GENERATION_LOCKS is None:
        # Striped locks prevent duplicate work without retaining one lock per file.
        _GENERATION_LOCKS = [asyncio.Lock() for _ in range(64)]
    return _GENERATION_LOCKS[int(_cache_key(file)[:8], 16) % len(_GENERATION_LOCKS)]


def _candidate_media_refs(file: File, message: object | None) -> Iterable[str]:
    seen: set[str] = set()

    def add(value: object | None):
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            return value
        return None

    value = add(file.file_id)
    if value:
        yield value

    if message is None:
        return

    photo = getattr(message, "photo", None)
    if isinstance(photo, (list, tuple)):
        for item in reversed(photo):
            value = add(getattr(item, "file_id", None))
            if value:
                yield value
    elif photo is not None:
        value = add(getattr(photo, "file_id", None))
        if value:
            yield value

    for attribute in ("document", "video", "audio"):
        media = getattr(message, attribute, None)
        value = add(getattr(media, "file_id", None) if media is not None else None)
        if value:
            yield value


def _render_webp_thumbnail(source_path: Path, destination_path: Path) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.stem}-",
        suffix=".tmp",
        dir=destination_path.parent,
    )
    os.close(descriptor)
    temporary_output = Path(temporary_name)

    try:
        with Image.open(source_path) as opened:
            # Animated files use their first frame for a stable card thumbnail.
            try:
                opened.seek(0)
            except EOFError:
                pass

            image = ImageOps.exif_transpose(opened)
            image.thumbnail(
                (settings.thumbnail_max_dimension, settings.thumbnail_max_dimension),
                Image.Resampling.LANCZOS,
            )

            has_alpha = image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            )
            prepared = image.convert("RGBA" if has_alpha else "RGB")
            prepared.save(
                temporary_output,
                format="WEBP",
                quality=settings.thumbnail_webp_quality,
                method=4,
            )

        os.replace(temporary_output, destination_path)
    finally:
        temporary_output.unlink(missing_ok=True)


async def read_cached_thumbnail(file: File) -> bytes | None:
    path = thumbnail_cache_path(file)
    if not path.is_file():
        return None
    try:
        return await asyncio.to_thread(path.read_bytes)
    except OSError as exc:
        logger.debug("Could not read thumbnail cache for file %s: %s", file.id, exc)
        return None


async def generate_image_thumbnail(file: File, message: object | None = None) -> bytes | None:
    """Generate and cache a WebP thumbnail from the original image once.

    The function is intentionally on-demand. Browser lazy loading controls which
    items request thumbnails, while the semaphore prevents a large folder from
    downloading many originals concurrently.
    """
    cached = await read_cached_thumbnail(file)
    if cached:
        return cached

    async with _generation_lock(file):
        cached = await read_cached_thumbnail(file)
        if cached:
            return cached

        async with _generation_semaphore():
            cache_path = thumbnail_cache_path(file)
            cache_path.parent.mkdir(parents=True, exist_ok=True)

            if message is None:
                try:
                    message = await get_message_from_channel(file.channel_message_id)
                except Exception as exc:
                    logger.debug(
                        "Could not fetch storage message while generating thumbnail for file %s: %s",
                        file.id,
                        exc,
                    )

            last_error: Exception | None = None
            # Keep the original in the OS temporary directory, never in the
            # persistent thumbnail volume. It is removed when this block exits.
            with tempfile.TemporaryDirectory(
                prefix="teleplay-thumb-"
            ) as temporary_directory:
                for index, media_ref in enumerate(_candidate_media_refs(file, message)):
                    requested_path = Path(temporary_directory) / f"source-{index}"
                    try:
                        downloaded_path = await tg_client.download_media(
                            media_ref,
                            file_name=str(requested_path),
                            in_memory=False,
                        )
                        if not downloaded_path:
                            continue

                        source_path = Path(str(downloaded_path))
                        if not source_path.is_file():
                            continue

                        await asyncio.to_thread(
                            _render_webp_thumbnail,
                            source_path,
                            cache_path,
                        )
                        return await read_cached_thumbnail(file)
                    except (UnidentifiedImageError, OSError, ValueError) as exc:
                        last_error = exc
                        logger.warning(
                            "Unsupported/corrupt image while generating thumbnail for file %s: %s",
                            file.id,
                            exc,
                        )
                    except Exception as exc:
                        last_error = exc
                        logger.warning(
                            "Thumbnail source download failed for file %s: %s",
                            file.id,
                            exc,
                        )

            if last_error:
                logger.error(
                    "Could not generate thumbnail for image file %s: %s",
                    file.id,
                    last_error,
                )
            return None


def delete_cached_thumbnail(file: File) -> None:
    """Best-effort cleanup used when a file is permanently deleted."""
    try:
        thumbnail_cache_path(file).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not remove thumbnail cache for file %s: %s", file.id, exc)
