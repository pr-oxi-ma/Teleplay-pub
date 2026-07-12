const encoder = new TextEncoder();
const inflightChunks = new Map();
const lastTouches = new Map();
// Bump when cached response headers/body semantics change. This invalidates
// old edge objects without invalidating Google Drive cache entries.
const CACHE_REPRESENTATION_VERSION = 2;
const PUBLIC_METADATA_TTL_SECONDS = 30;

function hex(buffer) {
  return [...new Uint8Array(buffer)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

function timingSafeEqual(left, right) {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

async function hmacHex(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return hex(await crypto.subtle.sign("HMAC", key, encoder.encode(message)));
}

function configuredOrigins(env) {
  return (env.ALLOWED_ORIGINS || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
}

function configurationError(env) {
  const missing = [];
  if (!env.RENDER_ORIGIN) missing.push("RENDER_ORIGIN");
  if (!env.EDGE_SIGNING_SECRET) missing.push("EDGE_SIGNING_SECRET");
  if (!env.ORIGIN_SECRET) missing.push("ORIGIN_SECRET");
  if (!env.TOUCH_SECRET) missing.push("TOUCH_SECRET");
  if (configuredOrigins(env).length === 0) missing.push("ALLOWED_ORIGINS");
  return missing.length ? `Missing Worker configuration: ${missing.join(", ")}` : null;
}

function corsHeaders(request, env) {
  const origin = request.headers.get("Origin");
  const allowedOrigins = configuredOrigins(env);
  const allowed = Boolean(origin && allowedOrigins.includes(origin));
  const headers = new Headers({
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Range, Content-Type",
    "Access-Control-Expose-Headers":
      "Content-Length, Content-Range, Accept-Ranges, Content-Type, Content-Disposition, Location, X-TelePlay-Edge-Cache, X-TelePlay-Origin-Cache, X-TelePlay-Public-Mode",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  });
  if (allowed) {
    headers.set("Access-Control-Allow-Origin", origin);
    headers.set("Access-Control-Allow-Credentials", "true");
  }
  return { headers, allowed: !origin || allowed };
}

function withCors(response, request, env) {
  const output = new Headers(response.headers);
  const cors = corsHeaders(request, env);
  for (const [key, value] of cors.headers.entries()) output.set(key, value);
  output.set("Cross-Origin-Resource-Policy", "cross-origin");
  output.set("X-Content-Type-Options", "nosniff");
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: output,
  });
}

function parseRange(header, size) {
  if (!header) return null;
  if (header.includes(",")) return { error: true };
  const match = /^bytes=(\d*)-(\d*)$/i.exec(header.trim());
  if (!match) return { error: true };
  const [, startText, endText] = match;
  if (!startText && !endText) return { error: true };

  let start;
  let end;
  if (!startText) {
    const suffix = Number(endText);
    if (!Number.isSafeInteger(suffix) || suffix <= 0) return { error: true };
    start = Math.max(0, size - suffix);
    end = size - 1;
  } else {
    start = Number(startText);
    end = endText ? Number(endText) : size - 1;
  }
  if (
    !Number.isSafeInteger(start) ||
    !Number.isSafeInteger(end) ||
    start < 0 ||
    start >= size ||
    end < start
  ) {
    return { error: true };
  }
  return { start, end: Math.min(end, size - 1) };
}

function chunkCacheKey(version, assetKey, chunkIndex) {
  return new Request(
    `https://teleplay-edge-cache.invalid/r${CACHE_REPRESENTATION_VERSION}/v${version}/${assetKey}/${chunkIndex}`,
    { method: "GET" },
  );
}

function publicMetadataCacheKey(publicHash, forceDownload) {
  return new Request(
    `https://teleplay-edge-cache.invalid/public/v1/${publicHash}/${forceDownload ? 1 : 0}`,
    { method: "GET" },
  );
}

async function fetchOriginRange({ env, fileId, assetKey, start, end }) {
  const origin = env.RENDER_ORIGIN.replace(/\/+$/, "");
  const url = `${origin}/api/stream/origin/${fileId}?cache_key=${assetKey}`;
  return fetch(url, {
    method: "GET",
    headers: {
      Range: `bytes=${start}-${end}`,
      "X-TelePlay-Origin-Secret": env.ORIGIN_SECRET,
      "Accept-Encoding": "identity",
    },
    redirect: "follow",
  });
}

function selectedOriginHeaders(originResponse, bodyLength, assetKey, chunkIndex) {
  const headers = new Headers();
  for (const name of ["Content-Type", "Content-Disposition", "X-TelePlay-Origin-Cache"]) {
    const value = originResponse.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set("Content-Type", headers.get("Content-Type") || "application/octet-stream");
  headers.set("Content-Length", String(bodyLength));
  headers.set("Cache-Control", "public, max-age=2592000, immutable");
  headers.set("ETag", `"${assetKey}-${chunkIndex}-${bodyLength}"`);
  return headers;
}

function applyDownloadDisposition(headers, forceDownload) {
  if (!forceDownload) return;
  const current = headers.get("Content-Disposition");
  if (current) {
    headers.set(
      "Content-Disposition",
      current.replace(/^\s*(inline|attachment)\s*;/i, "attachment;"),
    );
  } else {
    headers.set("Content-Disposition", "attachment");
  }
}

function sliceReadableStream(body, skipBytes, outputBytes) {
  const reader = body.getReader();
  let skipped = 0;
  let emitted = 0;
  return new ReadableStream({
    async pull(controller) {
      while (emitted < outputBytes) {
        const { value, done } = await reader.read();
        if (done) {
          controller.error(new Error("Cached media body ended early"));
          return;
        }
        let chunk = value;
        if (skipped < skipBytes) {
          const discard = Math.min(chunk.byteLength, skipBytes - skipped);
          skipped += discard;
          chunk = chunk.subarray(discard);
          if (chunk.byteLength === 0) continue;
        }
        const remaining = outputBytes - emitted;
        if (chunk.byteLength > remaining) chunk = chunk.subarray(0, remaining);
        emitted += chunk.byteLength;
        controller.enqueue(chunk);
        if (emitted >= outputBytes) {
          controller.close();
          reader.cancel().catch(() => {});
        }
        return;
      }
      controller.close();
    },
    async cancel(reason) {
      await reader.cancel(reason).catch(() => {});
    },
  });
}

async function loadOriginChunk({ env, ctx, fileId, assetKey, size, chunkIndex, chunkSize, cacheKey }) {
  const start = chunkIndex * chunkSize;
  const end = Math.min(size - 1, start + chunkSize - 1);
  const originResponse = await fetchOriginRange({ env, fileId, assetKey, start, end });
  if (originResponse.status !== 206 && !(originResponse.status === 200 && start === 0 && end === size - 1)) {
    const detail = await originResponse.text().catch(() => "Origin error");
    throw new Error(`Origin HTTP ${originResponse.status}: ${detail.slice(0, 160)}`);
  }
  if (!originResponse.body) throw new Error("Origin returned no media body");

  const expectedLength = end - start + 1;
  const declaredLength = Number(originResponse.headers.get("Content-Length") || expectedLength);
  if (declaredLength !== expectedLength) {
    throw new Error(`Origin declared ${declaredLength} bytes; expected ${expectedLength}`);
  }

  const headers = selectedOriginHeaders(originResponse, expectedLength, assetKey, chunkIndex);
  const [viewerBody, cacheBody] = originResponse.body.tee();
  const cachePromise = caches.default.put(
    cacheKey,
    new Response(cacheBody, { status: 200, headers }),
  );
  const inflightKey = `${assetKey}:${chunkIndex}`;
  const tracked = cachePromise.finally(() => inflightChunks.delete(inflightKey));
  inflightChunks.set(inflightKey, tracked);
  ctx.waitUntil(tracked.catch((error) => console.warn("TelePlay edge cache put failed", error)));
  return { body: viewerBody, headers, state: "MISS" };
}

async function getEdgeChunk(args) {
  const { version, assetKey, chunkIndex } = args;
  const cacheKey = chunkCacheKey(version, assetKey, chunkIndex);
  let cached = await caches.default.match(cacheKey);
  if (cached?.body) {
    return { body: cached.body, headers: new Headers(cached.headers), state: "HIT" };
  }

  const inflightKey = `${assetKey}:${chunkIndex}`;
  const existing = inflightChunks.get(inflightKey);
  if (existing) {
    try {
      await existing;
    } catch (error) {
      console.warn("TelePlay coalesced cache write failed; retrying origin", error);
    }
    cached = await caches.default.match(cacheKey);
    if (cached?.body) {
      return { body: cached.body, headers: new Headers(cached.headers), state: "COALESCED" };
    }
  }
  return loadOriginChunk({ ...args, cacheKey });
}

async function sendPopularityTouch(env, assetKey) {
  const nowSeconds = Math.floor(Date.now() / 1000);
  const signature = await hmacHex(env.TOUCH_SECRET, `${assetKey}.${nowSeconds}`);
  const origin = env.RENDER_ORIGIN.replace(/\/+$/, "");
  await fetch(`${origin}/api/stream/cache-touch`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-TelePlay-Touch-Timestamp": String(nowSeconds),
      "X-TelePlay-Touch-Signature": signature,
    },
    body: JSON.stringify({ cache_key: assetKey }),
  });
}

function maybeTouch(ctx, env, assetKey, state) {
  if (state !== "HIT" && state !== "COALESCED") return;
  const interval = Math.max(3600, Number(env.EDGE_TOUCH_INTERVAL_SECONDS || 21600));
  const now = Date.now();
  const previous = lastTouches.get(assetKey) || 0;
  if (now - previous < interval * 1000) return;
  lastTouches.set(assetKey, now);
  if (lastTouches.size > 5000) lastTouches.clear();
  ctx.waitUntil(sendPopularityTouch(env, assetKey).catch((error) => {
    console.warn("TelePlay edge popularity touch failed", error);
  }));
}

function invalidRangeResponse(size, request, env) {
  return withCors(
    new Response("Range Not Satisfiable", {
      status: 416,
      headers: { "Content-Range": `bytes */${size}` },
    }),
    request,
    env,
  );
}

async function serveOriginBypass(request, env, asset, parsedRange) {
  const requested = parsedRange || { start: 0, end: asset.size - 1 };
  const originResponse = await fetchOriginRange({
    env,
    fileId: asset.fileId,
    assetKey: asset.assetKey,
    start: requested.start,
    end: requested.end,
  });
  if (originResponse.status !== 206 && !(originResponse.status === 200 && !parsedRange)) {
    const detail = await originResponse.text().catch(() => "Origin error");
    return withCors(
      new Response(`Origin HTTP ${originResponse.status}: ${detail.slice(0, 160)}`, { status: 502 }),
      request,
      env,
    );
  }

  const headers = new Headers(originResponse.headers);
  const outputLength = requested.end - requested.start + 1;
  headers.set("Content-Length", String(outputLength));
  headers.set("Accept-Ranges", "bytes");
  headers.set("Cache-Control", "private, max-age=3600, no-transform");
  headers.set("X-TelePlay-Edge-Cache", "BYPASS");
  if (asset.publicMode) headers.set("X-TelePlay-Public-Mode", asset.publicMode.toUpperCase());
  applyDownloadDisposition(headers, asset.forceDownload);

  if (parsedRange) {
    headers.set("Content-Range", `bytes ${requested.start}-${requested.end}/${asset.size}`);
    return withCors(new Response(originResponse.body, { status: 206, headers }), request, env);
  }
  headers.delete("Content-Range");
  return withCors(new Response(originResponse.body, { status: 200, headers }), request, env);
}

async function serveAsset(request, env, ctx, asset) {
  const configured = Number(env.EDGE_CHUNK_SIZE_BYTES || 4 * 1024 * 1024);
  const chunkSize = Number.isSafeInteger(configured) && configured > 0
    ? Math.min(32 * 1024 * 1024, Math.max(256 * 1024, configured))
    : 4 * 1024 * 1024;
  const parsedRange = parseRange(request.headers.get("Range"), asset.size);
  if (parsedRange?.error) return invalidRangeResponse(asset.size, request, env);

  if (request.method === "HEAD") {
    const headers = new Headers({
      "Content-Length": String(asset.size),
      "Accept-Ranges": "bytes",
      "Cache-Control": "private, max-age=60",
      "X-TelePlay-Edge-Cache": "METADATA",
    });
    if (asset.mimeType) headers.set("Content-Type", asset.mimeType);
    if (asset.contentDisposition) headers.set("Content-Disposition", asset.contentDisposition);
    if (asset.publicMode) headers.set("X-TelePlay-Public-Mode", asset.publicMode.toUpperCase());
    applyDownloadDisposition(headers, asset.forceDownload);
    return withCors(new Response(null, { status: 200, headers }), request, env);
  }

  if (asset.bypassEdgeCache) {
    return serveOriginBypass(request, env, asset, parsedRange);
  }

  if (!parsedRange && asset.size > chunkSize) {
    const originResponse = await fetchOriginRange({
      env,
      fileId: asset.fileId,
      assetKey: asset.assetKey,
      start: 0,
      end: asset.size - 1,
    });
    const headers = new Headers(originResponse.headers);
    headers.delete("Content-Range");
    headers.set("Content-Length", String(asset.size));
    headers.set("X-TelePlay-Edge-Cache", "BYPASS");
    if (asset.publicMode) headers.set("X-TelePlay-Public-Mode", asset.publicMode.toUpperCase());
    applyDownloadDisposition(headers, asset.forceDownload);
    return withCors(
      new Response(originResponse.body, {
        status: originResponse.status === 206 ? 200 : originResponse.status,
        headers,
      }),
      request,
      env,
    );
  }

  const requested = parsedRange || { start: 0, end: asset.size - 1 };
  const chunkIndex = Math.floor(requested.start / chunkSize);
  const chunkStart = chunkIndex * chunkSize;
  const chunkEnd = Math.min(asset.size - 1, chunkStart + chunkSize - 1);
  const responseEnd = Math.min(requested.end, chunkEnd);

  let result;
  try {
    result = await getEdgeChunk({
      version: asset.version,
      env,
      ctx,
      fileId: asset.fileId,
      assetKey: asset.assetKey,
      size: asset.size,
      chunkIndex,
      chunkSize,
    });
  } catch (error) {
    console.error("TelePlay origin chunk failure", error);
    return withCors(new Response("Origin cache miss failed", { status: 502 }), request, env);
  }

  maybeTouch(ctx, env, asset.assetKey, result.state);
  const relativeStart = requested.start - chunkStart;
  const outputLength = responseEnd - requested.start + 1;
  const sliced = sliceReadableStream(result.body, relativeStart, outputLength);
  const headers = new Headers(result.headers);
  headers.set("Content-Length", String(outputLength));
  headers.set("Accept-Ranges", "bytes");
  headers.set("Cache-Control", "private, max-age=3600, no-transform");
  headers.set("X-TelePlay-Edge-Cache", result.state);
  if (asset.publicMode) headers.set("X-TelePlay-Public-Mode", asset.publicMode.toUpperCase());
  applyDownloadDisposition(headers, asset.forceDownload);

  if (parsedRange) {
    headers.set("Content-Range", `bytes ${requested.start}-${responseEnd}/${asset.size}`);
    return withCors(new Response(sliced, { status: 206, headers }), request, env);
  }
  headers.delete("Content-Range");
  return withCors(new Response(sliced, { status: 200, headers }), request, env);
}

async function handleMedia(request, env, ctx, match) {
  const [, fileIdText, assetKey] = match;
  const url = new URL(request.url);
  const version = Number(url.searchParams.get("v"));
  const size = Number(url.searchParams.get("size"));
  const expires = Number(url.searchParams.get("expires"));
  const tokenId = url.searchParams.get("token") || "";
  const suppliedSignature = (url.searchParams.get("sig") || "").toLowerCase();
  const fileId = Number(fileIdText);

  if (
    !Number.isSafeInteger(version) || version <= 0 ||
    !Number.isSafeInteger(fileId) || fileId <= 0 ||
    !/^[a-f0-9]{64}$/.test(assetKey) ||
    !Number.isSafeInteger(size) || size <= 0 ||
    !Number.isSafeInteger(expires) || expires < Math.floor(Date.now() / 1000) ||
    !/^[a-f0-9]{16}$/.test(tokenId) ||
    !/^[a-f0-9]{64}$/.test(suppliedSignature)
  ) {
    return withCors(new Response("Invalid or expired media URL", { status: 403 }), request, env);
  }

  const expected = await hmacHex(
    env.EDGE_SIGNING_SECRET,
    `${version}.${fileId}.${assetKey}.${size}.${expires}.${tokenId}`,
  );
  if (!timingSafeEqual(expected, suppliedSignature)) {
    return withCors(new Response("Invalid media signature", { status: 403 }), request, env);
  }

  return serveAsset(request, env, ctx, {
    version,
    fileId,
    assetKey,
    size,
    forceDownload: url.searchParams.get("download") === "1",
    bypassEdgeCache: false,
  });
}

function validatePublicMetadata(data) {
  if (!data || !["off", "redirect", "proxy"].includes(data.mode)) {
    throw new Error("Origin returned an invalid public stream mode");
  }
  if (
    !Number.isSafeInteger(data.version) || data.version <= 0 ||
    !Number.isSafeInteger(data.file_id) || data.file_id <= 0 ||
    !Number.isSafeInteger(data.size) || data.size <= 0 ||
    typeof data.cache_key !== "string" || !/^[a-f0-9]{64}$/.test(data.cache_key)
  ) {
    throw new Error("Origin returned invalid public stream metadata");
  }
  if (data.mode === "redirect" && (typeof data.edge_url !== "string" || !data.edge_url.startsWith("https://"))) {
    throw new Error("Origin did not return a valid redirect target");
  }
  return data;
}

async function resolvePublicAsset(env, ctx, publicHash, forceDownload) {
  const cacheKey = publicMetadataCacheKey(publicHash, forceDownload);
  const cached = await caches.default.match(cacheKey);
  if (cached) {
    const data = validatePublicMetadata(await cached.json());
    return { data, state: "HIT" };
  }

  const origin = env.RENDER_ORIGIN.replace(/\/+$/, "");
  const response = await fetch(
    `${origin}/api/stream/public-resolve/${publicHash}?download=${forceDownload ? 1 : 0}`,
    {
      method: "GET",
      headers: {
        "X-TelePlay-Origin-Secret": env.ORIGIN_SECRET,
        "Accept-Encoding": "identity",
      },
      redirect: "follow",
    },
  );
  if (response.status === 404) return { data: null, state: "MISS" };
  if (!response.ok) {
    const detail = await response.text().catch(() => "Resolver error");
    throw new Error(`Public resolver HTTP ${response.status}: ${detail.slice(0, 160)}`);
  }

  const data = validatePublicMetadata(await response.json());
  const cacheResponse = new Response(JSON.stringify(data), {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": `public, max-age=${PUBLIC_METADATA_TTL_SECONDS}`,
    },
  });
  ctx.waitUntil(
    caches.default.put(cacheKey, cacheResponse).catch((error) => {
      console.warn("TelePlay public metadata cache put failed", error);
    }),
  );
  return { data, state: "MISS" };
}

async function handlePublicStream(request, env, ctx, match) {
  const publicHash = match[1];
  const url = new URL(request.url);
  const forceDownload = url.searchParams.get("download") === "1";
  const resolved = await resolvePublicAsset(env, ctx, publicHash, forceDownload);
  const metadata = resolved.data;
  if (!metadata) {
    return withCors(new Response("File not found or link revoked", { status: 404 }), request, env);
  }

  if (metadata.mode === "redirect") {
    return withCors(
      new Response(null, {
        status: 307,
        headers: {
          Location: metadata.edge_url,
          "Cache-Control": "no-store",
          "X-TelePlay-Public-Mode": "REDIRECT",
          "X-TelePlay-Edge-Cache": `METADATA-${resolved.state}`,
        },
      }),
      request,
      env,
    );
  }

  return serveAsset(request, env, ctx, {
    version: metadata.version,
    fileId: metadata.file_id,
    assetKey: metadata.cache_key,
    size: metadata.size,
    mimeType: metadata.mime_type || "application/octet-stream",
    contentDisposition: metadata.content_disposition || null,
    forceDownload,
    bypassEdgeCache: metadata.mode === "off",
    publicMode: metadata.mode,
  });
}

export default {
  async fetch(request, env, ctx) {
    const configProblem = configurationError(env);
    if (configProblem) {
      console.error(configProblem);
      return new Response("Worker is not configured", { status: 500 });
    }

    const cors = corsHeaders(request, env);
    if (!cors.allowed) {
      return new Response("Origin not allowed", { status: 403, headers: cors.headers });
    }
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors.headers });
    }
    if (!["GET", "HEAD"].includes(request.method)) {
      return withCors(new Response("Method Not Allowed", { status: 405 }), request, env);
    }

    const pathname = new URL(request.url).pathname;
    const mediaMatch = /^\/media\/(\d+)\/([a-f0-9]{64})$/.exec(pathname);
    const publicMatch = /^\/api\/stream\/s\/([a-f0-9]{32,64})(?:\/.*)?$/.exec(pathname);

    try {
      if (mediaMatch) return await handleMedia(request, env, ctx, mediaMatch);
      if (publicMatch) return await handlePublicStream(request, env, ctx, publicMatch);
      return withCors(new Response("Not Found", { status: 404 }), request, env);
    } catch (error) {
      console.error("TelePlay edge cache error", error);
      return withCors(new Response("Edge cache failure", { status: 502 }), request, env);
    }
  },
};
