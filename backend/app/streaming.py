"""
Custom streaming utilities for Telegram media files.

The streamer keeps a small bounded prefetch window instead of downloading a long
range ahead of the browser. That makes playback start faster, keeps RAM stable
on large videos, and prevents server-side buffering from growing when the client
is slow or seeking frequently.
"""
import asyncio
import logging
from typing import AsyncGenerator, Awaitable, Callable

from pyrogram import Client

from .config import get_settings
from .telegram import clients
from .cache import get_bytes, set_bytes, set_lock

settings = get_settings()
logger = logging.getLogger("streamer")
logger.setLevel(logging.INFO)

# Keep these values easy to edit. Pyrogram's stream_media offset is chunk-based,
# and Telegram commonly returns ~1 MiB chunks here.
STREAM_CHUNK_SIZE = 1024 * 1024
STREAM_PREFETCH_CHUNKS = 4
STREAM_MAX_EMPTY_CHUNKS = 2

ShouldStopCallback = Callable[[], Awaitable[bool]]


async def _should_stop_streaming(should_stop: ShouldStopCallback | None) -> bool:
    if should_stop is None:
        return False
    try:
        return bool(await should_stop())
    except Exception:
        # Disconnect checks must never break normal streaming.
        return False


# Global semaphores to limit concurrency per client across all streams.
_client_semaphores: dict[int, asyncio.Semaphore] = {}


def get_client_semaphore(client_index: int) -> asyncio.Semaphore:
    if client_index not in _client_semaphores:
        _client_semaphores[client_index] = asyncio.Semaphore(
            settings.telegram_client_concurrency,
        )
    return _client_semaphores[client_index]


def _message_has_media(message) -> bool:
    return bool(
        getattr(message, "document", None)
        or getattr(message, "video", None)
        or getattr(message, "audio", None)
        or getattr(message, "photo", None)
    )


async def _hydrate_client_messages(initial_message, concurrency: int):
    """
    Fetch the same storage-channel message for helper clients.

    Each bot/client gets its own Message object to avoid stale file references.
    If helpers fail, fall back to the already fetched message so the stream still
    works with one client.
    """
    pool_size = len(clients)
    chat_id = initial_message.chat.id
    message_id = initial_message.id

    async def fetch_msg(client, idx):
        try:
            msg = await client.get_messages(chat_id, message_id)
            if msg and _message_has_media(msg):
                return idx, msg
            logger.warning("Bot %d: message %d has no media", idx, message_id)
        except Exception as exc:
            logger.warning("Bot %d: failed to fetch message %d: %s", idx, message_id, exc)
        return idx, None

    fetch_tasks = []
    for i in range(max(concurrency, 1)):
        client = clients[i % pool_size]
        client_index = getattr(client, "pool_index", i % pool_size)
        fetch_tasks.append(fetch_msg(client, client_index))

    fetch_results = await asyncio.gather(*fetch_tasks)
    client_messages = {idx: msg for idx, msg in fetch_results if msg is not None}

    if not client_messages and initial_message and _message_has_media(initial_message):
        main_index = getattr(clients[0], "pool_index", 0) if clients else 0
        client_messages[main_index] = initial_message
        logger.warning("Using initial message fallback for media stream")

    return client_messages


def _safe_int(value, fallback: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


def stream_chunk_cache_key(message, chunk_idx: int) -> str:
    chat = getattr(message, "chat", None)
    chat_id = _safe_int(getattr(chat, "id", settings.telegram_storage_channel_id))
    message_id = _safe_int(getattr(message, "id", getattr(message, "message_id", 0)))
    return f"teleplay:stream:v1:{chat_id}:{message_id}:{chunk_idx}"


def should_cache_stream_chunk(chunk_idx: int) -> bool:
    return (
        bool(settings.redis_url)
        and settings.redis_cache_stream_chunks
        and chunk_idx >= 0
        and chunk_idx < max(0, settings.redis_stream_chunk_cache_chunks)
    )


async def _fetch_chunk(chunk_idx: int, client: Client, message) -> bytes:
    cache_key = stream_chunk_cache_key(message, chunk_idx)
    if should_cache_stream_chunk(chunk_idx):
        cached = await get_bytes(cache_key)
        if cached is not None:
            return cached

    client_index = getattr(client, "pool_index", 0)
    semaphore = get_client_semaphore(client_index)

    async with semaphore:
        parts: list[bytes] = []
        async for part in client.stream_media(message, limit=1, offset=chunk_idx):
            parts.append(part)
        chunk = b"".join(parts)

    if chunk and should_cache_stream_chunk(chunk_idx):
        await set_bytes(
            cache_key,
            chunk,
            max(1, settings.redis_stream_chunk_ttl_seconds),
        )

    return chunk


async def warm_stream_start_cache(initial_message, chunk_count: int | None = None) -> int:
    """Warm only the first tiny stream chunks in optional Redis cache.

    This is used for previous/next media prefetch. It never downloads or caches a
    full file; it only primes the same first chunks the browser usually requests
    before playback can start. When Redis is not configured, this is a no-op.
    """
    if not settings.redis_url or not settings.redis_cache_stream_chunks:
        return 0

    if not initial_message or not _message_has_media(initial_message):
        return 0

    max_cached_chunks = max(0, settings.redis_stream_chunk_cache_chunks)
    wanted_chunks = max(0, chunk_count or settings.redis_prefetch_chunk_count)
    wanted_chunks = min(max_cached_chunks, wanted_chunks)
    if wanted_chunks <= 0:
        return 0

    lock_key = stream_chunk_cache_key(initial_message, -1).replace(":-1", ":prefetch-lock")
    if not await set_lock(lock_key, 30):
        return 0

    warmed = 0
    client = clients[0] if clients else None
    if client is None:
        return 0

    for chunk_idx in range(wanted_chunks):
        try:
            cache_key = stream_chunk_cache_key(initial_message, chunk_idx)
            if await get_bytes(cache_key) is not None:
                continue
            chunk = await _fetch_chunk(chunk_idx, client, initial_message)
            if chunk:
                warmed += 1
        except Exception as exc:
            logger.debug("Prefetch chunk %d failed: %s", chunk_idx, exc)
            break

    return warmed


async def parallel_stream_generator(
    initial_message,
    offset: int,
    length: int,
    chunk_size: int = STREAM_CHUNK_SIZE,
    concurrency: int | None = None,
    should_stop: ShouldStopCallback | None = None,
) -> AsyncGenerator[bytes, None]:
    """
    Stream chunks in order with bounded prefetch.

    Older code created futures for the whole requested range and workers could
    download far ahead of the browser. Large files could build up many completed
    chunks in memory. This version fetches only a small window, yields it, then
    moves to the next window.
    """
    if length <= 0:
        return

    pool_size = len(clients)
    if pool_size <= 0:
        logger.error("No Telegram clients available for streaming")
        return

    if concurrency is None:
        concurrency = max(1, min(pool_size, settings.telegram_client_concurrency))
    else:
        concurrency = max(1, min(concurrency, pool_size))

    prefetch_chunks = max(1, min(STREAM_PREFETCH_CHUNKS, concurrency * 2))
    start_chunk = offset // chunk_size
    end_chunk = (offset + length - 1) // chunk_size

    logger.debug(
        "Streaming chunks %d-%d with concurrency=%d prefetch=%d",
        start_chunk,
        end_chunk,
        concurrency,
        prefetch_chunks,
    )

    client_messages = await _hydrate_client_messages(initial_message, concurrency)
    if not client_messages:
        logger.error("No client could fetch streamable message")
        return

    usable_clients: list[tuple[Client, object]] = []
    for i in range(concurrency):
        client = clients[i % pool_size]
        client_index = getattr(client, "pool_index", i % pool_size)
        msg = client_messages.get(client_index)
        if msg is not None:
            usable_clients.append((client, msg))

    if not usable_clients:
        first_index, first_message = next(iter(client_messages.items()))
        fallback_client = clients[0]
        for client in clients:
            if getattr(client, "pool_index", 0) == first_index:
                fallback_client = client
                break
        usable_clients.append((fallback_client, first_message))

    current_chunk = start_chunk
    empty_chunks = 0
    while current_chunk <= end_chunk:
        if await _should_stop_streaming(should_stop):
            logger.debug("Client disconnected before chunk window %d", current_chunk)
            return

        window = list(
            range(current_chunk, min(current_chunk + prefetch_chunks, end_chunk + 1)),
        )
        tasks = []
        for index, chunk_idx in enumerate(window):
            client, msg = usable_clients[index % len(usable_clients)]
            tasks.append(asyncio.create_task(_fetch_chunk(chunk_idx, client, msg)))

        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        if await _should_stop_streaming(should_stop):
            logger.debug("Client disconnected after chunk window %d", current_chunk)
            return

        for chunk_idx, result in zip(window, results):
            if isinstance(result, Exception):
                logger.warning("Failed Telegram chunk %d: %s", chunk_idx, result)
                raise result

            chunk = result or b""
            if not chunk:
                empty_chunks += 1
                if empty_chunks >= STREAM_MAX_EMPTY_CHUNKS:
                    logger.warning("Stopping stream after repeated empty chunks")
                    return
                continue

            empty_chunks = 0
            yield chunk

            if await _should_stop_streaming(should_stop):
                logger.debug("Client disconnected while sending chunk %d", chunk_idx)
                return

        current_chunk += len(window)


async def stream_file(
    client: Client,  # kept for API compatibility; the pool is used instead
    message,
    from_bytes: int,
    until_bytes: int,
    should_stop: ShouldStopCallback | None = None,
    concurrency: int | None = None,
) -> AsyncGenerator[bytes, None]:
    """Stream a file range using the multi-client pool."""
    total_bytes_needed = until_bytes - from_bytes + 1
    bytes_yielded = 0
    bytes_to_skip = from_bytes % STREAM_CHUNK_SIZE

    logger.debug("Streaming %d-%d (%d bytes)", from_bytes, until_bytes, total_bytes_needed)

    async for chunk in parallel_stream_generator(
        message,
        from_bytes,
        total_bytes_needed,
        chunk_size=STREAM_CHUNK_SIZE,
        concurrency=concurrency,
        should_stop=should_stop,
    ):
        if bytes_to_skip > 0:
            chunk = chunk[bytes_to_skip:]
            bytes_to_skip = 0

        remaining = total_bytes_needed - bytes_yielded
        if len(chunk) > remaining:
            chunk = chunk[:remaining]

        if not chunk:
            continue

        if await _should_stop_streaming(should_stop):
            logger.debug("Client disconnected before yielding stream bytes")
            return

        yield chunk
        bytes_yielded += len(chunk)
        if bytes_yielded >= total_bytes_needed:
            break
