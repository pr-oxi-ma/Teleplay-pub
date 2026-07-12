"""Cloudflare edge-cache URL signing and internal touch verification."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode

from .config import get_settings

settings = get_settings()


def cache_key_for_values(file_unique_id: str, file_size: int) -> str:
    raw = (
        f"v{settings.media_cache_key_version}:"
        f"{file_unique_id}:{int(file_size)}"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def cache_key_for_file(file) -> str:
    return cache_key_for_values(file.file_unique_id, file.file_size)


def _signature_message(
    file_id: int,
    cache_key: str,
    file_size: int,
    expires: int,
    token_id: str,
) -> str:
    return (
        f"{settings.media_cache_key_version}.{int(file_id)}.{cache_key}."
        f"{int(file_size)}.{int(expires)}.{token_id}"
    )


def sign_edge_request(
    file_id: int,
    cache_key: str,
    file_size: int,
    expires: int,
    token_id: str,
) -> str:
    message = _signature_message(file_id, cache_key, file_size, expires, token_id)
    return hmac.new(
        settings.cloudflare_edge_signing_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_edge_stream_url(file, *, force_download: bool = False) -> str | None:
    """Return a short-lived signed Worker URL when edge caching is configured."""
    if not settings.cloudflare_cache_enabled:
        return None

    cache_key = cache_key_for_file(file)
    expires = int(time.time()) + max(300, settings.cloudflare_edge_url_ttl_seconds)
    token_id = secrets.token_hex(8)
    signature = sign_edge_request(
        file.id,
        cache_key,
        file.file_size,
        expires,
        token_id,
    )
    query_values = {
        "v": settings.media_cache_key_version,
        "size": int(file.file_size),
        "expires": expires,
        "token": token_id,
        "sig": signature,
    }
    if force_download:
        # This flag only changes Content-Disposition at the Worker. Access to the
        # immutable media object remains protected by the signed fields above.
        query_values["download"] = 1
    query = urlencode(query_values)
    base = settings.cloudflare_worker_origin.rstrip("/")
    return f"{base}/media/{int(file.id)}/{cache_key}?{query}"


def verify_edge_cache_key(file, supplied_cache_key: str) -> bool:
    return hmac.compare_digest(cache_key_for_file(file), supplied_cache_key)


def sign_touch(cache_key: str, timestamp: int) -> str:
    message = f"{cache_key}.{int(timestamp)}"
    return hmac.new(
        settings.cloudflare_touch_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_touch(cache_key: str, timestamp: int, supplied_signature: str) -> bool:
    now = int(time.time())
    if abs(now - int(timestamp)) > settings.cloudflare_edge_touch_max_skew_seconds:
        return False
    expected = sign_touch(cache_key, timestamp)
    return hmac.compare_digest(expected, supplied_signature.lower())
