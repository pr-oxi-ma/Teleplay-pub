"""
Optional Redis cache helpers.

The app must work without Redis. If REDIS_URL is empty, every helper becomes a
small no-op. If REDIS_URL is set but redis-py is not installed or Redis is down,
the cache disables itself and streaming continues normally.
"""
from __future__ import annotations

import logging
from typing import Optional

from .config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_redis = None
_redis_checked = False
_redis_failed = False


def redis_configured() -> bool:
    return bool(settings.redis_url.strip())


def cache_enabled() -> bool:
    return redis_configured() and not _redis_failed


async def get_redis():
    """Return a lazy Redis connection, or None when Redis is unavailable."""
    global _redis, _redis_checked, _redis_failed

    if not redis_configured() or _redis_failed:
        return None

    if _redis is not None:
        return _redis

    try:
        from redis.asyncio import Redis

        _redis = Redis.from_url(
            settings.redis_url,
            encoding=None,
            decode_responses=False,
            socket_connect_timeout=1.5,
            socket_timeout=3,
            retry_on_timeout=True,
        )

        if not _redis_checked:
            await _redis.ping()
            _redis_checked = True
            logger.info("Optional Redis cache enabled")

        return _redis
    except Exception as exc:
        _redis_failed = True
        logger.warning("Redis cache disabled: %s", exc)
        return None


async def close_cache() -> None:
    global _redis
    if _redis is None:
        return
    try:
        await _redis.aclose()
    except Exception:
        pass
    finally:
        _redis = None


async def get_bytes(key: str) -> Optional[bytes]:
    redis = await get_redis()
    if redis is None:
        return None
    try:
        value = await redis.get(key)
        return bytes(value) if value is not None else None
    except Exception as exc:
        logger.debug("Redis get failed for %s: %s", key, exc)
        return None


async def set_bytes(key: str, value: bytes, ttl_seconds: int) -> bool:
    if not value or ttl_seconds <= 0:
        return False

    redis = await get_redis()
    if redis is None:
        return False

    try:
        await redis.set(key, value, ex=ttl_seconds)
        return True
    except Exception as exc:
        logger.debug("Redis set failed for %s: %s", key, exc)
        return False


async def set_lock(key: str, ttl_seconds: int) -> bool:
    """Best-effort distributed lock. Returns True only when lock was acquired."""
    if ttl_seconds <= 0:
        return False

    redis = await get_redis()
    if redis is None:
        return False

    try:
        return bool(await redis.set(key, b"1", nx=True, ex=ttl_seconds))
    except Exception as exc:
        logger.debug("Redis lock failed for %s: %s", key, exc)
        return False
