import os
import unittest

from app.google_drive_cache import DriveCacheError
from app.media_cache import (
    CacheSource,
    ClaimedJob,
    EncryptedDriveRange,
    ManagedMediaCache,
)
from app.media_cache_crypto import (
    CacheCipher,
    PLAIN_BLOCK_SIZE,
    block_plain_length,
    encrypted_range_for_plain_range,
)


class FakeResponse:
    def __init__(self, payload: bytes, chunk_size: int = 131071):
        self.payload = payload
        self.chunk_size = chunk_size
        self.closed = False

    async def aiter_bytes(self, _size: int):
        for offset in range(0, len(self.payload), self.chunk_size):
            yield self.payload[offset : offset + self.chunk_size]

    async def aclose(self):
        self.closed = True


class EncryptedDriveRangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_cross_block_plain_range_is_decrypted_without_full_file_load(self):
        content = os.urandom(PLAIN_BLOCK_SIZE * 2 + 777)
        cipher = CacheCipher.create(
            master_key=bytes(range(32)),
            cache_key="b" * 64,
            plain_size=len(content),
            nonce_prefix=b"87654321",
        )
        encrypted_blocks = []
        for index in range(3):
            start = index * PLAIN_BLOCK_SIZE
            size = block_plain_length(len(content), index)
            encrypted_blocks.append(cipher.encrypt_block(index, content[start : start + size]))
        encrypted = b"".join(encrypted_blocks)

        start = PLAIN_BLOCK_SIZE - 19
        end = PLAIN_BLOCK_SIZE * 2 + 20
        first, last, enc_start, enc_end = encrypted_range_for_plain_range(
            len(content), start, end
        )
        response = FakeResponse(encrypted[enc_start : enc_end + 1])
        stream = EncryptedDriveRange(
            response,
            cipher,
            first,
            last,
            start,
            end,
            enc_end - enc_start + 1,
        )
        await stream.prepare()
        output = bytearray()
        async for chunk in stream.aiter_bytes():
            output.extend(chunk)
        await stream.aclose()
        self.assertEqual(bytes(output), content[start : end + 1])
        self.assertTrue(response.closed)

    async def test_first_block_authentication_fails_before_streaming(self):
        content = os.urandom(1000)
        cipher = CacheCipher.create(
            master_key=bytes(range(32)),
            cache_key="c" * 64,
            plain_size=len(content),
            nonce_prefix=b"12345678",
        )
        encrypted = bytearray(cipher.encrypt_block(0, content))
        encrypted[-1] ^= 1
        response = FakeResponse(bytes(encrypted))
        stream = EncryptedDriveRange(
            response, cipher, 0, 0, 0, len(content) - 1, len(encrypted)
        )
        with self.assertRaises(DriveCacheError):
            await stream.prepare()
        self.assertTrue(response.closed)


class EncryptedUploadResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_offset_recreates_identical_ciphertext(self):
        content = os.urandom(PLAIN_BLOCK_SIZE * 2 + 123)
        cache = ManagedMediaCache.__new__(ManagedMediaCache)
        cache._master_key = bytes(range(32))

        async def source_chunks(_source, start_offset):
            data = content[start_offset:]
            for offset in range(0, len(data), 170003):
                yield data[offset : offset + 170003]

        cache._source_chunks = source_chunks
        source = CacheSource(
            cache_key="d" * 64,
            file_unique_id="unique",
            channel_message_id=1,
            file_name="secret.mp4",
            file_size=len(content),
            mime_type="video/mp4",
            file_type="video",
        )
        claimed = ClaimedJob(
            id=1,
            cache_key=source.cache_key,
            job_type="fill",
            resumable_upload_url=None,
            bytes_uploaded=0,
            attempts=1,
            source_drive_file_id=None,
            source=source,
        )
        nonce = b"abcdefgh"
        full = bytearray()
        async for chunk in cache._encrypted_source_chunks(claimed, 0, nonce):
            full.extend(chunk)

        resume_offset = PLAIN_BLOCK_SIZE + 12345
        resumed = bytearray()
        async for chunk in cache._encrypted_source_chunks(claimed, resume_offset, nonce):
            resumed.extend(chunk)
        self.assertEqual(bytes(resumed), bytes(full[resume_offset:]))


if __name__ == "__main__":
    unittest.main()
