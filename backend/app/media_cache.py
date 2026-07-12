"""Production-oriented managed Google Drive media cache.

Playback stays on the request path, while cache writes are represented by
restart-safe database jobs. Workers claim jobs with expiring leases, persist
Google resumable-session URLs and offsets, and can continue after a deployment
or process crash. The SQL database is the cache catalog; playback never scans a
Drive folder.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
import socket
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from typing import AsyncIterator

from sqlalchemy import and_, case, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from .config import get_settings
from .database import async_session
from .edge_cache import cache_key_for_values
from .google_drive_cache import (
    DriveCacheError,
    DriveObjectMissing,
    DriveRateLimited,
    DriveUploadResult,
    DriveUploadSessionExpired,
    GoogleDriveCacheClient,
    drive_cache_client,
)
from .models import (
    File,
    MediaCacheDailyUsage,
    MediaCacheEntry,
    MediaCacheJob,
    MediaCacheLock,
)
from .streaming import stream_file as telegram_stream_file
from .telegram import get_message_from_channel, tg_client
from .time_utils import utcnow
from .media_types import resolve_media_type
from .media_cache_crypto import (
    CRYPTO_VERSION,
    PLAIN_BLOCK_SIZE,
    TAG_SIZE,
    CacheCipher,
    CacheCryptoError,
    block_encrypted_offset,
    block_plain_length,
    decode_master_key,
    encrypted_range_for_plain_range,
    encrypted_size,
)

settings = get_settings()
logger = logging.getLogger(__name__)
CACHE_FILL_START_DELAY_SECONDS = 120


@dataclass(slots=True, frozen=True)
class CacheSource:
    cache_key: str
    file_unique_id: str
    channel_message_id: int
    file_name: str
    file_size: int
    mime_type: str
    file_type: str


@dataclass(slots=True, frozen=True)
class ReadyCacheObject:
    cache_key: str
    drive_file_id: str
    size_bytes: int
    mime_type: str
    encryption_version: int
    encryption_nonce_prefix: str | None
    encrypted_size_bytes: int


@dataclass(slots=True, frozen=True)
class ClaimedJob:
    id: int
    cache_key: str
    job_type: str
    resumable_upload_url: str | None
    bytes_uploaded: int
    attempts: int
    source_drive_file_id: str | None
    source: CacheSource


class EncryptedDriveRange:
    """Response-like range stream that decrypts one authenticated block at a time.

    The first block is authenticated before FastAPI sends response headers.  A
    corrupted object or wrong master key can therefore fall back to Telegram
    instead of producing a truncated 206 response.
    """

    def __init__(self, response, cipher: CacheCipher, first_block: int, last_block: int,
                 plain_start: int, plain_end: int, encrypted_bytes: int) -> None:
        self._response = response
        self._cipher = cipher
        self._first = first_block
        self._last = last_block
        self._plain_start = plain_start
        self._plain_end = plain_end
        self._iterator = response.aiter_bytes(256 * 1024).__aiter__()
        self._buffer = bytearray()
        self._next_block = first_block
        self._prefetched: bytes | None = None
        self.drive_bytes_read = encrypted_bytes

    def _slice_plaintext(self, block_index: int, plaintext: bytes) -> bytes:
        block_start = block_index * PLAIN_BLOCK_SIZE
        left = max(0, self._plain_start - block_start)
        right = min(len(plaintext), self._plain_end - block_start + 1)
        return plaintext[left:right] if right > left else b""

    async def _read_encrypted_block(self, block_index: int) -> bytes:
        needed = block_plain_length(self._cipher.plain_size, block_index) + TAG_SIZE
        while len(self._buffer) < needed:
            try:
                chunk = await self._iterator.__anext__()
            except StopAsyncIteration as exc:
                raise DriveCacheError(
                    "Encrypted Drive object ended before the requested range"
                ) from exc
            if chunk:
                self._buffer.extend(chunk)
        encrypted_block = bytes(self._buffer[:needed])
        del self._buffer[:needed]
        return encrypted_block

    async def prepare(self) -> None:
        try:
            encrypted_block = await self._read_encrypted_block(self._first)
            plaintext = self._cipher.decrypt_block(self._first, encrypted_block)
        except (CacheCryptoError, DriveCacheError) as exc:
            await self._response.aclose()
            raise DriveCacheError("Encrypted Drive cache authentication failed") from exc
        self._prefetched = self._slice_plaintext(self._first, plaintext)
        self._next_block = self._first + 1

    async def aiter_bytes(self, _chunk_size: int = 256 * 1024):
        if self._prefetched is None:
            await self.prepare()
        if self._prefetched:
            yield self._prefetched
        while self._next_block <= self._last:
            block_index = self._next_block
            encrypted_block = await self._read_encrypted_block(block_index)
            try:
                plaintext = self._cipher.decrypt_block(block_index, encrypted_block)
            except CacheCryptoError as exc:
                raise DriveCacheError("Encrypted Drive cache authentication failed") from exc
            selected = self._slice_plaintext(block_index, plaintext)
            if selected:
                yield selected
            self._next_block += 1

    async def aclose(self) -> None:
        await self._response.aclose()


class JobLeaseLost(DriveCacheError):
    pass


class ManagedMediaCache:
    def __init__(self, drive: GoogleDriveCacheClient) -> None:
        self.drive = drive
        self.instance_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
        self._worker_tasks: list[asyncio.Task] = []
        self._cleanup_task: asyncio.Task | None = None
        self._stopping = False
        self._drive_touches: dict[str, float] = {}
        self._drive_failures: deque[float] = deque()
        self._drive_bypass_until = 0.0
        self._daily_usage_date = ""
        self._daily_drive_bytes = 0
        self._master_key = (
            decode_master_key(settings.media_cache_master_key_base64)
            if settings.google_drive_cache_enabled
            else b""
        )

    @property
    def enabled(self) -> bool:
        return settings.google_drive_cache_enabled

    @staticmethod
    def source_from_file(file) -> CacheSource:
        return CacheSource(
            cache_key=cache_key_for_values(file.file_unique_id, file.file_size),
            file_unique_id=str(file.file_unique_id),
            channel_message_id=int(file.channel_message_id),
            file_name=str(file.file_name),
            file_size=int(file.file_size),
            mime_type=resolve_media_type(file.file_name, file.mime_type, file.file_type),
            file_type=str(file.file_type or "document"),
        )

    async def start(self) -> None:
        if not self.enabled or self._worker_tasks:
            return
        self._stopping = False
        await self._load_daily_usage()
        for index in range(max(1, settings.google_drive_max_concurrent_fills)):
            self._worker_tasks.append(
                asyncio.create_task(
                    self._job_worker_loop(index),
                    name=f"media-cache-worker-{index}",
                )
            )
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="media-cache-cleanup",
        )
        logger.info(
            "Managed Drive cache enabled: budget=%sGB workers=%s instance=%s",
            settings.google_drive_cache_budget_gb,
            len(self._worker_tasks),
            self.instance_id,
        )

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._worker_tasks)
        self._worker_tasks.clear()
        if self._cleanup_task is not None:
            tasks.append(self._cleanup_task)
            self._cleanup_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.drive.close()

    @staticmethod
    def _usage_date() -> str:
        return utcnow().date().isoformat()

    async def _load_daily_usage(self) -> None:
        date_key = self._usage_date()
        async with async_session() as db:
            row = await db.get(MediaCacheDailyUsage, date_key)
            self._daily_usage_date = date_key
            self._daily_drive_bytes = int(row.drive_bytes if row else 0)

    async def _increment_daily_usage(
        self,
        *,
        drive_bytes: int = 0,
        telegram_bytes: int = 0,
        edge_hits: int = 0,
    ) -> None:
        if not any((drive_bytes, telegram_bytes, edge_hits)):
            return
        date_key = self._usage_date()
        for attempt in range(2):
            async with async_session() as db:
                row = await db.get(MediaCacheDailyUsage, date_key)
                if row is None:
                    row = MediaCacheDailyUsage(usage_date=date_key)
                    db.add(row)
                row.drive_bytes = int(row.drive_bytes or 0) + max(0, int(drive_bytes))
                row.telegram_bytes = int(row.telegram_bytes or 0) + max(0, int(telegram_bytes))
                row.edge_hits = int(row.edge_hits or 0) + max(0, int(edge_hits))
                row.updated_at = utcnow()
                try:
                    await db.commit()
                    break
                except IntegrityError:
                    await db.rollback()
                    if attempt:
                        raise
        if self._daily_usage_date != date_key:
            self._daily_usage_date = date_key
            self._daily_drive_bytes = 0
        self._daily_drive_bytes += max(0, int(drive_bytes))

    def _drive_reads_allowed(self) -> bool:
        loop_time = asyncio.get_running_loop().time()
        if loop_time < self._drive_bypass_until:
            return False
        limit_gb = settings.google_drive_daily_egress_soft_limit_gb
        if limit_gb > 0 and self._daily_drive_bytes >= limit_gb * 1024**3:
            return False
        return True

    def _note_drive_failure(self) -> None:
        now = asyncio.get_running_loop().time()
        self._drive_failures.append(now)
        while self._drive_failures and now - self._drive_failures[0] > 60:
            self._drive_failures.popleft()
        if len(self._drive_failures) >= settings.google_drive_circuit_failure_threshold:
            self._drive_bypass_until = max(
                self._drive_bypass_until,
                now + settings.google_drive_circuit_open_seconds,
            )
            self._drive_failures.clear()
            logger.warning(
                "Drive read circuit opened for %ss",
                settings.google_drive_circuit_open_seconds,
            )

    def _note_drive_success(self) -> None:
        if self._drive_failures:
            self._drive_failures.popleft()

    async def _get_or_create_entry(self, db, source: CacheSource) -> MediaCacheEntry:
        result = await db.execute(
            select(MediaCacheEntry).where(MediaCacheEntry.cache_key == source.cache_key)
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            entry = MediaCacheEntry(
                cache_key=source.cache_key,
                cache_version=settings.media_cache_key_version,
                file_unique_id=source.file_unique_id,
                source_message_id=source.channel_message_id,
                file_name=source.file_name,
                mime_type=source.mime_type,
                file_type=source.file_type,
                size_bytes=source.file_size,
                status="observed",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            db.add(entry)
            await db.flush()
        else:
            entry.source_message_id = source.channel_message_id
            entry.file_name = source.file_name
            entry.mime_type = source.mime_type
            entry.file_type = source.file_type
            entry.size_bytes = source.file_size
            entry.updated_at = utcnow()
        return entry

    async def record_telegram_access(self, file, bytes_served: int) -> None:
        """Record uncached bytes and durably queue a fill once admission is met."""
        if not self.enabled or bytes_served <= 0:
            return
        source = self.source_from_file(file)
        now = utcnow()
        for attempt in range(2):
            async with async_session() as db:
                try:
                    entry = await self._get_or_create_entry(db, source)
                    entry.telegram_hit_count = int(entry.telegram_hit_count or 0) + 1
                    entry.telegram_bytes_served = int(entry.telegram_bytes_served or 0) + int(bytes_served)
                    entry.last_telegram_access_at = now
                    entry.last_access_at = now

                    if entry.status == "evicting" and entry.drive_file_id:
                        entry.status = "ready"
                        entry.delete_after = None
                        entry.eviction_reason = None

                    if self._admission_met(entry):
                        await self._queue_fill_in_session(db, entry, now)
                    await db.commit()
                    break
                except IntegrityError:
                    await db.rollback()
                    if attempt:
                        raise
        await self._increment_daily_usage(telegram_bytes=bytes_served)

    def _admission_met(self, entry: MediaCacheEntry) -> bool:
        size = int(entry.size_bytes or 0)
        if size <= 0:
            return False
        max_bytes = settings.google_drive_max_cache_file_gb * 1024**3
        if max_bytes > 0 and size > max_bytes:
            return False
        if entry.pinned:
            return True
        small_limit = settings.google_drive_admission_small_file_mb * 1024**2
        if size <= small_limit:
            return True
        bytes_limit = settings.google_drive_admission_bytes_mb * 1024**2
        ratio_limit = max(1, int(size * settings.google_drive_admission_ratio))
        threshold = min(bytes_limit, ratio_limit)
        return int(entry.telegram_bytes_served or 0) >= threshold

    async def _queue_fill_in_session(self, db, entry: MediaCacheEntry, now) -> None:
        if entry.status == "ready" and entry.drive_file_id:
            return
        if entry.status in {"uploading", "deleting"}:
            return
        if entry.next_retry_at and entry.next_retry_at > now:
            return

        result = await db.execute(
            select(MediaCacheJob).where(MediaCacheJob.cache_key == entry.cache_key)
        )
        job = result.scalar_one_or_none()
        if job is None:
            job = MediaCacheJob(
                cache_key=entry.cache_key,
                job_type="fill",
                status="queued",
                next_attempt_at=now + timedelta(seconds=CACHE_FILL_START_DELAY_SECONDS),
                created_at=now,
                updated_at=now,
            )
            db.add(job)
        elif job.status == "queued" and job.next_attempt_at and job.next_attempt_at > now:
            entry.status = "queued"
            return
        elif job.status not in {"leased"} or (
            job.lease_expires_at is not None and job.lease_expires_at <= now
        ):
            if job.attempts >= settings.google_drive_job_max_attempts:
                # A new real access is evidence that the object remains useful;
                # allow another retry cycle instead of permanently poisoning it.
                job.attempts = 0
            job.job_type = "fill"
            job.status = "queued"
            job.next_attempt_at = now + timedelta(seconds=CACHE_FILL_START_DELAY_SECONDS)
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
        entry.status = "queued"
        entry.next_retry_at = None
        entry.delete_after = None
        entry.eviction_reason = None

    async def record_edge_touch(self, cache_key: str) -> bool:
        if not self.enabled:
            return False
        now = utcnow()
        async with async_session() as db:
            result = await db.execute(
                select(MediaCacheEntry).where(MediaCacheEntry.cache_key == cache_key)
            )
            entry = result.scalar_one_or_none()
            if entry is None:
                return False
            entry.edge_hit_count = int(entry.edge_hit_count or 0) + 1
            entry.last_edge_access_at = now
            entry.last_access_at = now
            if entry.status == "evicting" and entry.drive_file_id:
                entry.status = "ready"
                entry.delete_after = None
                entry.eviction_reason = None
            await db.commit()
        await self._increment_daily_usage(edge_hits=1)
        return True

    async def get_ready(self, file) -> ReadyCacheObject | None:
        if not self.enabled or not self._drive_reads_allowed():
            return None
        cache_key = cache_key_for_values(file.file_unique_id, file.file_size)
        now = utcnow()
        lease_until = now + timedelta(seconds=settings.google_drive_read_lease_seconds)
        async with async_session() as db:
            result = await db.execute(
                select(MediaCacheEntry).where(MediaCacheEntry.cache_key == cache_key)
            )
            entry = result.scalar_one_or_none()
            if entry is None or not entry.drive_file_id:
                return None
            if entry.status not in {"ready", "evicting", "migrating"}:
                return None

            values = {
                "status": "ready",
                "delete_after": None,
                "eviction_reason": None,
                "active_readers": MediaCacheEntry.active_readers + 1,
                "read_lease_until": lease_until,
                "updated_at": now,
            }
            loop_now = asyncio.get_running_loop().time()
            previous = self._drive_touches.get(cache_key, 0.0)
            if loop_now - previous >= settings.google_drive_access_touch_seconds:
                values.update(
                    {
                        "drive_hit_count": MediaCacheEntry.drive_hit_count + 1,
                        "last_drive_access_at": now,
                        "last_access_at": now,
                    }
                )
                self._drive_touches[cache_key] = loop_now

            acquired = await db.execute(
                update(MediaCacheEntry)
                .where(
                    MediaCacheEntry.id == entry.id,
                    MediaCacheEntry.status.in_(["ready", "evicting", "migrating"]),
                    MediaCacheEntry.drive_file_id.is_not(None),
                )
                .values(**values)
            )
            if acquired.rowcount != 1:
                await db.rollback()
                return None
            await db.commit()
            return ReadyCacheObject(
                cache_key=cache_key,
                drive_file_id=str(entry.drive_file_id),
                size_bytes=int(entry.size_bytes),
                mime_type=resolve_media_type(
                    file.file_name,
                    entry.mime_type or file.mime_type,
                    file.file_type,
                ),
                encryption_version=int(entry.encryption_version or 0),
                encryption_nonce_prefix=entry.encryption_nonce_prefix,
                encrypted_size_bytes=int(entry.encrypted_size_bytes or 0),
            )

    async def renew_drive_read(self, cache_key: str) -> None:
        """Extend an active read lease while a long Drive response is streaming."""
        now = utcnow()
        async with async_session() as db:
            await db.execute(
                update(MediaCacheEntry)
                .where(
                    MediaCacheEntry.cache_key == cache_key,
                    MediaCacheEntry.active_readers > 0,
                )
                .values(
                    read_lease_until=now
                    + timedelta(seconds=settings.google_drive_read_lease_seconds),
                    updated_at=now,
                )
            )
            await db.commit()

    async def release_drive_read(self, cache_key: str) -> None:
        """Release one reader without allowing a crashed process to block eviction forever."""
        now = utcnow()
        async with async_session() as db:
            await db.execute(
                update(MediaCacheEntry)
                .where(MediaCacheEntry.cache_key == cache_key)
                .values(
                    active_readers=case(
                        (MediaCacheEntry.active_readers > 0, MediaCacheEntry.active_readers - 1),
                        else_=0,
                    ),
                    read_lease_until=case(
                        (MediaCacheEntry.active_readers <= 1, None),
                        else_=MediaCacheEntry.read_lease_until,
                    ),
                    updated_at=now,
                )
            )
            await db.commit()

    async def open_range(self, ready: ReadyCacheObject, start: int, end: int):
        try:
            if ready.encryption_version == CRYPTO_VERSION:
                if not ready.encryption_nonce_prefix or ready.encrypted_size_bytes <= 0:
                    raise DriveCacheError("Encrypted cache metadata is incomplete")
                try:
                    nonce_prefix = base64.b64decode(
                        ready.encryption_nonce_prefix.encode("ascii"), validate=True
                    )
                except Exception as exc:
                    raise DriveCacheError("Encrypted cache nonce metadata is invalid") from exc
                cipher = CacheCipher.create(
                    master_key=self._master_key,
                    cache_key=ready.cache_key,
                    plain_size=ready.size_bytes,
                    nonce_prefix=nonce_prefix,
                )
                first, last, encrypted_start, encrypted_end = encrypted_range_for_plain_range(
                    ready.size_bytes, start, end
                )
                response = await self.drive.open_range(
                    ready.drive_file_id,
                    encrypted_start,
                    encrypted_end,
                    ready.encrypted_size_bytes,
                )
                encrypted_response = EncryptedDriveRange(
                    response,
                    cipher,
                    first,
                    last,
                    start,
                    end,
                    encrypted_end - encrypted_start + 1,
                )
                await encrypted_response.prepare()
                self._note_drive_success()
                return encrypted_response

            response = await self.drive.open_range(
                ready.drive_file_id,
                start,
                end,
                ready.size_bytes,
            )
            self._note_drive_success()
            return response
        except DriveObjectMissing:
            await self.release_drive_read(ready.cache_key)
            self._note_drive_failure()
            await self.mark_missing(ready.cache_key)
            raise
        except (DriveRateLimited, DriveCacheError):
            await self.release_drive_read(ready.cache_key)
            self._note_drive_failure()
            raise
        except Exception:
            await self.release_drive_read(ready.cache_key)
            raise

    async def record_drive_read(
        self,
        cache_key: str,
        bytes_served: int,
        *,
        truncated: bool = False,
    ) -> None:
        await self.release_drive_read(cache_key)
        if bytes_served > 0:
            await self._increment_daily_usage(drive_bytes=bytes_served)
        if not truncated:
            return
        async with async_session() as db:
            result = await db.execute(
                select(MediaCacheEntry).where(MediaCacheEntry.cache_key == cache_key)
            )
            entry = result.scalar_one_or_none()
            if entry is not None:
                entry.truncated_read_count = int(entry.truncated_read_count or 0) + 1
                entry.last_error = "Drive stream ended before the requested range completed"
                entry.updated_at = utcnow()
                await db.commit()
        self._note_drive_failure()

    async def mark_missing(self, cache_key: str) -> None:
        await self._mark_missing_and_queue(cache_key, "Drive object was not found during playback")

    async def _job_worker_loop(self, worker_index: int) -> None:
        while not self._stopping:
            try:
                claimed = await self._claim_job()
                if claimed is None:
                    await asyncio.sleep(settings.google_drive_job_poll_seconds)
                    continue
                await self._process_job(claimed)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Media cache worker %s failed", worker_index)
                await asyncio.sleep(settings.google_drive_job_poll_seconds)

    async def _claim_job(self) -> ClaimedJob | None:
        now = utcnow()
        lease_until = now + timedelta(seconds=settings.google_drive_job_lease_seconds)
        async with async_session() as db:
            result = await db.execute(
                select(MediaCacheJob.id)
                .where(
                    or_(
                        and_(
                            MediaCacheJob.status.in_(["queued", "retry"]),
                            or_(
                                MediaCacheJob.next_attempt_at.is_(None),
                                MediaCacheJob.next_attempt_at <= now,
                            ),
                        ),
                        and_(
                            MediaCacheJob.status == "leased",
                            MediaCacheJob.lease_expires_at.is_not(None),
                            MediaCacheJob.lease_expires_at <= now,
                        ),
                    )
                )
                .order_by(MediaCacheJob.created_at.asc())
                .limit(10)
            )
            candidate_ids = [int(row[0]) for row in result.all()]

        for job_id in candidate_ids:
            async with async_session() as db:
                claim = await db.execute(
                    update(MediaCacheJob)
                    .where(
                        MediaCacheJob.id == job_id,
                        or_(
                            and_(
                                MediaCacheJob.status.in_(["queued", "retry"]),
                                or_(
                                    MediaCacheJob.next_attempt_at.is_(None),
                                    MediaCacheJob.next_attempt_at <= now,
                                ),
                            ),
                            and_(
                                MediaCacheJob.status == "leased",
                                MediaCacheJob.lease_expires_at.is_not(None),
                                MediaCacheJob.lease_expires_at <= now,
                            ),
                        ),
                    )
                    .values(
                        status="leased",
                        lease_owner=self.instance_id,
                        lease_expires_at=lease_until,
                        attempts=MediaCacheJob.attempts + 1,
                        updated_at=now,
                    )
                )
                if claim.rowcount != 1:
                    await db.rollback()
                    continue
                await db.commit()
                row = await db.execute(
                    select(MediaCacheJob, MediaCacheEntry)
                    .join(MediaCacheEntry, MediaCacheEntry.cache_key == MediaCacheJob.cache_key)
                    .where(MediaCacheJob.id == job_id)
                )
                pair = row.one_or_none()
                if pair is None:
                    continue
                job, entry = pair
                source = CacheSource(
                    cache_key=entry.cache_key,
                    file_unique_id=entry.file_unique_id,
                    channel_message_id=int(entry.source_message_id),
                    file_name=entry.file_name,
                    file_size=int(entry.size_bytes),
                    mime_type=resolve_media_type(
                        entry.file_name,
                        entry.mime_type,
                        entry.file_type or "document",
                    ),
                    file_type=entry.file_type or "document",
                )
                return ClaimedJob(
                    id=int(job.id),
                    cache_key=job.cache_key,
                    job_type=str(job.job_type or "fill"),
                    resumable_upload_url=job.resumable_upload_url,
                    bytes_uploaded=int(job.bytes_uploaded or 0),
                    attempts=int(job.attempts or 0),
                    source_drive_file_id=str(entry.drive_file_id) if entry.drive_file_id else None,
                    source=source,
                )
        return None

    async def _lease_heartbeat(self, job_id: int) -> None:
        interval = max(20, settings.google_drive_job_lease_seconds // 3)
        try:
            while True:
                await asyncio.sleep(interval)
                async with async_session() as db:
                    result = await db.execute(
                        update(MediaCacheJob)
                        .where(
                            MediaCacheJob.id == job_id,
                            MediaCacheJob.status == "leased",
                            MediaCacheJob.lease_owner == self.instance_id,
                        )
                        .values(
                            lease_expires_at=utcnow()
                            + timedelta(seconds=settings.google_drive_job_lease_seconds),
                            updated_at=utcnow(),
                        )
                    )
                    await db.commit()
                    if result.rowcount != 1:
                        raise JobLeaseLost("Drive fill job lease was lost")
        except asyncio.CancelledError:
            return

    async def _checkpoint_job(self, job_id: int, upload_url: str, offset: int) -> None:
        now = utcnow()
        async with async_session() as db:
            result = await db.execute(
                update(MediaCacheJob)
                .where(
                    MediaCacheJob.id == job_id,
                    MediaCacheJob.status == "leased",
                    MediaCacheJob.lease_owner == self.instance_id,
                )
                .values(
                    resumable_upload_url=upload_url,
                    bytes_uploaded=int(offset),
                    lease_expires_at=now
                    + timedelta(seconds=settings.google_drive_job_lease_seconds),
                    updated_at=now,
                )
            )
            await db.commit()
            if result.rowcount != 1:
                raise JobLeaseLost("Drive fill job lease was lost")

    async def _source_chunks(self, source: CacheSource, start_offset: int) -> AsyncIterator[bytes]:
        """Yield plaintext Telegram bytes from an exact offset."""
        message = await get_message_from_channel(source.channel_message_id)
        if not message:
            raise DriveCacheError("Telegram storage message was not found")

        if source.file_type == "image":
            downloaded = await tg_client.download_media(message, in_memory=True)
            if downloaded is None:
                raise DriveCacheError("Telegram image download returned no data")
            if hasattr(downloaded, "getvalue"):
                content = downloaded.getvalue()
            elif isinstance(downloaded, (bytes, bytearray)):
                content = bytes(downloaded)
            else:
                downloaded.seek(0)
                content = downloaded.read()
            if len(content) != source.file_size:
                raise DriveCacheError(
                    f"Telegram image size {len(content)} did not match metadata {source.file_size}"
                )
            if start_offset < len(content):
                yield content[start_offset:]
            return

        if start_offset >= source.file_size:
            return
        async for chunk in telegram_stream_file(
            tg_client,
            message,
            start_offset,
            source.file_size - 1,
            concurrency=max(1, settings.google_drive_fill_telegram_concurrency),
        ):
            yield chunk

    async def _legacy_drive_chunks(
        self, drive_file_id: str, start_offset: int, plain_size: int
    ) -> AsyncIterator[bytes]:
        """Read a legacy plaintext Drive object without staging it on disk."""
        if start_offset >= plain_size:
            return
        response = await self.drive.open_range(
            drive_file_id, start_offset, plain_size - 1, plain_size
        )
        try:
            async for chunk in response.aiter_bytes(256 * 1024):
                if chunk:
                    yield chunk
        finally:
            await response.aclose()

    async def _ensure_encryption_nonce(self, cache_key: str) -> bytes:
        async with async_session() as db:
            result = await db.execute(
                select(MediaCacheEntry).where(MediaCacheEntry.cache_key == cache_key)
            )
            entry = result.scalar_one_or_none()
            if entry is None:
                raise DriveCacheError("Cache catalog row disappeared")
            if entry.encryption_nonce_prefix:
                try:
                    value = base64.b64decode(
                        entry.encryption_nonce_prefix.encode("ascii"), validate=True
                    )
                except Exception as exc:
                    raise DriveCacheError("Cache nonce metadata is invalid") from exc
                if len(value) != 8:
                    raise DriveCacheError("Cache nonce metadata has the wrong length")
                return value
            value = os.urandom(8)
            entry.encryption_nonce_prefix = base64.b64encode(value).decode("ascii")
            entry.encrypted_size_bytes = encrypted_size(int(entry.size_bytes or 0))
            entry.updated_at = utcnow()
            await db.commit()
            return value

    async def _encrypted_source_chunks(
        self,
        claimed: ClaimedJob,
        encrypted_offset: int,
        nonce_prefix: bytes,
    ) -> AsyncIterator[bytes]:
        """Encrypt plaintext source blocks and resume at any Drive upload offset."""
        total_encrypted = encrypted_size(claimed.source.file_size)
        if encrypted_offset >= total_encrypted:
            return
        block_span = PLAIN_BLOCK_SIZE + TAG_SIZE
        block_index = encrypted_offset // block_span
        offset_inside_ciphertext = encrypted_offset - block_encrypted_offset(block_index)
        plain_offset = block_index * PLAIN_BLOCK_SIZE
        cipher = CacheCipher.create(
            master_key=self._master_key,
            cache_key=claimed.cache_key,
            plain_size=claimed.source.file_size,
            nonce_prefix=nonce_prefix,
        )

        if claimed.job_type == "migrate":
            if not claimed.source_drive_file_id:
                raise DriveObjectMissing("Legacy Drive object is missing")
            source_iterator = self._legacy_drive_chunks(
                claimed.source_drive_file_id, plain_offset, claimed.source.file_size
            )
        else:
            source_iterator = self._source_chunks(claimed.source, plain_offset)

        buffer = bytearray()
        first_output = True
        async for chunk in source_iterator:
            if chunk:
                buffer.extend(chunk)
            while block_index * PLAIN_BLOCK_SIZE < claimed.source.file_size:
                needed = block_plain_length(claimed.source.file_size, block_index)
                if len(buffer) < needed:
                    break
                plaintext = bytes(buffer[:needed])
                del buffer[:needed]
                encrypted_block = cipher.encrypt_block(block_index, plaintext)
                if first_output:
                    if offset_inside_ciphertext > len(encrypted_block):
                        raise DriveCacheError("Resumable offset is outside an encrypted block")
                    encrypted_block = encrypted_block[offset_inside_ciphertext:]
                    first_output = False
                if encrypted_block:
                    yield encrypted_block
                block_index += 1
        if block_index * PLAIN_BLOCK_SIZE < claimed.source.file_size or buffer:
            raise DriveCacheError("Plaintext cache source ended before encryption completed")

    async def _set_entry_uploading(self, cache_key: str, job_type: str) -> None:
        async with async_session() as db:
            await db.execute(
                update(MediaCacheEntry)
                .where(MediaCacheEntry.cache_key == cache_key)
                .values(
                    status="migrating" if job_type == "migrate" else "uploading",
                    upload_started_at=utcnow(),
                    updated_at=utcnow(),
                    last_error=None,
                    delete_after=None,
                    eviction_reason=None,
                )
            )
            await db.commit()

    async def _migration_source_missing(self, claimed: ClaimedJob) -> None:
        """Convert a manually deleted legacy-object migration into a Telegram fill."""
        now = utcnow()
        async with async_session() as db:
            entry_result = await db.execute(
                select(MediaCacheEntry).where(MediaCacheEntry.cache_key == claimed.cache_key)
            )
            entry = entry_result.scalar_one_or_none()
            job = await db.get(MediaCacheJob, claimed.id)
            if entry is not None:
                entry.drive_file_id = None
                entry.legacy_drive_file_id = None
                entry.encryption_version = 0
                entry.encryption_nonce_prefix = None
                entry.encrypted_size_bytes = 0
                entry.status = "queued"
                entry.last_error = "Legacy Drive object was manually deleted; rebuilding encrypted cache"
                entry.last_verified_at = now
                entry.updated_at = now
            if job is not None:
                job.job_type = "fill"
                job.status = "queued"
                job.resumable_upload_url = None
                job.bytes_uploaded = 0
                job.attempts = 0
                job.next_attempt_at = now + timedelta(seconds=CACHE_FILL_START_DELAY_SECONDS)
                job.lease_owner = None
                job.lease_expires_at = None
                job.last_error = None
                job.updated_at = now
            await db.commit()

    async def _process_job(self, claimed: ClaimedJob) -> None:
        heartbeat = asyncio.create_task(
            self._lease_heartbeat(claimed.id),
            name=f"media-cache-heartbeat-{claimed.id}",
        )
        uploaded_file_id: str | None = None
        committed = False
        can_delete_legacy = False
        try:
            await self._set_entry_uploading(claimed.cache_key, claimed.job_type)
            nonce_prefix = await self._ensure_encryption_nonce(claimed.cache_key)
            encrypted_total = encrypted_size(claimed.source.file_size)
            upload_url = claimed.resumable_upload_url
            offset = claimed.bytes_uploaded
            completed: DriveUploadResult | None = None

            if upload_url:
                try:
                    offset, completed = await self.drive.query_resumable_offset(
                        upload_url, encrypted_total
                    )
                    await self._checkpoint_job(claimed.id, upload_url, offset)
                except DriveUploadSessionExpired:
                    upload_url = None
                    offset = 0
                    await self._checkpoint_job(claimed.id, "", 0)

            opaque_name = f"tp-e1-{uuid.uuid4().hex}.bin"
            if completed is None:
                if not upload_url:
                    upload_url = await self.drive.create_resumable_upload(
                        name=opaque_name,
                        size=encrypted_total,
                        mime_type="application/octet-stream",
                        cache_key=claimed.cache_key,
                        cache_format="e1",
                    )
                    offset = 0
                    await self._checkpoint_job(claimed.id, upload_url, 0)

                completed = await self.drive.upload_resumable(
                    name=opaque_name,
                    size=encrypted_total,
                    mime_type="application/octet-stream",
                    cache_key=claimed.cache_key,
                    chunks=self._encrypted_source_chunks(claimed, offset, nonce_prefix),
                    upload_url=upload_url,
                    start_offset=offset,
                    on_progress=lambda url, accepted: self._checkpoint_job(
                        claimed.id, url, accepted
                    ),
                    cache_format="e1",
                )

            uploaded_file_id = completed.file_id
            if int(completed.size or encrypted_total) != encrypted_total:
                raise DriveCacheError("Encrypted Drive object size verification failed")

            old_plaintext_id = (
                claimed.source_drive_file_id
                if claimed.job_type == "migrate" and claimed.source_drive_file_id != completed.file_id
                else None
            )
            now = utcnow()
            async with async_session() as db:
                entry_result = await db.execute(
                    select(MediaCacheEntry).where(
                        MediaCacheEntry.cache_key == claimed.cache_key
                    )
                )
                entry = entry_result.scalar_one_or_none()
                if entry is None:
                    await self.drive.delete_file(completed.file_id)
                    return
                entry.legacy_drive_file_id = old_plaintext_id
                can_delete_legacy = bool(
                    int(entry.active_readers or 0) <= 0
                    or entry.read_lease_until is None
                    or entry.read_lease_until <= now
                )
                entry.drive_file_id = completed.file_id
                entry.encryption_version = CRYPTO_VERSION
                entry.encryption_nonce_prefix = base64.b64encode(nonce_prefix).decode("ascii")
                entry.encrypted_size_bytes = encrypted_total
                entry.status = "ready"
                entry.upload_completed_at = now
                entry.last_access_at = now
                entry.last_verified_at = now
                entry.next_retry_at = None
                entry.last_error = None
                entry.updated_at = now
                job_result = await db.execute(
                    select(MediaCacheJob).where(
                        MediaCacheJob.id == claimed.id,
                        MediaCacheJob.lease_owner == self.instance_id,
                    )
                )
                job = job_result.scalar_one_or_none()
                if job is None:
                    raise JobLeaseLost("Drive fill completed after lease loss")
                job.status = "completed"
                job.lease_owner = None
                job.lease_expires_at = None
                job.bytes_uploaded = encrypted_total
                job.resumable_upload_url = None
                job.last_error = None
                job.updated_at = now
                await db.commit()
                committed = True

            if old_plaintext_id and can_delete_legacy:
                try:
                    await self.drive.delete_file(old_plaintext_id)
                    async with async_session() as db:
                        await db.execute(
                            update(MediaCacheEntry)
                            .where(
                                MediaCacheEntry.cache_key == claimed.cache_key,
                                MediaCacheEntry.legacy_drive_file_id == old_plaintext_id,
                            )
                            .values(legacy_drive_file_id=None, updated_at=utcnow())
                        )
                        await db.commit()
                except Exception as exc:
                    logger.warning("Encrypted migration succeeded but old plaintext cleanup failed: %s", exc)

            logger.info(
                "Encrypted Drive cache %s complete: key=%s plain=%s encrypted=%s",
                "migration" if claimed.job_type == "migrate" else "fill",
                claimed.cache_key[:12],
                claimed.source.file_size,
                encrypted_total,
            )
        except DriveObjectMissing as exc:
            if claimed.job_type == "migrate":
                logger.warning("Legacy Drive cache object missing for %s; rebuilding from Telegram", claimed.cache_key[:12])
                await self._migration_source_missing(claimed)
            else:
                await self._fail_job(claimed, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Drive cache job failed for %s: %s", claimed.cache_key[:12], exc)
            if uploaded_file_id and not committed:
                try:
                    await self.drive.delete_file(uploaded_file_id)
                except Exception:
                    logger.exception("Could not delete uncatalogued Drive object %s", uploaded_file_id)
            await self._fail_job(claimed, exc)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _fail_job(self, claimed: ClaimedJob, error: Exception) -> None:
        now = utcnow()
        attempts = max(1, claimed.attempts)
        retry_minutes = min(24 * 60, 5 * (2 ** min(attempts - 1, 8)))
        terminal = attempts >= settings.google_drive_job_max_attempts
        async with async_session() as db:
            job_result = await db.execute(
                select(MediaCacheJob).where(
                    MediaCacheJob.id == claimed.id,
                    MediaCacheJob.lease_owner == self.instance_id,
                )
            )
            job = job_result.scalar_one_or_none()
            if job is not None:
                if isinstance(error, DriveUploadSessionExpired):
                    job.resumable_upload_url = None
                    job.bytes_uploaded = 0
                job.status = "failed" if terminal else "retry"
                job.next_attempt_at = None if terminal else now + timedelta(minutes=retry_minutes)
                job.lease_owner = None
                job.lease_expires_at = None
                job.last_error = str(error)[:1000]
                job.updated_at = now
            entry_result = await db.execute(
                select(MediaCacheEntry).where(
                    MediaCacheEntry.cache_key == claimed.cache_key
                )
            )
            entry = entry_result.scalar_one_or_none()
            if entry is not None:
                if claimed.job_type == "migrate" and entry.drive_file_id:
                    # Keep the verified legacy object readable until migration succeeds.
                    entry.status = "ready"
                else:
                    entry.status = "failed" if terminal else "queued"
                entry.failure_count = int(entry.failure_count or 0) + 1
                entry.next_retry_at = None if terminal else now + timedelta(minutes=retry_minutes)
                entry.last_error = str(error)[:1000]
                entry.updated_at = now
            await db.commit()

    async def _cleanup_loop(self) -> None:
        try:
            await asyncio.sleep(settings.google_drive_cleanup_start_delay_seconds)
            while not self._stopping:
                try:
                    await self.run_cleanup()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Google Drive cache cleanup failed")
                await asyncio.sleep(settings.google_drive_cleanup_interval_seconds)
        except asyncio.CancelledError:
            return

    async def _acquire_lock(self, lock_key: str, ttl_seconds: int) -> bool:
        now = utcnow()
        expires = now + timedelta(seconds=ttl_seconds)
        for attempt in range(2):
            async with async_session() as db:
                row = await db.get(MediaCacheLock, lock_key)
                if row is None:
                    db.add(
                        MediaCacheLock(
                            lock_key=lock_key,
                            owner=self.instance_id,
                            expires_at=expires,
                            updated_at=now,
                        )
                    )
                elif row.owner == self.instance_id or row.expires_at <= now:
                    row.owner = self.instance_id
                    row.expires_at = expires
                    row.updated_at = now
                else:
                    return False
                try:
                    await db.commit()
                    return True
                except IntegrityError:
                    await db.rollback()
                    if attempt:
                        return False
        return False

    async def _release_lock(self, lock_key: str) -> None:
        async with async_session() as db:
            await db.execute(
                delete(MediaCacheLock).where(
                    MediaCacheLock.lock_key == lock_key,
                    MediaCacheLock.owner == self.instance_id,
                )
            )
            await db.commit()

    @staticmethod
    def _last_effective_access(entry: MediaCacheEntry):
        values = [
            entry.last_access_at,
            entry.last_edge_access_at,
            entry.last_drive_access_at,
            entry.last_telegram_access_at,
            entry.upload_completed_at,
            entry.created_at,
        ]
        return max(value for value in values if value is not None)

    @classmethod
    def _eviction_score(cls, entry: MediaCacheEntry, now) -> float:
        age_days = max(
            0.0,
            (now - cls._last_effective_access(entry)).total_seconds() / 86400,
        )
        size_gb = max(0.01, int(entry.size_bytes or 0) / 1024**3)
        popularity = (
            1
            + max(0, int(entry.edge_hit_count or 0)) * 5
            + max(0, int(entry.drive_hit_count or 0)) * 3
            + max(0, int(entry.telegram_hit_count or 0))
        )
        failure_bonus = 10_000 if entry.status in {"failed", "missing"} else 0
        return (((age_days + 1.0) * math.sqrt(size_gb)) / popularity) + failure_bonus

    async def _queue_migration(self, cache_key: str) -> None:
        now = utcnow()
        async with async_session() as db:
            entry_result = await db.execute(
                select(MediaCacheEntry).where(MediaCacheEntry.cache_key == cache_key)
            )
            entry = entry_result.scalar_one_or_none()
            if entry is None or not entry.drive_file_id or int(entry.encryption_version or 0) >= CRYPTO_VERSION:
                return
            job_result = await db.execute(
                select(MediaCacheJob).where(MediaCacheJob.cache_key == cache_key)
            )
            job = job_result.scalar_one_or_none()
            if job is None:
                job = MediaCacheJob(cache_key=cache_key, job_type="migrate", status="queued", next_attempt_at=now)
                db.add(job)
            elif job.status != "leased":
                job.job_type = "migrate"
                job.status = "queued"
                job.resumable_upload_url = None
                job.bytes_uploaded = 0
                job.attempts = 0
                job.next_attempt_at = now
                job.lease_owner = None
                job.lease_expires_at = None
                job.last_error = None
                job.updated_at = now
            entry.status = "ready"
            entry.updated_at = now
            await db.commit()

    async def _mark_missing_and_queue(self, cache_key: str, reason: str) -> None:
        now = utcnow()
        legacy_id: str | None = None
        async with async_session() as db:
            entry_result = await db.execute(
                select(MediaCacheEntry).where(MediaCacheEntry.cache_key == cache_key)
            )
            entry = entry_result.scalar_one_or_none()
            if entry is None:
                return
            legacy_id = str(entry.legacy_drive_file_id) if entry.legacy_drive_file_id else None
            entry.drive_file_id = None
            entry.legacy_drive_file_id = None
            entry.encryption_version = 0
            entry.encryption_nonce_prefix = None
            entry.encrypted_size_bytes = 0
            entry.status = "queued"
            entry.last_verified_at = now
            entry.last_error = reason[:1000]
            entry.failure_count = int(entry.failure_count or 0) + 1
            entry.updated_at = now
            job_result = await db.execute(
                select(MediaCacheJob).where(MediaCacheJob.cache_key == cache_key)
            )
            job = job_result.scalar_one_or_none()
            if job is None:
                job = MediaCacheJob(
                    cache_key=cache_key,
                    job_type="fill",
                    status="queued",
                    next_attempt_at=now + timedelta(seconds=CACHE_FILL_START_DELAY_SECONDS),
                )
                db.add(job)
            elif job.status != "leased":
                job.job_type = "fill"
                job.status = "queued"
                job.resumable_upload_url = None
                job.bytes_uploaded = 0
                job.attempts = 0
                job.next_attempt_at = now + timedelta(seconds=CACHE_FILL_START_DELAY_SECONDS)
                job.lease_owner = None
                job.lease_expires_at = None
                job.last_error = None
                job.updated_at = now
            await db.commit()
        if legacy_id:
            try:
                await self.drive.delete_file(legacy_id)
            except Exception as exc:
                logger.warning("Could not remove stale legacy plaintext object %s: %s", legacy_id, exc)

    async def _reconcile_batch(self, limit: int | None = None) -> dict[str, int]:
        """Verify catalog pointers, repair manual deletions, and queue legacy encryption."""
        batch = max(1, int(limit or settings.google_drive_cleanup_batch_size))
        async with async_session() as db:
            result = await db.execute(
                select(MediaCacheEntry)
                .where(
                    MediaCacheEntry.drive_file_id.is_not(None),
                    MediaCacheEntry.status.in_(["ready", "migrating", "evicting"]),
                )
                .order_by(MediaCacheEntry.last_verified_at.asc().nullsfirst(), MediaCacheEntry.id.asc())
                .limit(batch)
            )
            rows = [
                (
                    entry.cache_key,
                    str(entry.drive_file_id),
                    int(entry.size_bytes or 0),
                    int(entry.encryption_version or 0),
                    int(entry.encrypted_size_bytes or 0),
                    str(entry.legacy_drive_file_id) if entry.legacy_drive_file_id else None,
                    int(entry.active_readers or 0),
                    entry.read_lease_until,
                )
                for entry in result.scalars().all()
            ]

        stats = {"checked": 0, "missing": 0, "migrations_queued": 0, "legacy_deleted": 0}
        for (
            cache_key,
            drive_file_id,
            plain_size,
            enc_version,
            enc_size,
            legacy_id,
            active_readers,
            read_lease_until,
        ) in rows:
            stats["checked"] += 1
            try:
                metadata = await self.drive.get_metadata(drive_file_id)
                expected = enc_size if enc_version == CRYPTO_VERSION else plain_size
                actual = int(metadata.get("size") or 0)
                if bool(metadata.get("trashed")) or actual != expected:
                    await self._mark_missing_and_queue(
                        cache_key,
                        "Drive object was manually deleted, trashed, or has an unexpected size",
                    )
                    stats["missing"] += 1
                    continue
                async with async_session() as db:
                    await db.execute(
                        update(MediaCacheEntry)
                        .where(MediaCacheEntry.cache_key == cache_key)
                        .values(last_verified_at=utcnow(), updated_at=utcnow())
                    )
                    await db.commit()
                if enc_version < CRYPTO_VERSION:
                    await self._queue_migration(cache_key)
                    stats["migrations_queued"] += 1
                if legacy_id and (
                    active_readers <= 0
                    or read_lease_until is None
                    or read_lease_until <= utcnow()
                ):
                    try:
                        await self.drive.delete_file(legacy_id)
                        async with async_session() as db:
                            await db.execute(
                                update(MediaCacheEntry)
                                .where(MediaCacheEntry.cache_key == cache_key)
                                .values(legacy_drive_file_id=None, updated_at=utcnow())
                            )
                            await db.commit()
                        stats["legacy_deleted"] += 1
                    except Exception as exc:
                        logger.warning("Could not remove legacy plaintext Drive object %s: %s", legacy_id, exc)
            except DriveObjectMissing:
                await self._mark_missing_and_queue(cache_key, "Drive object was manually deleted")
                stats["missing"] += 1
            except DriveRateLimited:
                break
            except Exception as exc:
                logger.warning("Drive reconciliation failed for %s: %s", cache_key[:12], exc)
        return stats

    async def reconcile_now(self) -> dict[str, int]:
        if not self.enabled:
            return {"checked": 0, "missing": 0, "migrations_queued": 0, "legacy_deleted": 0}
        if not await self._acquire_lock("media-cache-reconcile", 15 * 60):
            return {"checked": 0, "missing": 0, "migrations_queued": 0, "legacy_deleted": 0}
        try:
            return await self._reconcile_batch()
        finally:
            await self._release_lock("media-cache-reconcile")

    async def run_cleanup(self) -> None:
        if not self.enabled:
            return
        if not await self._acquire_lock("media-cache-cleanup", 15 * 60):
            return
        try:
            now = utcnow()
            await self._delete_due(now)
            await self._reconcile_batch()
            budget = settings.google_drive_cache_budget_gb * 1024**3
            high = int(budget * settings.google_drive_high_watermark)
            low = int(budget * settings.google_drive_low_watermark)

            async with async_session() as db:
                used_result = await db.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        MediaCacheEntry.encryption_version == CRYPTO_VERSION,
                                        MediaCacheEntry.encrypted_size_bytes,
                                    ),
                                    else_=MediaCacheEntry.size_bytes,
                                )
                            ),
                            0,
                        )
                    ).where(
                        MediaCacheEntry.status == "ready",
                        MediaCacheEntry.drive_file_id.is_not(None),
                    )
                )
                projected_used = int(used_result.scalar_one() or 0)

                result = await db.execute(
                    select(MediaCacheEntry).where(
                        MediaCacheEntry.pinned.is_(False),
                        MediaCacheEntry.status.in_(
                            ["ready", "failed", "missing", "observed"]
                        ),
                        or_(
                            MediaCacheEntry.active_readers <= 0,
                            MediaCacheEntry.read_lease_until.is_(None),
                            MediaCacheEntry.read_lease_until <= now,
                        ),
                    )
                )
                entries = list(result.scalars().all())

                orphan_result = await db.execute(
                    select(MediaCacheEntry.cache_key)
                    .where(
                        ~exists(
                            select(File.id).where(
                                File.file_unique_id == MediaCacheEntry.file_unique_id
                            )
                        )
                    )
                    .execution_options(include_deleted=True)
                )
                orphan_keys = {str(row[0]) for row in orphan_result.all()}

                cold_cutoff = now - timedelta(days=settings.google_drive_cold_ttl_days)
                never_cutoff = now - timedelta(
                    days=settings.google_drive_never_reused_ttl_days
                )
                observed_cutoff = now - timedelta(days=7)
                pressure = projected_used >= high
                candidates: list[MediaCacheEntry] = []
                for entry in entries:
                    last = self._last_effective_access(entry)
                    popularity = int(entry.edge_hit_count or 0) + int(
                        entry.drive_hit_count or 0
                    )
                    never_reused = (
                        entry.status == "ready"
                        and popularity == 0
                        and entry.upload_completed_at is not None
                        and entry.upload_completed_at <= never_cutoff
                    )
                    cold = entry.status == "ready" and last <= cold_cutoff
                    garbage = entry.status in {"failed", "missing"}
                    stale_observed = (
                        entry.status == "observed" and entry.created_at <= observed_cutoff
                    )
                    orphaned = entry.cache_key in orphan_keys
                    if pressure or never_reused or cold or garbage or stale_observed or orphaned:
                        candidates.append(entry)

                candidates.sort(
                    key=lambda item: self._eviction_score(item, now),
                    reverse=True,
                )
                grace = timedelta(hours=settings.google_drive_eviction_grace_hours)
                for entry in candidates:
                    previous_status = entry.status
                    occupied = (
                        int(
                            entry.encrypted_size_bytes
                            if int(entry.encryption_version or 0) == CRYPTO_VERSION
                            else entry.size_bytes
                            or 0
                        )
                        if previous_status == "ready" and entry.drive_file_id
                        else 0
                    )
                    last = self._last_effective_access(entry)
                    timed_out = (
                        last <= cold_cutoff
                        or (
                            int(entry.edge_hit_count or 0)
                            + int(entry.drive_hit_count or 0)
                            == 0
                            and entry.upload_completed_at is not None
                            and entry.upload_completed_at <= never_cutoff
                        )
                    )
                    orphaned = entry.cache_key in orphan_keys
                    garbage = previous_status in {"failed", "missing", "observed"}
                    if not pressure and not (timed_out or orphaned or garbage):
                        continue
                    entry.status = "evicting"
                    entry.delete_after = now + grace
                    entry.eviction_reason = (
                        "storage-pressure"
                        if pressure and occupied
                        else "orphaned"
                        if orphaned
                        else "garbage"
                        if garbage
                        else "cold-or-unused"
                    )
                    entry.updated_at = now
                    projected_used -= occupied
                    if pressure and projected_used <= low:
                        pressure = False
                await db.commit()

            if settings.google_drive_eviction_grace_hours == 0:
                await self._delete_due(utcnow())
        finally:
            await self._release_lock("media-cache-cleanup")

    async def _delete_due(self, now) -> None:
        async with async_session() as db:
            result = await db.execute(
                select(MediaCacheEntry.id)
                .where(
                    MediaCacheEntry.status == "evicting",
                    MediaCacheEntry.delete_after.is_not(None),
                    MediaCacheEntry.delete_after <= now,
                    MediaCacheEntry.pinned.is_(False),
                    or_(
                        MediaCacheEntry.active_readers <= 0,
                        MediaCacheEntry.read_lease_until.is_(None),
                        MediaCacheEntry.read_lease_until <= now,
                    ),
                )
                .order_by(MediaCacheEntry.delete_after.asc())
                .limit(settings.google_drive_cleanup_batch_size)
            )
            ids = [int(row[0]) for row in result.all()]

        for entry_id in ids:
            drive_file_id: str | None = None
            legacy_drive_file_id: str | None = None
            cache_key = ""
            async with async_session() as db:
                claim = await db.execute(
                    update(MediaCacheEntry)
                    .where(
                        MediaCacheEntry.id == entry_id,
                        MediaCacheEntry.status == "evicting",
                        MediaCacheEntry.delete_after <= now,
                        MediaCacheEntry.pinned.is_(False),
                        or_(
                            MediaCacheEntry.active_readers <= 0,
                            MediaCacheEntry.read_lease_until.is_(None),
                            MediaCacheEntry.read_lease_until <= now,
                        ),
                    )
                    .values(
                        status="deleting",
                        active_readers=0,
                        read_lease_until=None,
                        updated_at=utcnow(),
                    )
                )
                if claim.rowcount != 1:
                    await db.rollback()
                    continue
                await db.commit()
                row = await db.get(MediaCacheEntry, entry_id)
                if row is None:
                    continue
                drive_file_id = str(row.drive_file_id) if row.drive_file_id else None
                legacy_drive_file_id = (
                    str(row.legacy_drive_file_id) if row.legacy_drive_file_id else None
                )
                cache_key = row.cache_key

            try:
                if drive_file_id:
                    await self.drive.delete_file(drive_file_id)
                if legacy_drive_file_id and legacy_drive_file_id != drive_file_id:
                    await self.drive.delete_file(legacy_drive_file_id)
                async with async_session() as db:
                    # Delete the durable job explicitly as well as relying on
                    # PostgreSQL ON DELETE CASCADE. SQLite development databases
                    # may not have foreign-key cascades enabled.
                    await db.execute(
                        delete(MediaCacheJob).where(MediaCacheJob.cache_key == cache_key)
                    )
                    await db.execute(
                        delete(MediaCacheEntry).where(
                            MediaCacheEntry.id == entry_id,
                            MediaCacheEntry.status == "deleting",
                        )
                    )
                    await db.commit()
                logger.info("Evicted Drive cache object %s", cache_key[:12])
            except Exception as exc:
                logger.warning("Failed to evict Drive cache object %s: %s", cache_key[:12], exc)
                async with async_session() as db:
                    await db.execute(
                        update(MediaCacheEntry)
                        .where(
                            MediaCacheEntry.id == entry_id,
                            MediaCacheEntry.status == "deleting",
                        )
                        .values(
                            status="evicting",
                            delete_after=utcnow() + timedelta(hours=1),
                            last_error=str(exc)[:1000],
                            updated_at=utcnow(),
                        )
                    )
                    await db.commit()

    async def status_snapshot(self) -> dict:
        async with async_session() as db:
            status_result = await db.execute(
                select(
                    MediaCacheEntry.status,
                    func.count(MediaCacheEntry.id),
                    func.coalesce(func.sum(MediaCacheEntry.size_bytes), 0),
                ).group_by(MediaCacheEntry.status)
            )
            jobs_result = await db.execute(
                select(MediaCacheJob.status, func.count(MediaCacheJob.id)).group_by(
                    MediaCacheJob.status
                )
            )
            encryption_result = await db.execute(
                select(
                    MediaCacheEntry.encryption_version,
                    func.count(MediaCacheEntry.id),
                )
                .where(MediaCacheEntry.drive_file_id.is_not(None))
                .group_by(MediaCacheEntry.encryption_version)
            )
            migration_result = await db.execute(
                select(func.count(MediaCacheJob.id)).where(
                    MediaCacheJob.job_type == "migrate",
                    MediaCacheJob.status.in_(["queued", "leased", "retry"]),
                )
            )
            last_verified_result = await db.execute(
                select(func.max(MediaCacheEntry.last_verified_at))
            )
            last_verified = last_verified_result.scalar_one_or_none()
            usage = await db.get(MediaCacheDailyUsage, self._usage_date())
        encryption_counts = {int(version or 0): int(count) for version, count in encryption_result.all()}
        loop_now = asyncio.get_running_loop().time()
        return {
            "enabled": self.enabled,
            "mode": settings.normalized_cache_mode,
            "instance_id": self.instance_id,
            "drive_circuit_open": loop_now < self._drive_bypass_until,
            "drive_circuit_seconds_remaining": max(
                0, int(self._drive_bypass_until - loop_now)
            ),
            "entries": {
                str(status): {"count": int(count), "bytes": int(size or 0)}
                for status, count, size in status_result.all()
            },
            "jobs": {str(status): int(count) for status, count in jobs_result.all()},
            "encryption": {
                "encrypted": encryption_counts.get(CRYPTO_VERSION, 0),
                "legacy_plaintext": sum(
                    count for version, count in encryption_counts.items() if version < CRYPTO_VERSION
                ),
                "pending_migration": int(migration_result.scalar_one() or 0),
                "last_reconciliation": last_verified.isoformat() if last_verified else None,
            },
            "today": {
                "date": self._usage_date(),
                "drive_bytes": int(usage.drive_bytes if usage else 0),
                "telegram_bytes": int(usage.telegram_bytes if usage else 0),
                "edge_hits": int(usage.edge_hits if usage else 0),
            },
        }


media_cache = ManagedMediaCache(drive_cache_client)
