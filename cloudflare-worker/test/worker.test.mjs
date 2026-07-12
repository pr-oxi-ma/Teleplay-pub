import assert from 'node:assert/strict';
import test from 'node:test';

const stored = new Map();
globalThis.caches = {
  default: {
    async put(request, response) {
      const body = await response.arrayBuffer();
      stored.set(request.url, {
        body,
        status: response.status,
        headers: [...response.headers.entries()],
      });
    },
    async match(request) {
      const item = stored.get(request.url);
      if (!item) return undefined;
      return new Response(item.body.slice(0), {
        status: item.status,
        headers: item.headers,
      });
    },
  },
};

const fileSize = 5 * 1024 * 1024;
const originBody = new Uint8Array(fileSize);
for (let index = 0; index < originBody.length; index += 1) originBody[index] = index % 251;

const publicHashes = {
  proxy: '1'.repeat(32),
  redirect: '2'.repeat(32),
  off: '3'.repeat(32),
};
const assetKey = 'a'.repeat(64);
let originCalls = 0;
let resolverCalls = 0;
let touchCalls = 0;

globalThis.fetch = async (input, init = {}) => {
  const url = typeof input === 'string' ? input : input.url;
  if (url.endsWith('/api/stream/cache-touch')) {
    touchCalls += 1;
    return new Response('{"touched":true}', { status: 200 });
  }

  const resolverMatch = /\/api\/stream\/public-resolve\/([a-f0-9]{32,64})/.exec(url);
  if (resolverMatch) {
    resolverCalls += 1;
    const hash = resolverMatch[1];
    const mode = hash === publicHashes.redirect ? 'redirect' : hash === publicHashes.off ? 'off' : 'proxy';
    return Response.json({
      mode,
      version: 2,
      file_id: 69,
      size: fileSize,
      cache_key: assetKey,
      mime_type: 'audio/mp4',
      content_disposition: 'inline; filename="track.m4a"',
      edge_url: mode === 'redirect'
        ? 'https://worker.example/media/69/signed-target'
        : null,
    });
  }

  const originMatch = /\/api\/stream\/origin\/69/.exec(url);
  assert.ok(originMatch, `Unexpected fetch URL: ${url}`);
  originCalls += 1;
  const range = new Headers(init.headers).get('Range');
  const match = /^bytes=(\d+)-(\d+)$/.exec(range || '');
  assert.ok(match, `Missing/invalid origin range: ${range}`);
  const start = Number(match[1]);
  const end = Number(match[2]);
  const body = originBody.slice(start, end + 1);
  return new Response(body, {
    status: 206,
    headers: {
      'Content-Type': 'audio/mp4',
      'Content-Disposition': 'inline; filename="track.m4a"',
      'Content-Length': String(body.byteLength),
      'Content-Range': `bytes ${start}-${end}/${fileSize}`,
      'X-TelePlay-Origin-Cache': 'GDRIVE',
    },
  });
};

const worker = (await import('../src/index.js')).default;
const env = {
  RENDER_ORIGIN: 'https://api.example.test',
  ALLOWED_ORIGINS: 'https://web.example.test',
  EDGE_CHUNK_SIZE_BYTES: String(4 * 1024 * 1024),
  EDGE_TOUCH_INTERVAL_SECONDS: '3600',
  EDGE_SIGNING_SECRET: 's'.repeat(64),
  ORIGIN_SECRET: 'o'.repeat(64),
  TOUCH_SECRET: 't'.repeat(64),
};

async function hmacHex(secret, message) {
  const key = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'],
  );
  const bytes = new Uint8Array(
    await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(message)),
  );
  return [...bytes].map((value) => value.toString(16).padStart(2, '0')).join('');
}

async function signedRequest(extraQuery = '') {
  const version = 2;
  const fileId = 69;
  const expires = Math.floor(Date.now() / 1000) + 3600;
  const token = 'b'.repeat(16);
  const sig = await hmacHex(
    env.EDGE_SIGNING_SECRET,
    `${version}.${fileId}.${assetKey}.${fileSize}.${expires}.${token}`,
  );
  return new Request(
    `https://worker.example/media/${fileId}/${assetKey}?v=${version}&size=${fileSize}&expires=${expires}&token=${token}&sig=${sig}${extraQuery}`,
    {
      headers: {
        Origin: 'https://web.example.test',
        Range: 'bytes=0-1048575',
      },
    },
  );
}

function publicRequest(hash, query = '') {
  return new Request(
    `https://api.example.test/api/stream/s/${hash}/track.m4a${query}`,
    {
      headers: {
        Origin: 'https://web.example.test',
        Range: 'bytes=0-1048575',
      },
      redirect: 'manual',
    },
  );
}

function context() {
  const promises = [];
  return {
    promises,
    waitUntil(promise) { promises.push(Promise.resolve(promise)); },
  };
}

async function finish(ctx) {
  await Promise.all(ctx.promises);
}

test('signed and public proxy URLs share one canonical L1 chunk', async () => {
  const firstCtx = context();
  const first = await worker.fetch(await signedRequest(), env, firstCtx);
  assert.equal(first.status, 206);
  assert.equal(first.headers.get('X-TelePlay-Edge-Cache'), 'MISS');
  assert.equal(first.headers.get('X-TelePlay-Origin-Cache'), 'GDRIVE');
  const firstBytes = new Uint8Array(await first.arrayBuffer());
  assert.deepEqual(firstBytes, originBody.slice(0, 1024 * 1024));
  await finish(firstCtx);
  assert.equal(originCalls, 1);

  const proxyCtx = context();
  const proxy = await worker.fetch(publicRequest(publicHashes.proxy), env, proxyCtx);
  assert.equal(proxy.status, 206);
  assert.equal(proxy.headers.get('X-TelePlay-Public-Mode'), 'PROXY');
  assert.equal(proxy.headers.get('X-TelePlay-Edge-Cache'), 'HIT');
  assert.deepEqual(new Uint8Array(await proxy.arrayBuffer()), firstBytes);
  await finish(proxyCtx);
  assert.equal(originCalls, 1, 'public URL must reuse signed URL chunk');
  assert.equal(resolverCalls, 1);

  const repeatCtx = context();
  const repeat = await worker.fetch(publicRequest(publicHashes.proxy), env, repeatCtx);
  assert.equal(repeat.headers.get('X-TelePlay-Edge-Cache'), 'HIT');
  await repeat.arrayBuffer();
  await finish(repeatCtx);
  assert.equal(resolverCalls, 1, 'public metadata should be cached briefly');
  assert.equal(touchCalls, 1, 'popularity touch is sampled');
});

test('redirect mode returns a temporary signed edge redirect', async () => {
  const ctx = context();
  const response = await worker.fetch(publicRequest(publicHashes.redirect), env, ctx);
  assert.equal(response.status, 307);
  assert.equal(response.headers.get('Location'), 'https://worker.example/media/69/signed-target');
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
  assert.equal(response.headers.get('X-TelePlay-Public-Mode'), 'REDIRECT');
  await finish(ctx);
});

test('off mode bypasses L1 while retaining Drive/Telegram origin selection', async () => {
  const before = originCalls;
  const ctx = context();
  const response = await worker.fetch(publicRequest(publicHashes.off), env, ctx);
  assert.equal(response.status, 206);
  assert.equal(response.headers.get('X-TelePlay-Public-Mode'), 'OFF');
  assert.equal(response.headers.get('X-TelePlay-Edge-Cache'), 'BYPASS');
  assert.equal(response.headers.get('X-TelePlay-Origin-Cache'), 'GDRIVE');
  assert.deepEqual(
    new Uint8Array(await response.arrayBuffer()),
    originBody.slice(0, 1024 * 1024),
  );
  await finish(ctx);
  assert.equal(originCalls, before + 1);
});

test('download flag changes disposition without creating a second media chunk', async () => {
  const before = originCalls;
  const ctx = context();
  const response = await worker.fetch(publicRequest(publicHashes.proxy, '?download=1'), env, ctx);
  assert.equal(response.status, 206);
  assert.match(response.headers.get('Content-Disposition') || '', /^attachment;/i);
  assert.equal(response.headers.get('X-TelePlay-Edge-Cache'), 'HIT');
  await response.arrayBuffer();
  await finish(ctx);
  assert.equal(originCalls, before);
});
