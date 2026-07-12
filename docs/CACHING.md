# TelePlay production cache

TelePlay can use two cache layers while keeping Telegram as the source of truth:

```text
Viewer
  |
  v
Cloudflare Worker L1 - plaintext fixed-size media chunks
  | HIT: Render, Drive and Telegram are bypassed
  |
  | MISS
  v
TelePlay backend
  +-- encrypted Google Drive L2 ready
  |      fetch encrypted ranges -> authenticate/decrypt 1 MiB blocks -> stream
  |
  +-- L2 missing/unavailable
         stream requested Telegram range
         queue delayed durable encrypted Drive fill when admitted
```

## Cache modes

```env
CACHE_MODE=off
CACHE_MODE=gdrive
CACHE_MODE=cloudflare
CACHE_MODE=hybrid
```

| Mode | Cloudflare L1 | Encrypted Drive L2 | Telegram fallback |
|---|---:|---:|---:|
| `off` | No | No | Yes |
| `gdrive` | No | Yes | Yes |
| `cloudflare` | Yes | No | Yes |
| `hybrid` | Yes | Yes | Yes |

`hybrid` is the recommended production mode.

## Storage and memory behavior

### Cloudflare L1 HIT

```text
Cloudflare -> viewer
```

- Render is not contacted for media bytes.
- Google Drive is not read.
- Telegram is not read.
- The edge object is plaintext so normal browser and Android players work.

### Cloudflare MISS + Drive L2 HIT

```text
Drive ciphertext -> Render 1 MiB crypto blocks -> Worker -> viewer
```

- The full file is not downloaded to Render.
- The full file is not assembled in RAM.
- Render holds only bounded encrypted/plaintext buffers.
- Worker streams to the viewer while writing the aligned L1 object.

### Drive miss

```text
Telegram requested range -> Worker/viewer
                      \-> delayed encrypted Drive fill
```

The viewer is not required to wait for a full Drive upload. New Drive fill jobs
start after a fixed two-minute low-priority delay.

## Google Drive encryption

Drive stores independent authenticated AES-256-GCM blocks. Objects use random
names such as:

```text
tp-e1-<random>.bin
```

and MIME type:

```text
application/octet-stream
```

The original filename is not written to Drive. Downloading the Drive object
directly produces unreadable ciphertext.

Generate the required 32-byte master key once:

```bash
python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
```

Store it only as:

```env
MEDIA_CACHE_MASTER_KEY_BASE64=<generated value>
```

Do not rotate or lose it casually. Telegram originals remain safe, but existing
encrypted Drive objects cannot be decrypted after a key change and must be
rebuilt.

## Minimal production variables

```env
CACHE_MODE=hybrid
MEDIA_CACHE_KEY_VERSION=2

GOOGLE_DRIVE_CLIENT_ID=...
GOOGLE_DRIVE_CLIENT_SECRET=...
GOOGLE_DRIVE_REFRESH_TOKEN=...
GOOGLE_DRIVE_CACHE_FOLDER_ID=...
MEDIA_CACHE_MASTER_KEY_BASE64=...

CLOUDFLARE_WORKER_BASE_URL=https://l1-media.exa.qzz.io
CLOUDFLARE_EDGE_SIGNING_SECRET=...
CLOUDFLARE_ORIGIN_SECRET=...
CLOUDFLARE_TOUCH_SECRET=...

PUBLIC_STREAM_EDGE_MODE=redirect
```

Only useful policy overrides are shown in the example environment files:

```env
GDRIVE_CACHE_BUDGET_GB=4000
GDRIVE_MAX_CACHE_FILE_GB=5
```

Other cache settings have production defaults in `backend/app/config.py`.

`CLOUDFLARE_WORKER_BASE_URL` must be an absolute origin such as
`https://l1-media.example.com`. The backend rejects values without a scheme or
values containing a path, query, credentials, or fragment.

## Cloudflare production configuration

The bundled production config uses a clean Worker Custom Domain:

```toml
name = "teleplay-media-edge"
main = "src/index.js"
compatibility_date = "2026-07-01"
workers_dev = false

[vars]
RENDER_ORIGIN = "https://api.telxstream.exa.qzz.io"
ALLOWED_ORIGINS = "https://telxstream.exa.qzz.io"
EDGE_CHUNK_SIZE_BYTES = "4194304"
EDGE_TOUCH_INTERVAL_SECONDS = "21600"

[[routes]]
pattern = "l1-media.exa.qzz.io"
custom_domain = true

[secrets]
required = ["EDGE_SIGNING_SECRET", "ORIGIN_SECRET", "TOUCH_SECRET"]
```

Cloudflare creates/manages the DNS record and edge certificate for the Custom
Domain. Delete any existing DNS record with the same hostname before the first
deployment.

The secret mapping must be exact:

```text
Worker EDGE_SIGNING_SECRET = Render CLOUDFLARE_EDGE_SIGNING_SECRET
Worker ORIGIN_SECRET       = Render CLOUDFLARE_ORIGIN_SECRET
Worker TOUCH_SECRET        = Render CLOUDFLARE_TOUCH_SECRET
```

## Automatic Worker deployment

The included GitHub Actions workflow:

1. Installs Worker dependencies without a package lock.
2. Validates JavaScript syntax.
3. Runs Worker tests.
4. Validates the Wrangler production bundle.
5. Authenticates using `CLOUDFLARE_API_TOKEN`.
6. Inspects existing Worker secret-binding names.
7. Bootstraps only missing bindings on first deployment.
8. Deploys the code and Custom Domain.
9. Deletes temporary secret files even after failure.

Always add:

```text
CLOUDFLARE_API_TOKEN
```

For a brand-new Worker, also add:

```text
EDGE_SIGNING_SECRET
ORIGIN_SECRET
TOUCH_SECRET
```

After first deployment, the three bootstrap secrets may be removed from GitHub.
The deployed Worker bindings remain and normal code deployments need only the
API token.

The workflow does not use a `CLOUDFLARE_ACCOUNT_ID` GitHub secret. Scope the API
token to one Cloudflare account and the target zone. `wrangler whoami` runs
before deployment and stops the job if account inference is not possible.

## Public readable stream modes

The stable readable link remains:

```text
https://api.example.com/api/stream/s/<public-hash>/<filename>
```

Set:

```env
PUBLIC_STREAM_EDGE_MODE=off
```

or `redirect` / `proxy`.

### `off`

The backend serves the raw link directly from encrypted Drive L2 or Telegram.
Cloudflare L1 is not used for that public link. Authenticated signed stream URLs
can still use L1 when `CACHE_MODE` includes Cloudflare.

### `redirect`

The backend validates the public hash and returns a temporary 307 to a signed
Worker URL.

Recommended clean-domain profile:

```env
CLOUDFLARE_WORKER_BASE_URL=https://l1-media.exa.qzz.io
PUBLIC_STREAM_EDGE_MODE=redirect
```

Flow:

```text
api.../api/stream/s/...
  -> 307 Location: https://l1-media.../media/...signed...
  -> Worker HIT or MISS handling
```

The redirect is unconditional when edge caching is enabled. The Worker checks
L1 in the viewer's Cloudflare location. A backend-side “redirect only if cached”
check would be unreliable because Cloudflare Cache API is data-center local.

The API hostname does not need to be orange-clouded in redirect mode.

### `proxy`

A Worker Route intercepts the public API URL, so the browser-visible URL remains
unchanged. The API hostname must be in a Cloudflare-managed zone and proxied.
Proxy mode remains available for forks that prefer it, but the bundled
production profile uses custom-domain redirect.

See `PUBLIC_STREAM_EDGE.md` for complete setup and trade-offs.

## Duplicate cache prevention

The canonical media identity is derived from cache version, Telegram
`file_unique_id`, and file size. Database uniqueness prevents duplicate Drive
entries and duplicate fill jobs for the same identity.

Cloudflare's internal L1 key uses:

```text
cache representation version + media cache key + chunk index
```

Temporary URL token, expiry, filename, public hash, and database file row ID do
not create a second chunk object when the canonical media identity is the same.

Content-identical Telegram files with different Telegram `file_unique_id`
values are treated as different cache identities; TelePlay does not hash every
full source file before streaming.

## Existing plaintext Drive migration

Migration `backend/migrations/010_encrypted_drive_cache.sql` adds encryption and
legacy-pointer fields.

Each old object is migrated safely:

```text
old plaintext Drive object
  -> stream one block at a time
  -> encrypt and resumably upload a new opaque object
  -> verify encrypted size
  -> atomically switch the database pointer
  -> delete the old plaintext object after active reader leases expire
```

A restart resumes from durable database state. A missing legacy object is
converted to a Telegram rebuild job.

## Manual Drive deletions and reconciliation

`ready.count` is a database catalog count, not a live Drive-folder scan. When a
user manually deletes a Drive object, reconciliation verifies the exact stored
Drive ID, removes stale READY state, and queues an encrypted Telegram rebuild.

Run reconciliation immediately:

```bash
export ORIGIN_SECRET='the exact Render CLOUDFLARE_ORIGIN_SECRET'

curl -sS -X POST \
  -H "X-TelePlay-Origin-Secret: $ORIGIN_SECRET" \
  https://api.telxstream.exa.qzz.io/api/stream/cache-reconcile \
  | python -m json.tool
```

Automatic reconciliation also runs through the normal cache cleanup lifecycle.
Cloudflare L1 may continue serving an already-cached plaintext chunk until its
edge object expires or is evicted; that is independent of Drive L2 state.

## Status

```bash
curl -sS \
  -H "X-TelePlay-Origin-Secret: $ORIGIN_SECRET" \
  https://api.telxstream.exa.qzz.io/api/stream/cache-status \
  | python -m json.tool
```

Important fields:

- `entries.ready.count`: reconciled READY L2 entries.
- `entries.ready.bytes`: plaintext media size represented by READY entries.
- `encryption.encrypted`: encrypted L2 entries.
- `encryption.legacy_plaintext`: old readable entries still awaiting migration.
- `encryption.pending_migration`: queued/running legacy conversions.
- `jobs`: durable fill/migration job states.
- `today.drive_bytes`: plaintext bytes delivered after Drive decryption.
- `today.telegram_bytes`: bytes served from Telegram.
- `today.edge_hits`: sampled Worker L1 hits/touches.

## Header interpretation

```text
X-TelePlay-Edge-Cache: HIT
```

Cloudflare served the canonical chunk; no media bytes came from Render.

```text
X-TelePlay-Edge-Cache: MISS
X-TelePlay-Origin-Cache: GDRIVE
```

Worker missed L1; Render decrypted the requested Drive blocks.

```text
X-TelePlay-Edge-Cache: MISS
X-TelePlay-Origin-Cache: TELEGRAM
```

Both cache layers missed or L2 was unavailable; Telegram served the range.

```text
X-TelePlay-Public-Mode: REDIRECT
```

The readable public URL issued a temporary redirect to the signed Worker URL.

## Production tests

See `CACHE_TESTING.md` for small-file, large-file, redirect, L1 HIT/MISS,
encrypted L2, concurrent request, reconciliation, and byte-integrity tests.
