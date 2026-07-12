# Production cache validation

This guide verifies the deployed TelePlay cache for both small and large files,
including the clean custom-domain public redirect.

## What to verify

- readable raw URL returns HTTP 307 to `https://l1-media.exa.qzz.io/media/...`;
- redirected request returns valid `206 Partial Content`;
- first uncached edge request reports `MISS` and the repeat reports `HIT`;
- signed URL validation, CORS, invalid signatures and invalid ranges work;
- Cloudflare bytes match direct-origin bytes;
- small files enter encrypted Drive L2 after admission;
- large files span multiple aligned edge chunks and use threshold admission;
- Drive objects are opaque `tp-e1-*.bin` objects using
  `application/octet-stream`;
- direct origin later reports `X-TelePlay-Origin-Cache: GDRIVE`;
- concurrent requests do not create duplicate fill jobs or Drive objects;
- manually deleted Drive objects are detected by reconciliation.

## Test inputs

Use two fresh files:

- small: 1-4 MiB;
- large: 30-100 MiB.

Fresh files make the cold `MISS -> Telegram -> encrypted Drive fill` path easier
to observe. Already-played files are still valid, but may start with an L1 HIT
or an L2 GDRIVE response.

Define:

```bash
export ORIGIN_SECRET='the exact Render CLOUDFLARE_ORIGIN_SECRET'
export FRONTEND_ORIGIN='https://telxstream.exa.qzz.io'
export API_BASE='https://api.telxstream.exa.qzz.io'

export SMALL_RAW_URL='https://api.telxstream.exa.qzz.io/api/stream/s/<small-hash>/<filename>'
export LARGE_RAW_URL='https://api.telxstream.exa.qzz.io/api/stream/s/<large-hash>/<filename>'
```

## 1. Verify public redirect

```bash
curl -sS -D /tmp/small-redirect.headers -o /dev/null \
  -H 'Range: bytes=0-1048575' \
  "$SMALL_RAW_URL"

cat /tmp/small-redirect.headers
```

Expected:

```text
HTTP/2 307
location: https://l1-media.exa.qzz.io/media/...
cache-control: no-store
x-teleplay-public-mode: REDIRECT
```

The redirect target should not contain the old account-specific
`*.workers.dev` hostname.

Extract the signed cache URL:

```bash
SMALL_EDGE_URL="$(awk 'BEGIN{IGNORECASE=1} /^location:/ {$1=""; sub(/^ /,""); sub(/\r$/,""); print; exit}' /tmp/small-redirect.headers)"
printf 'Small cache URL: %s\n' "$SMALL_EDGE_URL"
```

Repeat for the large file:

```bash
curl -sS -D /tmp/large-redirect.headers -o /dev/null \
  -H 'Range: bytes=0-1048575' \
  "$LARGE_RAW_URL"

LARGE_EDGE_URL="$(awk 'BEGIN{IGNORECASE=1} /^location:/ {$1=""; sub(/^ /,""); sub(/\r$/,""); print; exit}' /tmp/large-redirect.headers)"
printf 'Large cache URL: %s\n' "$LARGE_EDGE_URL"
```

Signed URLs are temporary. Do not publish them or persist them in public logs.

## 2. Verify L1 MISS/HIT and byte ranges

First request:

```bash
curl -sS -D /tmp/small-edge-1.headers -o /tmp/small-edge-1.bin \
  -H "Origin: $FRONTEND_ORIGIN" \
  -H 'Range: bytes=0-1048575' \
  "$SMALL_EDGE_URL"

cat /tmp/small-edge-1.headers
```

Expected status and headers:

```text
HTTP/2 206
content-range: bytes 0-.../<file-size>
x-teleplay-edge-cache: MISS
```

A first-request `HIT` is valid if the same canonical chunk was already warmed.

Repeat the exact request:

```bash
curl -sS -D /tmp/small-edge-2.headers -o /tmp/small-edge-2.bin \
  -H "Origin: $FRONTEND_ORIGIN" \
  -H 'Range: bytes=0-1048575' \
  "$SMALL_EDGE_URL"

cat /tmp/small-edge-2.headers
cmp /tmp/small-edge-1.bin /tmp/small-edge-2.bin
```

Expected repeat state:

```text
x-teleplay-edge-cache: HIT
```

Run the same commands for `LARGE_EDGE_URL`. Also test a second aligned area:

```bash
curl -sS -D - -o /dev/null \
  -H "Origin: $FRONTEND_ORIGIN" \
  -H 'Range: bytes=4194304-5242879' \
  "$LARGE_EDGE_URL"
```

This checks a different 4 MiB L1 object.

## 3. Verify redirect-follow behavior directly

```bash
curl -sS -L -D - -o /dev/null \
  -H "Origin: $FRONTEND_ORIGIN" \
  -H 'Range: bytes=0-1048575' \
  "$SMALL_RAW_URL"
```

The header chain should show the API `307`, then the Worker `206`.

## 4. Verify private-origin protection

Without the secret:

```bash
curl -sS -D - -o /dev/null \
  "$API_BASE/api/stream/cache-status"
```

Expected: `403`.

With the secret:

```bash
curl -sS \
  -H "X-TelePlay-Origin-Secret: $ORIGIN_SECRET" \
  "$API_BASE/api/stream/cache-status" \
  | python -m json.tool
```

Expected: `200` and a cache status JSON object.

## 5. Verify encrypted Drive L2

The project also includes `tools/test_production_cache.py` for deep cache-state
inspection. Supply fresh signed edge URLs extracted from the redirects:

```bash
export SMALL_EDGE_URL
export LARGE_EDGE_URL

python tools/test_production_cache.py \
  --full-fill \
  --max-wait 1800 \
  --poll 10 \
  --report teleplay-cache-full.json
```

It verifies:

- edge signatures, CORS and ranges;
- byte-for-byte edge/origin equality;
- concurrent requests and request coalescing;
- small and large admission;
- durable two-minute fill delay;
- encrypted Drive upload and readback;
- opaque filename/MIME metadata;
- no duplicate job/entry creation.

Expected final state:

```text
entry.status             = ready
entry.encryption_version = 1
job.status               = completed
Drive name               = tp-e1-<random>.bin
Drive MIME               = application/octet-stream
origin source            = GDRIVE
```

## 6. Verify direct L2 range

Extract `file_id` and `cache_key` from the signed URL path:

```text
https://l1-media.exa.qzz.io/media/<file_id>/<cache_key>?...
```

Then call the private origin:

```bash
curl -sS -D - -o /tmp/origin-range.bin \
  -H "X-TelePlay-Origin-Secret: $ORIGIN_SECRET" \
  -H 'Range: bytes=0-1048575' \
  "$API_BASE/api/stream/origin/<file_id>?cache_key=<cache_key>"
```

After L2 is ready, expected:

```text
HTTP/2 206
x-teleplay-origin-cache: GDRIVE
```

Compare `/tmp/origin-range.bin` with the Worker output for the same range.

## 7. Verify manual Drive deletion reconciliation

Delete or trash one cached `tp-e1-*.bin` object manually in Google Drive, then:

```bash
curl -sS -X POST \
  -H "X-TelePlay-Origin-Secret: $ORIGIN_SECRET" \
  "$API_BASE/api/stream/cache-reconcile" \
  | python -m json.tool
```

Expected:

```text
missing >= 1
stale READY pointer removed
entry queued for encrypted Telegram rebuild
READY count corrected
```

Cloudflare may still serve previously cached L1 chunks. L1 and Drive L2 are
separate layers.

## 8. Common failures

### Raw URL returns 200/206 instead of 307

Render does not have `PUBLIC_STREAM_EDGE_MODE=redirect`, Cloudflare cache is not
enabled, or the backend was not redeployed.

### Location still contains `workers.dev`

Render still has the old `CLOUDFLARE_WORKER_BASE_URL`. Set it to:

```env
CLOUDFLARE_WORKER_BASE_URL=https://l1-media.exa.qzz.io
```

Then redeploy and fetch a fresh URL.

### Worker returns 403

The edge signing secrets do not match, the URL expired, or the signed query was
modified.

### Worker returns 502

Inspect backend logs for `/api/stream/origin/...`, verify `RENDER_ORIGIN`, and
confirm Worker `ORIGIN_SECRET` equals Render `CLOUDFLARE_ORIGIN_SECRET`.

### Repeated request remains MISS

Use the exact same range from the same network/location. Cloudflare Cache API is
data-center local. Also inspect Worker logs for cache-put errors.

### Drive job stays queued

The fixed 120-second delay is expected. Continue polling before treating it as a
failure.
