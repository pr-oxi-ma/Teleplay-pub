"""Chunked authenticated encryption for Google Drive media-cache objects.

Drive stores only opaque AES-256-GCM blocks.  The original media filename and
MIME type remain in TelePlay's database, not in the Drive object.  Each block is
independently authenticated so HTTP byte ranges can be decrypted without
loading the whole file.
"""
from __future__ import annotations

import base64
import math
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

CRYPTO_VERSION = 1
PLAIN_BLOCK_SIZE = 1024 * 1024  # fixed 1 MiB; protocol constant, not a tuning knob
TAG_SIZE = 16
NONCE_PREFIX_SIZE = 8


class CacheCryptoError(RuntimeError):
    pass


def decode_master_key(encoded: str) -> bytes:
    try:
        key = base64.b64decode(encoded.strip(), validate=True)
    except Exception as exc:
        raise CacheCryptoError("MEDIA_CACHE_MASTER_KEY_BASE64 is not valid base64") from exc
    if len(key) != 32:
        raise CacheCryptoError("MEDIA_CACHE_MASTER_KEY_BASE64 must decode to exactly 32 bytes")
    return key


def derive_file_key(master_key: bytes, cache_key: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"TelePlay-Drive-Cache-v1",
        info=("asset:" + cache_key).encode("ascii"),
    ).derive(master_key)


def encrypted_size(plain_size: int) -> int:
    if plain_size <= 0:
        return 0
    return plain_size + math.ceil(plain_size / PLAIN_BLOCK_SIZE) * TAG_SIZE


def block_plain_length(plain_size: int, block_index: int) -> int:
    start = block_index * PLAIN_BLOCK_SIZE
    if start >= plain_size:
        return 0
    return min(PLAIN_BLOCK_SIZE, plain_size - start)


def block_encrypted_offset(block_index: int) -> int:
    return block_index * (PLAIN_BLOCK_SIZE + TAG_SIZE)


def encrypted_range_for_plain_range(plain_size: int, start: int, end: int) -> tuple[int, int, int, int]:
    if plain_size <= 0 or start < 0 or end < start or end >= plain_size:
        raise CacheCryptoError("Invalid plaintext range")
    first = start // PLAIN_BLOCK_SIZE
    last = end // PLAIN_BLOCK_SIZE
    enc_start = block_encrypted_offset(first)
    enc_end = block_encrypted_offset(last) + block_plain_length(plain_size, last) + TAG_SIZE - 1
    return first, last, enc_start, enc_end


@dataclass(slots=True, frozen=True)
class CacheCipher:
    cache_key: str
    plain_size: int
    nonce_prefix: bytes
    key: bytes

    @classmethod
    def create(cls, *, master_key: bytes, cache_key: str, plain_size: int, nonce_prefix: bytes):
        if len(nonce_prefix) != NONCE_PREFIX_SIZE:
            raise CacheCryptoError("Cache nonce prefix must be exactly 8 bytes")
        return cls(
            cache_key=cache_key,
            plain_size=int(plain_size),
            nonce_prefix=bytes(nonce_prefix),
            key=derive_file_key(master_key, cache_key),
        )

    def _nonce(self, block_index: int) -> bytes:
        if block_index < 0 or block_index > 0xFFFFFFFF:
            raise CacheCryptoError("Cache block index is outside the nonce domain")
        return self.nonce_prefix + int(block_index).to_bytes(4, "big")

    def _aad(self, block_index: int) -> bytes:
        return (
            f"TPMC1|{self.cache_key}|{self.plain_size}|{int(block_index)}"
        ).encode("ascii")

    def encrypt_block(self, block_index: int, plaintext: bytes) -> bytes:
        expected = block_plain_length(self.plain_size, block_index)
        if len(plaintext) != expected:
            raise CacheCryptoError(
                f"Plain block {block_index} has {len(plaintext)} bytes; expected {expected}"
            )
        return AESGCM(self.key).encrypt(
            self._nonce(block_index), plaintext, self._aad(block_index)
        )

    def decrypt_block(self, block_index: int, ciphertext: bytes) -> bytes:
        expected = block_plain_length(self.plain_size, block_index) + TAG_SIZE
        if len(ciphertext) != expected:
            raise CacheCryptoError(
                f"Encrypted block {block_index} has {len(ciphertext)} bytes; expected {expected}"
            )
        try:
            return AESGCM(self.key).decrypt(
                self._nonce(block_index), ciphertext, self._aad(block_index)
            )
        except Exception as exc:
            raise CacheCryptoError(
                f"Encrypted cache block {block_index} failed authentication"
            ) from exc
