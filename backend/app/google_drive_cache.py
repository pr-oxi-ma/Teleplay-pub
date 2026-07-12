"""Async Google Drive REST client for TelePlay's managed L2 media cache.

The client uses OAuth refresh-token credentials and Drive resumable uploads.
Upload session URLs and accepted offsets are persisted by the caller, allowing a
new Render process to continue a cache fill after a restart without holding a
complete media file in memory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
RANGE_RE = re.compile(r"bytes=0-(\d+)$")
CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.I)


class DriveCacheError(RuntimeError):
    """Base Drive cache failure."""


class DriveObjectMissing(DriveCacheError):
    """Catalog points at an object that no longer exists."""


class DriveRateLimited(DriveCacheError):
    """Drive requested retry/backoff or exhausted download quota."""


class DriveUploadSessionExpired(DriveCacheError):
    """A persisted resumable session can no longer be used."""


@dataclass(slots=True)
class DriveUploadResult:
    file_id: str
    size: int
    name: str


ProgressCallback = Callable[[str, int], Awaitable[None]]


class GoogleDriveCacheClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._access_token = ""
        self._access_token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return settings.google_drive_cache_enabled

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=20.0, read=180.0, write=180.0, pool=30.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                follow_redirects=True,
                headers={"User-Agent": "TelePlay-MediaCache/2"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _token(self, force_refresh: bool = False) -> str:
        now = time.time()
        if not force_refresh and self._access_token and now < self._access_token_expires_at - 90:
            return self._access_token

        async with self._token_lock:
            now = time.time()
            if not force_refresh and self._access_token and now < self._access_token_expires_at - 90:
                return self._access_token

            client = await self._http()
            response = await client.post(
                TOKEN_URL,
                data={
                    "client_id": settings.google_drive_client_id,
                    "client_secret": settings.google_drive_client_secret,
                    "refresh_token": settings.google_drive_refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if response.status_code >= 400:
                raise DriveCacheError(
                    f"Google OAuth refresh failed with HTTP {response.status_code}"
                )
            payload = response.json()
            token = str(payload.get("access_token") or "")
            if not token:
                raise DriveCacheError("Google OAuth response did not contain an access token")
            self._access_token = token
            self._access_token_expires_at = now + int(payload.get("expires_in") or 3600)
            return token

    async def _authorized_headers(self, force_refresh: bool = False) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._token(force_refresh)}"}

    @staticmethod
    def _error_reason(response: httpx.Response) -> str:
        try:
            payload = response.json()
            errors = payload.get("error", {}).get("errors") or []
            if errors and isinstance(errors[0], dict):
                return str(errors[0].get("reason") or "")
        except Exception:
            pass
        return ""

    @classmethod
    def _is_retryable_response(cls, response: httpx.Response) -> bool:
        if response.status_code in {429, 500, 502, 503, 504}:
            return True
        if response.status_code == 403:
            return cls._error_reason(response) in {
                "rateLimitExceeded",
                "userRateLimitExceeded",
                "sharingRateLimitExceeded",
                "downloadQuotaExceeded",
            }
        return False

    @classmethod
    def _raise_for_drive_response(cls, response: httpx.Response, operation: str) -> None:
        if response.status_code == 404:
            raise DriveObjectMissing(f"Drive object missing during {operation}")
        if cls._is_retryable_response(response):
            raise DriveRateLimited(
                f"Drive throttled {operation} with HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                detail = str(payload.get("error", {}).get("message") or "")
            except Exception:
                detail = response.text[:200]
            raise DriveCacheError(
                f"Drive {operation} failed with HTTP {response.status_code}"
                + (f": {detail}" if detail else "")
            )

    @staticmethod
    async def _sleep_backoff(attempt: int) -> None:
        delay = min(32.0, 0.75 * (2**attempt)) + random.random()
        await asyncio.sleep(delay)

    async def open_range(
        self,
        file_id: str,
        start: int,
        end: int,
        expected_size: int,
    ) -> httpx.Response:
        """Open and validate a Drive byte range before application headers are sent."""
        client = await self._http()
        url = f"{DRIVE_API}/files/{file_id}?alt=media&supportsAllDrives=true"
        full_request = start == 0 and end == expected_size - 1

        for attempt in range(2):
            headers = await self._authorized_headers(force_refresh=attempt == 1)
            headers.update(
                {
                    "Range": f"bytes={int(start)}-{int(end)}",
                    "Accept-Encoding": "identity",
                }
            )
            request = client.build_request("GET", url, headers=headers)
            response = await client.send(request, stream=True)
            if response.status_code == 401 and attempt == 0:
                await response.aclose()
                continue
            self._raise_for_drive_response(response, "download")

            expected_length = end - start + 1
            if response.status_code == 206:
                content_range = response.headers.get("Content-Range", "")
                match = CONTENT_RANGE_RE.fullmatch(content_range.strip())
                if not match:
                    await response.aclose()
                    raise DriveCacheError("Drive returned an invalid Content-Range")
                got_start, got_end, got_total = match.groups()
                if int(got_start) != start or int(got_end) != end:
                    await response.aclose()
                    raise DriveCacheError("Drive returned the wrong byte range")
                if got_total != "*" and int(got_total) != expected_size:
                    await response.aclose()
                    raise DriveCacheError("Drive returned an unexpected object size")
            elif not (response.status_code == 200 and full_request):
                await response.aclose()
                raise DriveCacheError("Drive ignored a required byte-range request")

            length_header = response.headers.get("Content-Length")
            if length_header and int(length_header) != expected_length:
                await response.aclose()
                raise DriveCacheError("Drive returned an unexpected Content-Length")
            return response

        raise DriveCacheError("Drive download authorization failed")

    async def create_resumable_upload(
        self,
        *,
        name: str,
        size: int,
        mime_type: str,
        cache_key: str,
        cache_format: str = "e1",
    ) -> str:
        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": mime_type or "application/octet-stream",
            "appProperties": {
                "teleplayCache": cache_format,
                "cacheKey": cache_key,
            },
        }
        if settings.google_drive_cache_folder_id:
            metadata["parents"] = [settings.google_drive_cache_folder_id]

        client = await self._http()
        url = (
            f"{DRIVE_UPLOAD_API}/files?uploadType=resumable"
            "&supportsAllDrives=true&fields=id,size,name"
        )
        for attempt in range(2):
            headers = await self._authorized_headers(force_refresh=attempt == 1)
            headers.update(
                {
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Type": mime_type or "application/octet-stream",
                    "X-Upload-Content-Length": str(int(size)),
                }
            )
            response = await client.post(url, headers=headers, content=json.dumps(metadata))
            if response.status_code == 401 and attempt == 0:
                continue
            self._raise_for_drive_response(response, "resumable upload creation")
            location = response.headers.get("Location")
            if not location:
                raise DriveCacheError("Drive did not return a resumable upload URL")
            return location
        raise DriveCacheError("Drive upload authorization failed")

    async def query_resumable_offset(
        self,
        upload_url: str,
        size: int,
    ) -> tuple[int, DriveUploadResult | None]:
        client = await self._http()
        for attempt in range(2):
            headers = await self._authorized_headers(force_refresh=attempt == 1)
            headers.update({"Content-Length": "0", "Content-Range": f"bytes */{size}"})
            response = await client.put(upload_url, headers=headers, content=b"")
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code in {404, 410}:
                raise DriveUploadSessionExpired("Drive resumable upload session expired")
            if response.status_code in {200, 201}:
                payload = response.json()
                return size, DriveUploadResult(
                    file_id=str(payload.get("id") or ""),
                    size=int(payload.get("size") or size),
                    name=str(payload.get("name") or ""),
                )
            if response.status_code == 308:
                accepted = response.headers.get("Range", "")
                if not accepted:
                    return 0, None
                match = RANGE_RE.fullmatch(accepted.strip())
                if not match:
                    raise DriveCacheError("Drive returned an invalid resumable Range header")
                return int(match.group(1)) + 1, None
            self._raise_for_drive_response(response, "resumable upload status")
        raise DriveCacheError("Drive upload status authorization failed")

    async def upload_resumable(
        self,
        *,
        name: str,
        size: int,
        mime_type: str,
        cache_key: str,
        chunks: AsyncIterator[bytes],
        upload_url: str | None,
        start_offset: int,
        on_progress: ProgressCallback,
        cache_format: str = "e1",
    ) -> DriveUploadResult:
        """Upload bytes beginning at ``start_offset`` and checkpoint every piece."""
        if size <= 0:
            raise DriveCacheError("Cannot upload an empty cache object")

        if upload_url:
            server_offset, completed = await self.query_resumable_offset(upload_url, size)
            if completed is not None:
                if not completed.file_id:
                    raise DriveCacheError("Completed Drive upload did not return a file ID")
                return completed
            start_offset = server_offset
        else:
            upload_url = await self.create_resumable_upload(
                name=name,
                size=size,
                mime_type=mime_type,
                cache_key=cache_key,
                cache_format=cache_format,
            )
            start_offset = 0
            await on_progress(upload_url, 0)

        configured = max(256 * 1024, settings.google_drive_upload_chunk_mb * 1024 * 1024)
        piece_size = configured - (configured % (256 * 1024))
        piece_size = max(256 * 1024, piece_size)
        client = await self._http()
        buffer = bytearray()
        uploaded = int(start_offset)

        async def send_piece(piece: bytes) -> DriveUploadResult | None:
            nonlocal uploaded
            start = uploaded
            end = start + len(piece) - 1
            force_refresh = False
            for attempt in range(7):
                try:
                    headers = await self._authorized_headers(force_refresh=force_refresh)
                    force_refresh = False
                    headers.update(
                        {
                            "Content-Length": str(len(piece)),
                            "Content-Type": mime_type or "application/octet-stream",
                            "Content-Range": f"bytes {start}-{end}/{size}",
                        }
                    )
                    response = await client.put(upload_url, headers=headers, content=piece)
                except (httpx.TimeoutException, httpx.TransportError):
                    if attempt >= 6:
                        raise
                    await self._sleep_backoff(attempt)
                    continue

                if response.status_code in {404, 410}:
                    raise DriveUploadSessionExpired("Drive resumable upload session expired")
                if response.status_code == 308:
                    accepted = response.headers.get("Range", "")
                    if accepted:
                        match = RANGE_RE.fullmatch(accepted.strip())
                        if not match or int(match.group(1)) != end:
                            raise DriveCacheError("Drive accepted an unexpected upload offset")
                    uploaded = end + 1
                    await on_progress(upload_url, uploaded)
                    return None
                if response.status_code in {200, 201}:
                    payload = response.json()
                    uploaded = end + 1
                    await on_progress(upload_url, uploaded)
                    result = DriveUploadResult(
                        file_id=str(payload.get("id") or ""),
                        size=int(payload.get("size") or size),
                        name=str(payload.get("name") or name),
                    )
                    if not result.file_id:
                        raise DriveCacheError("Drive upload completed without a file ID")
                    return result
                if response.status_code == 401 or self._is_retryable_response(response):
                    if attempt >= 6:
                        self._raise_for_drive_response(response, "resumable upload")
                    force_refresh = response.status_code == 401
                    await self._sleep_backoff(attempt)
                    continue
                self._raise_for_drive_response(response, "resumable upload")
            raise DriveCacheError("Drive resumable upload retries exhausted")

        final_result: DriveUploadResult | None = None
        async for chunk in chunks:
            if not chunk:
                continue
            buffer.extend(chunk)
            while len(buffer) >= piece_size and uploaded + piece_size < size:
                piece = bytes(buffer[:piece_size])
                del buffer[:piece_size]
                result = await send_piece(piece)
                if result is not None:
                    final_result = result
                    break
            if final_result is not None:
                break

        if final_result is None and uploaded < size:
            needed = size - uploaded
            if len(buffer) < needed:
                raise DriveCacheError(
                    f"Telegram source ended early at {uploaded + len(buffer)} of {size} bytes"
                )
            final_result = await send_piece(bytes(buffer[:needed]))

        if final_result is None:
            # A process may have resumed exactly at EOF. Ask Drive for the final object.
            _, final_result = await self.query_resumable_offset(upload_url, size)
        if final_result is None:
            raise DriveCacheError("Drive upload did not produce a completed object")
        return final_result

    async def delete_file(self, file_id: str) -> None:
        client = await self._http()
        url = f"{DRIVE_API}/files/{file_id}?supportsAllDrives=true"
        for attempt in range(2):
            headers = await self._authorized_headers(force_refresh=attempt == 1)
            response = await client.delete(url, headers=headers)
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code == 404:
                return
            self._raise_for_drive_response(response, "delete")
            return
        raise DriveCacheError("Drive delete authorization failed")

    async def get_metadata(self, file_id: str) -> dict[str, Any]:
        client = await self._http()
        url = (
            f"{DRIVE_API}/files/{file_id}"
            "?supportsAllDrives=true&fields=id,size,name,mimeType,trashed,appProperties"
        )
        for attempt in range(2):
            headers = await self._authorized_headers(force_refresh=attempt == 1)
            response = await client.get(url, headers=headers)
            if response.status_code == 401 and attempt == 0:
                continue
            self._raise_for_drive_response(response, "metadata")
            return dict(response.json())
        raise DriveCacheError("Drive metadata authorization failed")


drive_cache_client = GoogleDriveCacheClient()
