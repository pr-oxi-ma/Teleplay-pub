#!/usr/bin/env python3
"""End-to-end production smoke test for TelePlay's hybrid media cache.

The test uses signed Cloudflare media URLs plus the internal origin secret. It
never needs a user session cookie and never writes media bodies to disk.

Use fresh test files for a deterministic first-MISS/fill test:
- small: 1-4 MiB (must be <= the configured small-file admission limit)
- large: 30-100 MiB (large enough to exercise multi-chunk admission)

The optional --full-fill mode can transfer up to the configured admission
threshold from Telegram and then waits for the complete encrypted Drive upload.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MIB = 1024 * 1024
EDGE_CHUNK = 4 * MIB
READ_BUFFER = 64 * 1024


@dataclass
class HttpResult:
    method: str
    url: str
    status: int
    headers: dict[str, str]
    bytes_read: int
    sha256: str | None
    ttfb_ms: float
    total_ms: float
    error_body: str | None = None


class Reporter:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.http: list[dict[str, Any]] = []

    def add_http(self, result: HttpResult) -> None:
        data = asdict(result)
        # Signed URLs and cache keys are sensitive operational data. Keep only
        # the host/path in persisted reports.
        parsed = urllib.parse.urlsplit(data["url"])
        data["url"] = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        self.http.append(data)

    def check(self, name: str, condition: bool, detail: Any = None, *, warning: bool = False) -> bool:
        state = "PASS" if condition else ("WARN" if warning else "FAIL")
        self.checks.append({"name": name, "state": state, "detail": detail})
        icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}[state]
        text = f"{icon} {name}"
        if detail not in (None, ""):
            text += f" — {detail}"
        print(text, flush=True)
        return condition

    @property
    def failed(self) -> int:
        return sum(1 for item in self.checks if item["state"] == "FAIL")

    @property
    def warnings(self) -> int:
        return sum(1 for item in self.checks if item["state"] == "WARN")


def redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def http_request(
    reporter: Reporter,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 180.0,
    hash_body: bool = True,
    max_body: int | None = None,
) -> HttpResult:
    request = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    started = time.perf_counter()
    response = None
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        result = HttpResult(
            method=method,
            url=url,
            status=0,
            headers={},
            bytes_read=0,
            sha256=None,
            ttfb_ms=elapsed,
            total_ms=elapsed,
            error_body=f"{type(exc).__name__}: {exc}",
        )
        reporter.add_http(result)
        return result

    status = int(getattr(response, "status", getattr(response, "code", 0)))
    response_headers = {key.lower(): value for key, value in response.headers.items()}
    digest = hashlib.sha256() if hash_body and method != "HEAD" else None
    bytes_read = 0
    first_byte_at = None
    error_chunks: list[bytes] = []

    try:
        if method != "HEAD":
            while True:
                remaining = None if max_body is None else max_body - bytes_read
                if remaining is not None and remaining <= 0:
                    break
                chunk = response.read(READ_BUFFER if remaining is None else min(READ_BUFFER, remaining))
                if not chunk:
                    break
                if first_byte_at is None:
                    first_byte_at = time.perf_counter()
                bytes_read += len(chunk)
                if digest is not None:
                    digest.update(chunk)
                if status >= 400 and sum(map(len, error_chunks)) < 4096:
                    error_chunks.append(chunk[: 4096 - sum(map(len, error_chunks))])
    finally:
        try:
            response.close()
        except Exception:
            pass

    ended = time.perf_counter()
    ttfb = ((first_byte_at or ended) - started) * 1000
    error_body = None
    if status >= 400 and error_chunks:
        error_body = b"".join(error_chunks).decode("utf-8", "replace")[:4096]
    result = HttpResult(
        method=method,
        url=url,
        status=status,
        headers=response_headers,
        bytes_read=bytes_read,
        sha256=digest.hexdigest() if digest is not None else None,
        ttfb_ms=round(ttfb, 2),
        total_ms=round((ended - started) * 1000, 2),
        error_body=error_body,
    )
    reporter.add_http(result)
    return result


def json_request(
    reporter: Reporter,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout: float = 180.0,
) -> tuple[HttpResult, dict[str, Any] | None]:
    data = b"" if method in {"POST", "PUT", "PATCH"} else None
    request = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    started = time.perf_counter()
    response = None
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        result = HttpResult(
            method=method, url=url, status=0, headers={}, bytes_read=0, sha256=None,
            ttfb_ms=round(elapsed, 2), total_ms=round(elapsed, 2),
            error_body=f"{type(exc).__name__}: {exc}",
        )
        reporter.add_http(result)
        return result, None

    status = int(getattr(response, "status", getattr(response, "code", 0)))
    response_headers = {key.lower(): value for key, value in response.headers.items()}
    try:
        raw = response.read(2 * MIB)
    finally:
        try:
            response.close()
        except Exception:
            pass
    ended = time.perf_counter()
    body = raw.decode("utf-8", "replace")
    result = HttpResult(
        method=method,
        url=url,
        status=status,
        headers=response_headers,
        bytes_read=len(raw),
        sha256=None,
        ttfb_ms=round((ended - started) * 1000, 2),
        total_ms=round((ended - started) * 1000, 2),
        error_body=body[:4096] if status >= 400 else None,
    )
    reporter.add_http(result)
    try:
        return result, json.loads(body or "{}")
    except json.JSONDecodeError:
        return result, {"_raw": body[:1000]}


def parse_edge_url(value: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(value.strip())
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 3 or parts[0] != "media":
        raise ValueError("edge URL path must be /media/<file_id>/<cache_key>")
    file_id = int(parts[1])
    cache_key = parts[2]
    if len(cache_key) != 64 or any(ch not in "0123456789abcdef" for ch in cache_key):
        raise ValueError("edge URL contains an invalid cache key")
    query = urllib.parse.parse_qs(parsed.query)
    required = {"v", "size", "expires", "token", "sig"}
    missing = sorted(required - set(query))
    if missing:
        raise ValueError(f"edge URL is missing query parameters: {', '.join(missing)}")
    size = int(query["size"][0])
    expires = int(query["expires"][0])
    if expires < int(time.time()):
        raise ValueError("edge URL has expired; copy a fresh stream_url")
    return {
        "url": value.strip(),
        "file_id": file_id,
        "cache_key": cache_key,
        "size": size,
        "version": int(query["v"][0]),
        "expires": expires,
        "host": parsed.netloc,
    }


def mutate_signature(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    signature = query.get("sig", [""])[0]
    replacement = "0" if not signature.endswith("0") else "1"
    query["sig"] = [signature[:-1] + replacement]
    encoded = urllib.parse.urlencode({key: values[0] for key, values in query.items()})
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, encoded, parsed.fragment))


def range_headers(origin: str, start: int, end: int) -> dict[str, str]:
    return {
        "Origin": origin,
        "Range": f"bytes={start}-{end}",
        "Accept-Encoding": "identity",
        "User-Agent": "TelePlay-Cache-Test/1",
    }


def expected_content_range(start: int, end: int, size: int) -> str:
    return f"bytes {start}-{end}/{size}"


def inspect_url(api_base: str, item: dict[str, Any]) -> str:
    return (
        f"{api_base.rstrip('/')}/api/stream/cache-inspect/{item['file_id']}"
        f"?cache_key={urllib.parse.quote(item['cache_key'])}"
    )


def origin_url(api_base: str, item: dict[str, Any]) -> str:
    return (
        f"{api_base.rstrip('/')}/api/stream/origin/{item['file_id']}"
        f"?cache_key={urllib.parse.quote(item['cache_key'])}"
    )


def get_inspection(
    reporter: Reporter,
    api_base: str,
    secret: str,
    item: dict[str, Any],
    *,
    check: bool = True,
) -> dict[str, Any] | None:
    result, payload = json_request(
        reporter,
        inspect_url(api_base, item),
        headers={"X-TelePlay-Origin-Secret": secret, "Accept": "application/json"},
    )
    if check:
        reporter.check(
            f"{item['label']}: protected cache-inspect endpoint",
            result.status == 200 and payload is not None and "file" in payload,
            f"HTTP {result.status}",
        )
    return payload if result.status == 200 and payload and "file" in payload else None


def request_media_range(
    reporter: Reporter,
    url: str,
    origin: str,
    start: int,
    end: int,
    *,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 240.0,
) -> HttpResult:
    headers = range_headers(origin, start, end)
    if extra_headers:
        headers.update(extra_headers)
    return http_request(reporter, url, headers=headers, timeout=timeout)


def verify_range_response(
    reporter: Reporter,
    label: str,
    result: HttpResult,
    start: int,
    end: int,
    size: int,
    *,
    edge: bool,
) -> None:
    expected_length = end - start + 1
    reporter.check(f"{label}: HTTP 206", result.status == 206, f"HTTP {result.status}")
    reporter.check(
        f"{label}: exact Content-Range",
        result.headers.get("content-range") == expected_content_range(start, end, size),
        result.headers.get("content-range"),
    )
    reporter.check(
        f"{label}: exact response length",
        result.bytes_read == expected_length
        and int(result.headers.get("content-length", "-1")) == expected_length,
        f"body={result.bytes_read}, header={result.headers.get('content-length')}",
    )
    reporter.check(
        f"{label}: byte ranges advertised",
        result.headers.get("accept-ranges", "").lower() == "bytes",
        result.headers.get("accept-ranges"),
    )
    if edge:
        state = result.headers.get("x-teleplay-edge-cache")
        reporter.check(
            f"{label}: edge cache state present",
            state in {"MISS", "HIT", "COALESCED"},
            state,
        )
    source = result.headers.get("x-teleplay-origin-cache")
    reporter.check(
        f"{label}: origin source header present",
        source in {"TELEGRAM", "GDRIVE"},
        source,
    )


def wait_for_edge_hit(
    reporter: Reporter,
    item: dict[str, Any],
    frontend_origin: str,
    start: int,
    end: int,
    first: HttpResult,
) -> HttpResult:
    last = first
    for _ in range(6):
        if last.headers.get("x-teleplay-edge-cache") in {"HIT", "COALESCED"}:
            break
        time.sleep(1.5)
        last = request_media_range(reporter, item["url"], frontend_origin, start, end)
    reporter.check(
        f"{item['label']}: repeated range becomes edge HIT",
        last.headers.get("x-teleplay-edge-cache") in {"HIT", "COALESCED"},
        last.headers.get("x-teleplay-edge-cache"),
    )
    reporter.check(
        f"{item['label']}: MISS/HIT body is identical",
        bool(first.sha256 and last.sha256 and first.sha256 == last.sha256),
        f"{first.sha256} / {last.sha256}",
    )
    return last


def verify_edge_and_origin(
    reporter: Reporter,
    item: dict[str, Any],
    api_base: str,
    frontend_origin: str,
    origin_secret: str,
) -> dict[str, Any]:
    label = item["label"]
    size = item["size"]
    probe_end = min(size - 1, MIB - 1)

    print(f"\n=== {label.upper()} FILE: edge/range tests ({size:,} bytes) ===")

    head = http_request(
        reporter,
        item["url"],
        method="HEAD",
        headers={"Origin": frontend_origin, "User-Agent": "TelePlay-Cache-Test/1"},
        hash_body=False,
    )
    reporter.check(f"{label}: signed Worker HEAD", head.status == 200, f"HTTP {head.status}")
    reporter.check(
        f"{label}: HEAD reports complete size",
        int(head.headers.get("content-length", "-1")) == size,
        head.headers.get("content-length"),
    )
    reporter.check(
        f"{label}: HEAD metadata state",
        head.headers.get("x-teleplay-edge-cache") == "METADATA",
        head.headers.get("x-teleplay-edge-cache"),
    )

    invalid = http_request(
        reporter,
        mutate_signature(item["url"]),
        headers={"Origin": frontend_origin},
        max_body=4096,
        hash_body=False,
    )
    reporter.check(f"{label}: invalid signature rejected", invalid.status == 403, f"HTTP {invalid.status}")

    disallowed = request_media_range(
        reporter,
        item["url"],
        "https://not-allowed.invalid",
        0,
        probe_end,
    )
    reporter.check(f"{label}: disallowed browser origin rejected", disallowed.status == 403, f"HTTP {disallowed.status}")

    invalid_range = http_request(
        reporter,
        item["url"],
        headers=range_headers(frontend_origin, size, size + 10),
        max_body=4096,
        hash_body=False,
    )
    reporter.check(f"{label}: invalid range rejected", invalid_range.status == 416, f"HTTP {invalid_range.status}")
    reporter.check(
        f"{label}: invalid range reports total size",
        invalid_range.headers.get("content-range") == f"bytes */{size}",
        invalid_range.headers.get("content-range"),
    )

    first = request_media_range(reporter, item["url"], frontend_origin, 0, probe_end)
    verify_range_response(reporter, f"{label}: first edge range", first, 0, probe_end, size, edge=True)
    repeated = wait_for_edge_hit(reporter, item, frontend_origin, 0, probe_end, first)

    no_secret = request_media_range(
        reporter,
        origin_url(api_base, item),
        frontend_origin,
        0,
        probe_end,
    )
    reporter.check(f"{label}: direct origin rejects missing secret", no_secret.status == 403, f"HTTP {no_secret.status}")

    direct = request_media_range(
        reporter,
        origin_url(api_base, item),
        frontend_origin,
        0,
        probe_end,
        extra_headers={"X-TelePlay-Origin-Secret": origin_secret},
    )
    verify_range_response(reporter, f"{label}: protected direct-origin range", direct, 0, probe_end, size, edge=False)
    reporter.check(
        f"{label}: Worker bytes equal direct-origin bytes",
        bool(repeated.sha256 and direct.sha256 and repeated.sha256 == direct.sha256),
        f"{repeated.sha256} / {direct.sha256}",
    )

    # Verify slicing from the same 4 MiB cached object.
    if size > 768 * 1024:
        sub_start = 512 * 1024
        sub_end = min(size - 1, 768 * 1024 - 1)
        sub_edge = request_media_range(reporter, item["url"], frontend_origin, sub_start, sub_end)
        sub_origin = request_media_range(
            reporter,
            origin_url(api_base, item),
            frontend_origin,
            sub_start,
            sub_end,
            extra_headers={"X-TelePlay-Origin-Secret": origin_secret},
        )
        verify_range_response(reporter, f"{label}: in-chunk edge slice", sub_edge, sub_start, sub_end, size, edge=True)
        reporter.check(
            f"{label}: in-chunk slice served from L1",
            sub_edge.headers.get("x-teleplay-edge-cache") in {"HIT", "COALESCED"},
            sub_edge.headers.get("x-teleplay-edge-cache"),
        )
        reporter.check(
            f"{label}: sliced bytes match origin",
            bool(sub_edge.sha256 and sub_origin.sha256 and sub_edge.sha256 == sub_origin.sha256),
            f"{sub_edge.sha256} / {sub_origin.sha256}",
        )

    return {
        "first_edge_state": first.headers.get("x-teleplay-edge-cache"),
        "first_origin_source": first.headers.get("x-teleplay-origin-cache"),
        "direct_origin_source": direct.headers.get("x-teleplay-origin-cache"),
        "sample_sha256": direct.sha256,
        "sample_start": 0,
        "sample_end": probe_end,
    }


def concurrency_probe(
    reporter: Reporter,
    item: dict[str, Any],
    frontend_origin: str,
) -> None:
    if item["size"] <= EDGE_CHUNK:
        reporter.check(
            f"{item['label']}: concurrent cold-chunk probe",
            False,
            "file has only one edge chunk; use a fresh >8 MiB file for a cold coalescing test",
            warning=True,
        )
        return

    # Pick the final edge chunk to avoid the startup chunks used above.
    chunk_index = (item["size"] - 1) // EDGE_CHUNK
    start = chunk_index * EDGE_CHUNK
    end = min(item["size"] - 1, start + 256 * 1024 - 1)

    def one() -> HttpResult:
        local = Reporter()
        return request_media_range(local, item["url"], frontend_origin, start, end)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: one(), range(4)))
    for result in results:
        reporter.add_http(result)
    statuses = [result.headers.get("x-teleplay-edge-cache") for result in results]
    hashes = {result.sha256 for result in results if result.sha256}
    reporter.check(
        f"{item['label']}: four concurrent requests succeed",
        all(result.status == 206 and result.bytes_read == end - start + 1 for result in results),
        statuses,
    )
    reporter.check(
        f"{item['label']}: concurrent responses are byte-identical",
        len(hashes) == 1,
        list(hashes),
    )
    reporter.check(
        f"{item['label']}: request coalescing observed",
        "COALESCED" in statuses or "HIT" in statuses,
        statuses,
        warning=True,
    )
    time.sleep(1.5)
    final = request_media_range(reporter, item["url"], frontend_origin, start, end)
    reporter.check(
        f"{item['label']}: concurrent target is cached afterwards",
        final.headers.get("x-teleplay-edge-cache") in {"HIT", "COALESCED"},
        final.headers.get("x-teleplay-edge-cache"),
    )


def ensure_admission_and_wait(
    reporter: Reporter,
    item: dict[str, Any],
    api_base: str,
    frontend_origin: str,
    origin_secret: str,
    *,
    max_wait: int,
    poll: int,
) -> dict[str, Any] | None:
    print(f"\n=== {item['label'].upper()} FILE: admission, encrypted L2 fill and readback ===")
    inspection = get_inspection(reporter, api_base, origin_secret, item)
    if inspection is None:
        return None

    admission = inspection.get("admission") or {}
    threshold = int(admission.get("threshold_bytes") or 0)
    served = int(admission.get("telegram_bytes_served") or 0)
    size = item["size"]
    max_cache = int(admission.get("max_cache_file_bytes") or 0)
    reporter.check(
        f"{item['label']}: file is within L2 max-size policy",
        max_cache <= 0 or size <= max_cache,
        f"size={size:,}, max={max_cache:,}",
    )

    entry = inspection.get("entry")
    if entry and entry.get("status") == "ready" and int(entry.get("encryption_version") or 0) == 1:
        print(f"ℹ️ {item['label']}: encrypted L2 is already ready; no Telegram admission transfer needed.")
    else:
        remaining = max(1, threshold - served)
        print(
            f"ℹ️ {item['label']}: admission threshold={threshold:,} bytes, "
            f"already recorded={served:,}, need about {remaining:,} more Telegram bytes."
        )
        cursor = 0
        safety = 0
        while remaining > 0 and safety < 256:
            safety += 1
            length = min(EDGE_CHUNK, remaining, size - cursor)
            if length <= 0:
                cursor = 0
                continue
            start = cursor
            end = start + length - 1
            result = request_media_range(
                reporter,
                origin_url(api_base, item),
                frontend_origin,
                start,
                end,
                extra_headers={"X-TelePlay-Origin-Secret": origin_secret},
                timeout=300,
            )
            reporter.check(
                f"{item['label']}: admission range {start:,}-{end:,}",
                result.status == 206 and result.bytes_read == length,
                f"HTTP {result.status}, source={result.headers.get('x-teleplay-origin-cache')}, bytes={result.bytes_read}",
            )
            if result.status != 206:
                return get_inspection(reporter, api_base, origin_secret, item)
            if result.headers.get("x-teleplay-origin-cache") == "GDRIVE":
                break
            remaining -= length
            cursor = end + 1
            if cursor >= size:
                cursor = 0

        inspection = get_inspection(reporter, api_base, origin_secret, item)
        if inspection is None:
            return None
        job = inspection.get("job") or {}
        entry = inspection.get("entry") or {}
        reporter.check(
            f"{item['label']}: durable fill job queued or active",
            entry.get("status") in {"queued", "uploading", "ready"}
            and job.get("status") in {"queued", "leased", "completed"},
            f"entry={entry.get('status')}, job={job.get('status')}, type={job.get('job_type')}",
        )

    deadline = time.monotonic() + max_wait
    last_status = None
    while time.monotonic() < deadline:
        inspection = get_inspection(reporter, api_base, origin_secret, item, check=False)
        if inspection is None:
            return None
        entry = inspection.get("entry") or {}
        job = inspection.get("job") or {}
        state = (
            entry.get("status"),
            entry.get("encryption_version"),
            job.get("status"),
            job.get("bytes_uploaded"),
        )
        if state != last_status:
            print(
                f"ℹ️ {item['label']}: entry={state[0]}, encryption={state[1]}, "
                f"job={state[2]}, uploaded={int(state[3] or 0):,}"
            )
            last_status = state
        if entry.get("status") == "ready" and int(entry.get("encryption_version") or 0) == 1:
            break
        if job.get("status") == "failed":
            reporter.check(
                f"{item['label']}: encrypted fill did not fail",
                False,
                job.get("last_error"),
            )
            return inspection
        time.sleep(max(2, poll))
    else:
        reporter.check(
            f"{item['label']}: encrypted L2 became ready within timeout",
            False,
            f"timeout={max_wait}s, last={last_status}",
        )
        return inspection

    entry = inspection.get("entry") or {}
    job = inspection.get("job") or {}
    drive = (inspection.get("drive") or {}).get("metadata") or {}
    expected_encrypted = size + 16 * ((size + MIB - 1) // MIB)

    reporter.check(f"{item['label']}: L2 entry READY", entry.get("status") == "ready", entry.get("status"))
    reporter.check(
        f"{item['label']}: AES-GCM cache format v1",
        int(entry.get("encryption_version") or 0) == 1,
        entry.get("encryption_version"),
    )
    reporter.check(
        f"{item['label']}: encrypted size matches 1 MiB block format",
        int(entry.get("encrypted_size_bytes") or 0) == expected_encrypted,
        f"actual={entry.get('encrypted_size_bytes')}, expected={expected_encrypted}",
    )
    reporter.check(
        f"{item['label']}: durable job completed",
        job.get("status") == "completed",
        f"{job.get('job_type')} / {job.get('status')}",
    )
    reporter.check(
        f"{item['label']}: Drive metadata available",
        bool(drive) and not (inspection.get("drive") or {}).get("error"),
        (inspection.get("drive") or {}).get("error"),
    )
    reporter.check(
        f"{item['label']}: Drive object has opaque random name",
        str(drive.get("name") or "").startswith("tp-e1-")
        and str(drive.get("name") or "").endswith(".bin")
        and item.get("name", "") not in str(drive.get("name") or ""),
        drive.get("name"),
    )
    reporter.check(
        f"{item['label']}: Drive object MIME is binary",
        drive.get("mime_type") == "application/octet-stream",
        drive.get("mime_type"),
    )
    reporter.check(
        f"{item['label']}: Drive metadata marks encrypted cache format",
        (drive.get("app_properties") or {}).get("teleplayCache") == "e1"
        and (drive.get("app_properties") or {}).get("cacheKey") == item["cache_key"],
        drive.get("app_properties"),
    )
    reporter.check(
        f"{item['label']}: Drive object is present and untrashed",
        int(drive.get("size") or 0) == expected_encrypted and not bool(drive.get("trashed")),
        f"size={drive.get('size')}, trashed={drive.get('trashed')}",
    )

    # Directly bypass L1 and prove the ready object is decrypted back to the
    # original plaintext range by Render.
    end = min(size - 1, MIB - 1)
    l2 = request_media_range(
        reporter,
        origin_url(api_base, item),
        frontend_origin,
        0,
        end,
        extra_headers={"X-TelePlay-Origin-Secret": origin_secret},
    )
    verify_range_response(reporter, f"{item['label']}: encrypted L2 readback", l2, 0, end, size, edge=False)
    reporter.check(
        f"{item['label']}: readback source is GDRIVE",
        l2.headers.get("x-teleplay-origin-cache") == "GDRIVE",
        l2.headers.get("x-teleplay-origin-cache"),
    )
    return inspection


def preflight(
    reporter: Reporter,
    api_base: str,
    frontend_origin: str,
    origin_secret: str,
) -> dict[str, Any] | None:
    print("=== PREFLIGHT ===")
    status_url = f"{api_base.rstrip('/')}/api/stream/cache-status"
    no_secret, _ = json_request(reporter, status_url)
    reporter.check("cache-status rejects unauthenticated request", no_secret.status == 403, f"HTTP {no_secret.status}")
    result, payload = json_request(
        reporter,
        status_url,
        headers={"X-TelePlay-Origin-Secret": origin_secret, "Origin": frontend_origin},
    )
    reporter.check("protected cache-status is reachable", result.status == 200, f"HTTP {result.status}")
    if result.status != 200 or payload is None:
        return None
    reporter.check("managed cache is enabled", payload.get("enabled") is True, payload.get("enabled"))
    reporter.check("cache mode is hybrid", payload.get("mode") == "hybrid", payload.get("mode"))
    reporter.check(
        "Drive circuit breaker is closed",
        payload.get("drive_circuit_open") is False,
        payload.get("drive_circuit_seconds_remaining"),
        warning=True,
    )
    return payload


def run_reconciliation(
    reporter: Reporter,
    api_base: str,
    origin_secret: str,
    *,
    expect_missing: int | None,
) -> dict[str, Any] | None:
    print("\n=== DRIVE RECONCILIATION ===")
    url = f"{api_base.rstrip('/')}/api/stream/cache-reconcile"
    result, payload = json_request(
        reporter,
        url,
        method="POST",
        headers={"X-TelePlay-Origin-Secret": origin_secret, "Content-Type": "application/json"},
        timeout=300,
    )
    reporter.check("manual reconciliation endpoint succeeds", result.status == 200, f"HTTP {result.status}")
    if result.status != 200 or payload is None:
        return None
    stats = payload.get("reconciliation") or {}
    reporter.check("reconciliation checked catalog entries", int(stats.get("checked") or 0) >= 0, stats)
    if expect_missing is not None:
        reporter.check(
            "manually deleted Drive objects detected",
            int(stats.get("missing") or 0) >= expect_missing,
            f"expected>={expect_missing}, actual={stats.get('missing')}",
        )
    else:
        print(f"ℹ️ reconciliation result: {json.dumps(stats, sort_keys=True)}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Full production validation for TelePlay Cloudflare L1 + encrypted Drive L2",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--api-base", default="https://api.telxstream.exa.qzz.io")
    parser.add_argument("--frontend-origin", default="https://telxstream.exa.qzz.io")
    parser.add_argument("--origin-secret", default=os.getenv("ORIGIN_SECRET"))
    parser.add_argument("--small-url", default=os.getenv("SMALL_EDGE_URL"))
    parser.add_argument("--large-url", default=os.getenv("LARGE_EDGE_URL"))
    parser.add_argument(
        "--full-fill",
        action="store_true",
        help="Trigger admission and wait for full encrypted Drive fills (can transfer substantial data)",
    )
    parser.add_argument("--max-wait", type=int, default=1800, help="seconds to wait per encrypted fill")
    parser.add_argument("--poll", type=int, default=10, help="cache-inspect polling interval")
    parser.add_argument("--reconcile", action="store_true", help="run Drive pointer reconciliation")
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="only test cache-status and manual Drive-deletion reconciliation; media URLs are not required",
    )
    parser.add_argument(
        "--expect-missing",
        type=int,
        default=None,
        help="after manually deleting Drive objects, require at least this many to be detected",
    )
    parser.add_argument("--skip-concurrency", action="store_true")
    parser.add_argument("--report", default=None, help="JSON report path")
    args = parser.parse_args()

    if not args.origin_secret:
        parser.error("--origin-secret or ORIGIN_SECRET is required")
    if args.expect_missing is not None:
        args.reconcile = True
    if args.reconcile_only:
        args.reconcile = True
    elif not args.small_url or not args.large_url:
        parser.error("--small-url/SMALL_EDGE_URL and --large-url/LARGE_EDGE_URL are required")

    small = large = None
    if not args.reconcile_only:
        try:
            small = parse_edge_url(args.small_url)
            large = parse_edge_url(args.large_url)
        except Exception as exc:
            parser.error(str(exc))
        small["label"] = "small"
        large["label"] = "large"

    reporter = Reporter()
    started = datetime.now(timezone.utc)
    print(f"TelePlay cache production test started at {started.isoformat()}")
    print(f"API: {args.api_base}")
    print(f"Frontend Origin: {args.frontend_origin}")
    if small is not None and large is not None:
        print(f"Small URL: {redact_url(small['url'])} ({small['size']:,} bytes)")
        print(f"Large URL: {redact_url(large['url'])} ({large['size']:,} bytes)")

    before = preflight(reporter, args.api_base, args.frontend_origin, args.origin_secret)

    if args.reconcile_only:
        reconciliation = run_reconciliation(
            reporter, args.api_base, args.origin_secret, expect_missing=args.expect_missing
        )
        after = preflight(reporter, args.api_base, args.frontend_origin, args.origin_secret)
        finished = datetime.now(timezone.utc)
        report = {
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": round((finished - started).total_seconds(), 2),
            "targets": {"api_base": args.api_base, "frontend_origin": args.frontend_origin},
            "options": {"reconcile_only": True, "expect_missing": args.expect_missing},
            "status_before": before,
            "status_after": after,
            "reconciliation": reconciliation,
            "checks": reporter.checks,
            "http_requests": reporter.http,
            "summary": {
                "passed": sum(1 for item in reporter.checks if item["state"] == "PASS"),
                "warnings": reporter.warnings,
                "failed": reporter.failed,
            },
        }
        report_path = Path(args.report or f"teleplay-cache-reconcile-{int(time.time())}.json")
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print("\n=== SUMMARY ===")
        print(
            f"Passed: {report['summary']['passed']} | Warnings: {report['summary']['warnings']} | "
            f"Failed: {report['summary']['failed']}"
        )
        print(f"Report: {report_path.resolve()}")
        return 1 if reporter.failed else 0
    reporter.check(
        "small test file is not larger than the large test file",
        small["size"] < large["size"],
        f"small={small['size']:,}, large={large['size']:,}",
    )

    small_info = get_inspection(reporter, args.api_base, args.origin_secret, small)
    large_info = get_inspection(reporter, args.api_base, args.origin_secret, large)
    if small_info:
        small["name"] = (small_info.get("file") or {}).get("name") or ""
        limit = int((small_info.get("admission") or {}).get("small_file_limit_bytes") or 0)
        reporter.check(
            "small file uses small-file admission policy",
            small["size"] <= limit,
            f"size={small['size']:,}, limit={limit:,}",
        )
    if large_info:
        large["name"] = (large_info.get("file") or {}).get("name") or ""
        limit = int((large_info.get("admission") or {}).get("small_file_limit_bytes") or 0)
        reporter.check(
            "large file exercises threshold-based admission",
            large["size"] > limit,
            f"size={large['size']:,}, small-limit={limit:,}",
        )

    verify_edge_and_origin(reporter, small, args.api_base, args.frontend_origin, args.origin_secret)
    verify_edge_and_origin(reporter, large, args.api_base, args.frontend_origin, args.origin_secret)

    if not args.skip_concurrency:
        print("\n=== WORKER CONCURRENCY / COALESCING ===")
        concurrency_probe(reporter, large, args.frontend_origin)

    small_fill = None
    large_fill = None
    if args.full_fill:
        small_fill = ensure_admission_and_wait(
            reporter,
            small,
            args.api_base,
            args.frontend_origin,
            args.origin_secret,
            max_wait=args.max_wait,
            poll=args.poll,
        )
        large_fill = ensure_admission_and_wait(
            reporter,
            large,
            args.api_base,
            args.frontend_origin,
            args.origin_secret,
            max_wait=args.max_wait,
            poll=args.poll,
        )
    else:
        reporter.check(
            "encrypted full-fill verification",
            False,
            "not run; pass --full-fill to trigger admission and wait for encrypted Drive uploads",
            warning=True,
        )

    final_small = get_inspection(reporter, args.api_base, args.origin_secret, small, check=False)
    final_large = get_inspection(reporter, args.api_base, args.origin_secret, large, check=False)
    for label, inspection in (("small", final_small), ("large", final_large)):
        metadata = ((inspection or {}).get("drive") or {}).get("metadata") or {}
        if metadata:
            print(
                f"ℹ️ {label} Drive object: name={metadata.get('name')}, "
                f"id={metadata.get('id')}, size={metadata.get('size')}"
            )

    reconciliation = None
    if args.reconcile:
        reconciliation = run_reconciliation(
            reporter,
            args.api_base,
            args.origin_secret,
            expect_missing=args.expect_missing,
        )

    after = preflight(reporter, args.api_base, args.frontend_origin, args.origin_secret)
    finished = datetime.now(timezone.utc)
    report = {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 2),
        "targets": {
            "api_base": args.api_base,
            "frontend_origin": args.frontend_origin,
            "small": {key: value for key, value in small.items() if key != "url"},
            "large": {key: value for key, value in large.items() if key != "url"},
        },
        "options": {
            "full_fill": args.full_fill,
            "reconcile": args.reconcile,
            "expect_missing": args.expect_missing,
            "max_wait": args.max_wait,
            "poll": args.poll,
        },
        "status_before": before,
        "status_after": after,
        "final_inspection": {"small": final_small, "large": final_large},
        "reconciliation": reconciliation,
        "checks": reporter.checks,
        "http_requests": reporter.http,
        "summary": {
            "passed": sum(1 for item in reporter.checks if item["state"] == "PASS"),
            "warnings": reporter.warnings,
            "failed": reporter.failed,
        },
    }
    report_path = Path(args.report or f"teleplay-cache-test-{int(time.time())}.json")
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    print("\n=== SUMMARY ===")
    print(
        f"Passed: {report['summary']['passed']} | "
        f"Warnings: {report['summary']['warnings']} | Failed: {report['summary']['failed']}"
    )
    print(f"Report: {report_path.resolve()}")
    return 1 if reporter.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
