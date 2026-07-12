import os
import unittest

from app.media_cache_crypto import (
    CacheCipher,
    PLAIN_BLOCK_SIZE,
    TAG_SIZE,
    block_encrypted_offset,
    block_plain_length,
    encrypted_range_for_plain_range,
    encrypted_size,
)


class MediaCacheCryptoTests(unittest.TestCase):
    def setUp(self):
        self.master = bytes(range(32))
        self.cache_key = "a" * 64
        self.nonce = b"12345678"

    def test_round_trip_independent_blocks(self):
        content = os.urandom(PLAIN_BLOCK_SIZE + 12345)
        cipher = CacheCipher.create(
            master_key=self.master,
            cache_key=self.cache_key,
            plain_size=len(content),
            nonce_prefix=self.nonce,
        )
        out = bytearray()
        for index in range(2):
            start = index * PLAIN_BLOCK_SIZE
            block = content[start : start + block_plain_length(len(content), index)]
            encrypted = cipher.encrypt_block(index, block)
            self.assertEqual(len(encrypted), len(block) + TAG_SIZE)
            out.extend(cipher.decrypt_block(index, encrypted))
        self.assertEqual(bytes(out), content)
        self.assertEqual(encrypted_size(len(content)), len(content) + 2 * TAG_SIZE)

    def test_plain_range_maps_to_complete_crypto_blocks(self):
        plain_size = PLAIN_BLOCK_SIZE * 3 + 100
        start = PLAIN_BLOCK_SIZE - 20
        end = PLAIN_BLOCK_SIZE * 2 + 25
        first, last, enc_start, enc_end = encrypted_range_for_plain_range(
            plain_size, start, end
        )
        self.assertEqual((first, last), (0, 2))
        self.assertEqual(enc_start, 0)
        expected = block_encrypted_offset(2) + PLAIN_BLOCK_SIZE + TAG_SIZE - 1
        self.assertEqual(enc_end, expected)


if __name__ == "__main__":
    unittest.main()
