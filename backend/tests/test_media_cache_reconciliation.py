import os
import tempfile
import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.media_cache as cache_module
from app.database import Base
from app.google_drive_cache import DriveObjectMissing, DriveUploadResult
from app.media_cache import CacheSource, ClaimedJob, ManagedMediaCache
from app.media_cache_crypto import CacheCipher, block_plain_length
from app.models import MediaCacheEntry, MediaCacheJob
from app.time_utils import utcnow


class FakeDrive:
    def __init__(self, metadata=None, missing=False):
        self.metadata = metadata or {}
        self.missing = missing
        self.deleted = []

    async def get_metadata(self, _file_id):
        if self.missing:
            raise DriveObjectMissing("missing")
        return dict(self.metadata)

    async def delete_file(self, file_id):
        self.deleted.append(file_id)

    async def close(self):
        return None


class FakeUploadDrive(FakeDrive):
    def __init__(self):
        super().__init__()
        self.uploaded = b""

    async def create_resumable_upload(self, **_kwargs):
        return "https://upload.example/session"

    async def upload_resumable(self, *, chunks, on_progress, size, **_kwargs):
        content = bytearray()
        async for chunk in chunks:
            content.extend(chunk)
        self.uploaded = bytes(content)
        await on_progress("https://upload.example/session", len(content))
        return DriveUploadResult(file_id="encrypted-new", size=size, name="tp-e1.bin")


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.path = tempfile.mkstemp(prefix="teleplay-cache-test-", suffix=".db")
        os.close(handle)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.original_sessions = cache_module.async_session
        cache_module.async_session = self.sessions

    async def asyncTearDown(self):
        cache_module.async_session = self.original_sessions
        await self.engine.dispose()
        os.unlink(self.path)

    async def seed(self, drive_file_id="drive-1"):
        async with self.sessions() as db:
            entry = MediaCacheEntry(
                cache_key="a" * 64,
                cache_version=2,
                file_unique_id="unique-1",
                source_message_id=42,
                file_name="old.mp4",
                mime_type="video/mp4",
                file_type="video",
                size_bytes=1234,
                drive_file_id=drive_file_id,
                status="ready",
                encryption_version=0,
                upload_completed_at=utcnow(),
            )
            db.add(entry)
            db.add(
                MediaCacheJob(
                    cache_key=entry.cache_key,
                    job_type="fill",
                    status="completed",
                    bytes_uploaded=1234,
                )
            )
            await db.commit()

    async def test_manual_drive_delete_removes_stale_ready_and_queues_encrypted_refill(self):
        await self.seed()
        cache = ManagedMediaCache(FakeDrive(missing=True))
        stats = await cache._reconcile_batch(limit=10)
        self.assertEqual(stats["missing"], 1)

        async with self.sessions() as db:
            entry = (await db.execute(select(MediaCacheEntry))).scalar_one()
            job = (await db.execute(select(MediaCacheJob))).scalar_one()
            self.assertEqual(entry.status, "queued")
            self.assertIsNone(entry.drive_file_id)
            self.assertEqual(entry.encryption_version, 0)
            self.assertEqual(job.job_type, "fill")
            self.assertEqual(job.status, "queued")
            self.assertEqual(job.attempts, 0)

    async def test_surviving_plaintext_object_is_queued_for_migration(self):
        await self.seed()
        cache = ManagedMediaCache(
            FakeDrive(metadata={"id": "drive-1", "size": "1234", "trashed": False})
        )
        stats = await cache._reconcile_batch(limit=10)
        self.assertEqual(stats["migrations_queued"], 1)

        async with self.sessions() as db:
            entry = (await db.execute(select(MediaCacheEntry))).scalar_one()
            job = (await db.execute(select(MediaCacheJob))).scalar_one()
            self.assertEqual(entry.status, "ready")
            self.assertEqual(entry.drive_file_id, "drive-1")
            self.assertEqual(job.job_type, "migrate")
            self.assertEqual(job.status, "queued")

    async def test_legacy_migration_switches_atomically_and_deletes_plaintext(self):
        plaintext = os.urandom(1024 * 1024 + 321)
        async with self.sessions() as db:
            entry = MediaCacheEntry(
                cache_key="e" * 64,
                cache_version=2,
                file_unique_id="unique-e",
                source_message_id=77,
                file_name="private.mp4",
                mime_type="video/mp4",
                file_type="video",
                size_bytes=len(plaintext),
                drive_file_id="legacy-old",
                status="migrating",
                encryption_version=0,
                upload_completed_at=utcnow(),
            )
            db.add(entry)
            await db.flush()
            job = MediaCacheJob(
                cache_key=entry.cache_key,
                job_type="migrate",
                status="leased",
                lease_owner="placeholder",
                attempts=1,
            )
            db.add(job)
            await db.commit()
            job_id = job.id

        drive = FakeUploadDrive()
        cache = ManagedMediaCache(drive)
        cache._master_key = bytes(range(32))
        async with self.sessions() as db:
            job = await db.get(MediaCacheJob, job_id)
            job.lease_owner = cache.instance_id
            await db.commit()

        async def legacy_chunks(_drive_id, start_offset, _plain_size):
            remaining = plaintext[start_offset:]
            for offset in range(0, len(remaining), 160003):
                yield remaining[offset : offset + 160003]

        cache._legacy_drive_chunks = legacy_chunks
        source = CacheSource(
            cache_key="e" * 64,
            file_unique_id="unique-e",
            channel_message_id=77,
            file_name="private.mp4",
            file_size=len(plaintext),
            mime_type="video/mp4",
            file_type="video",
        )
        claimed = ClaimedJob(
            id=job_id,
            cache_key=source.cache_key,
            job_type="migrate",
            resumable_upload_url=None,
            bytes_uploaded=0,
            attempts=1,
            source_drive_file_id="legacy-old",
            source=source,
        )
        await cache._process_job(claimed)

        async with self.sessions() as db:
            entry = (await db.execute(select(MediaCacheEntry))).scalar_one()
            job = (await db.execute(select(MediaCacheJob))).scalar_one()
            self.assertEqual(entry.drive_file_id, "encrypted-new")
            self.assertIsNone(entry.legacy_drive_file_id)
            self.assertEqual(entry.encryption_version, 1)
            self.assertEqual(job.status, "completed")
        self.assertIn("legacy-old", drive.deleted)

        nonce = __import__("base64").b64decode(entry.encryption_nonce_prefix)
        cipher = CacheCipher.create(
            master_key=bytes(range(32)),
            cache_key=entry.cache_key,
            plain_size=len(plaintext),
            nonce_prefix=nonce,
        )
        recovered = bytearray()
        cursor = 0
        for index in range(2):
            length = block_plain_length(len(plaintext), index) + 16
            recovered.extend(cipher.decrypt_block(index, drive.uploaded[cursor : cursor + length]))
            cursor += length
        self.assertEqual(bytes(recovered), plaintext)


if __name__ == "__main__":
    unittest.main()
