"""Reliable media-type resolution for streamed Telegram/Drive files.

Telegram metadata is not always trustworthy: image documents can arrive as
``application/octet-stream`` or even ``text/plain``. Browsers navigating a raw
stream URL then render the binary bytes as text. This module keeps MIME handling
consistent across uploads, Telegram responses, Drive cache objects and the
Cloudflare origin.
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import PurePath

_MIME_RE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")

# Values that carry no useful content information. An extension/file category is
# more reliable than any of these.
_GENERIC_MIME_TYPES = {
    "application/octet-stream",
    "binary/octet-stream",
    "application/binary",
    "application/force-download",
    "application/download",
    "unknown/unknown",
}

# Python/OS MIME databases differ. Keep browser-important formats deterministic.
_EXTENSION_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".ts": "video/mp2t",
    ".3gp": "video/3gpp",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".xml": "application/xml",
    ".zip": "application/zip",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".apk": "application/vnd.android.package-archive",
}

_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "audio/mp3": "audio/mpeg",
    "audio/x-m4a": "audio/mp4",
    "video/x-m4v": "video/mp4",
}

# Active-content image/text types should not open inline on the API origin.
_UNSAFE_INLINE_TYPES = {
    "image/svg+xml",
    "text/html",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
}


def clean_mime_type(value: str | None) -> str:
    """Return a normalized bare MIME type or an empty string when invalid."""
    if not value:
        return ""
    cleaned = value.split(";", 1)[0].strip().lower()
    cleaned = _MIME_ALIASES.get(cleaned, cleaned)
    return cleaned if _MIME_RE.fullmatch(cleaned) else ""


def extension_mime_type(file_name: str | None) -> str:
    if not file_name:
        return ""
    suffix = PurePath(file_name).suffix.lower()
    if suffix in _EXTENSION_MIME_TYPES:
        return _EXTENSION_MIME_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(file_name, strict=False)
    return clean_mime_type(guessed)


def resolve_media_type(
    file_name: str | None,
    stored_mime_type: str | None,
    file_type: str | None = None,
) -> str:
    """Resolve a safe browser-facing MIME type.

    The semantic file category and a known filename extension override generic or
    contradictory Telegram metadata. Valid specific Telegram metadata remains
    preferred for document records where a filename can have been renamed.
    """
    stored = clean_mime_type(stored_mime_type)
    guessed = extension_mime_type(file_name)
    category = (file_type or "").strip().lower()

    expected_prefix = {
        "image": "image/",
        "video": "video/",
        "audio": "audio/",
    }.get(category)

    if expected_prefix:
        # A specific same-family Telegram MIME remains authoritative when the
        # user later renames a file to a different extension. Generic or
        # cross-family metadata (the bug this module fixes) is ignored.
        if stored.startswith(expected_prefix):
            return stored
        if guessed.startswith(expected_prefix):
            return guessed
        # Telegram photos are JPEG when neither metadata nor filename identifies
        # the exact format. Videos/audio are usually named, so conservative
        # category fallbacks are only used after both signals failed.
        return {
            "image": "image/jpeg",
            "video": "video/mp4",
            "audio": "audio/mpeg",
        }[category]

    if stored and stored not in _GENERIC_MIME_TYPES:
        # A common broken Telegram/server value is text/plain for binary media.
        # Use the known extension when their top-level families contradict.
        if guessed and stored.split("/", 1)[0] != guessed.split("/", 1)[0]:
            return guessed
        return stored

    if guessed:
        return guessed
    return "application/octet-stream"


def sniff_media_type(content: bytes, fallback: str = "application/octet-stream") -> str:
    """Detect common media formats from their magic bytes.

    Used for in-memory image responses where the bytes are already available.
    This repairs legacy rows with wrong names and MIME metadata without a DB
    migration.
    """
    head = bytes(content[:64])
    stripped = bytes(content[:512]).lstrip()

    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if head.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in {b"avif", b"avis"}:
            return "image/avif"
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic"
        return "video/mp4"
    if stripped.startswith(b"%PDF-"):
        return "application/pdf"
    lower = stripped[:200].lower()
    if lower.startswith(b"<svg") or (lower.startswith(b"<?xml") and b"<svg" in lower):
        return "image/svg+xml"

    return clean_mime_type(fallback) or "application/octet-stream"


def supports_inline_display(mime_type: str) -> bool:
    mime = clean_mime_type(mime_type)
    if not mime or mime in _UNSAFE_INLINE_TYPES:
        return False
    return (
        mime.startswith("image/")
        or mime.startswith("video/")
        or mime.startswith("audio/")
        or mime == "application/pdf"
        or mime in {"text/plain", "text/csv", "application/json"}
    )
