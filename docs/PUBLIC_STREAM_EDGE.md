# Public stream edge modes

TelePlay exposes readable public links such as:

```text
https://api.example.com/api/stream/s/<public-hash>/<filename>
```

The link itself remains stable and revocable. `PUBLIC_STREAM_EDGE_MODE` decides
how the media bytes are delivered.

## Modes

| Mode | Public link response | Cloudflare L1 | Own domain required | Visible media host |
|---|---|---:|---:|---|
| `off` | Backend streams directly | No | No | API hostname |
| `redirect` | Backend returns HTTP 307 to a signed Worker URL | Yes | No | Worker host or custom Worker domain |
| `proxy` | Worker Route serves the original public URL | Yes | Yes | API hostname |

`redirect` always redirects when Cloudflare cache is configured. The backend
cannot reliably decide whether a chunk is already cached in the viewer's
Cloudflare data center. The Worker makes the correct local decision:

```text
L1 HIT  -> serve the plaintext edge chunk immediately
L1 MISS -> fetch from encrypted Drive L2 or Telegram, stream it, then populate L1
```

## Recommended production setup: custom-domain redirect

This is the bundled production profile:

```text
Backend API:  https://api.telxstream.exa.qzz.io
Frontend:     https://telxstream.exa.qzz.io
Worker:       https://l1-media.exa.qzz.io
Public mode:  redirect
```

The original readable URL still starts on the API hostname:

```text
https://api.telxstream.exa.qzz.io/api/stream/s/<hash>/<filename>
```

The backend validates the public hash and responds with a temporary redirect:

```http
HTTP/2 307
Location: https://l1-media.exa.qzz.io/media/<file-id>/<cache-key>?...
Cache-Control: no-store
X-TelePlay-Public-Mode: REDIRECT
```

The browser or media player then requests the signed URL from the clean Worker
custom domain. The account-specific `*.workers.dev` hostname is not exposed.

### Render variables

```env
CACHE_MODE=hybrid
CLOUDFLARE_WORKER_BASE_URL=https://l1-media.exa.qzz.io
PUBLIC_STREAM_EDGE_MODE=redirect

CLOUDFLARE_EDGE_SIGNING_SECRET=<same value as Worker EDGE_SIGNING_SECRET>
CLOUDFLARE_ORIGIN_SECRET=<same value as Worker ORIGIN_SECRET>
CLOUDFLARE_TOUCH_SECRET=<same value as Worker TOUCH_SECRET>
```

`CLOUDFLARE_WORKER_BASE_URL` must include `https://` and must not contain a
path, query string, username, password, or fragment.

### Worker production configuration

The bundled `cloudflare-worker/wrangler.production.toml` contains:

```toml
workers_dev = false

[[routes]]
pattern = "l1-media.exa.qzz.io"
custom_domain = true
```

Cloudflare creates and manages the DNS record and edge certificate for a Worker
Custom Domain. Before first deployment, delete any existing A, AAAA, or CNAME
record named `l1-media.exa.qzz.io`; Cloudflare cannot attach a Custom Domain to
a hostname with a conflicting record.

The API hostname does **not** need to be orange-clouded for redirect mode. The
public request reaches Render normally, receives a 307, and only the redirected
media request uses the Worker custom domain.

### Open-source fork setup

A fork owner should either:

1. Edit `cloudflare-worker/wrangler.production.toml` and replace
   `l1-media.exa.qzz.io` with a hostname in their own Cloudflare-managed zone; or
2. Copy `wrangler.toml.example`, leave `workers_dev = true`, deploy to
   `*.workers.dev`, and set `CLOUDFLARE_WORKER_BASE_URL` to that generated URL.

A domain-free `workers.dev` setup works, but the Cloudflare account subdomain is
visible after redirect.

## First deployment and automatic secret bootstrap

The GitHub workflow deploys the Worker on pushes to `vercel` that change the
Worker or its workflow.

Always add this repository secret:

```text
CLOUDFLARE_API_TOKEN
```

The bundled workflow intentionally does not use a
`CLOUDFLARE_ACCOUNT_ID` repository secret. Scope the API token to one Cloudflare
account and the target zone. The job runs `wrangler whoami` before deployment
and stops early if account inference is not possible.

For a brand-new Worker, also add:

```text
EDGE_SIGNING_SECRET
ORIGIN_SECRET
TOUCH_SECRET
```

The values must match Render exactly:

```text
GitHub EDGE_SIGNING_SECRET = Render CLOUDFLARE_EDGE_SIGNING_SECRET
GitHub ORIGIN_SECRET       = Render CLOUDFLARE_ORIGIN_SECRET
GitHub TOUCH_SECRET        = Render CLOUDFLARE_TOUCH_SECRET
```

The workflow calls `wrangler secret list` before deployment:

- Existing Worker with all three bindings: deploys code using only the API token.
- Brand-new Worker or missing binding: provisions only the missing secret
  bindings from GitHub Actions secrets while deploying the Worker.

The temporary secrets file is deleted in an `always()` cleanup step. Existing
Worker secrets omitted from a normal deployment remain preserved.

## Deployment order

1. Remove any conflicting `l1-media` DNS record from the Cloudflare zone.
2. Create or update the Cloudflare API token and save it as
   `CLOUDFLARE_API_TOKEN` in GitHub Actions secrets.
3. For the first Worker deployment, add the three Worker secret values to
   GitHub Actions secrets.
4. Commit the workflow to the default `main` branch so it is visible in the
   Actions UI.
5. Keep the same workflow in the `vercel` branch, where Worker changes trigger
   automatic deployment.
6. Push `cloudflare-worker/**` or the workflow to `vercel`.
7. Wait for the Worker deployment and custom-domain certificate to become
   active.
8. Set Render to the custom Worker origin and `redirect` mode.
9. Redeploy Render.
10. Test the redirect and L1 HIT/MISS behavior.

## Verification

Set a readable public URL:

```bash
RAW_URL='https://api.telxstream.exa.qzz.io/api/stream/s/<hash>/<filename>'
```

Check only the redirect:

```bash
curl -sS -D - -o /dev/null \
  -H 'Range: bytes=0-1048575' \
  "$RAW_URL"
```

Expected:

```text
HTTP/2 307
location: https://l1-media.exa.qzz.io/media/...
x-teleplay-public-mode: REDIRECT
cache-control: no-store
```

Follow the redirect and test the edge cache:

```bash
curl -sS -L -D - -o /dev/null \
  -H 'Origin: https://telxstream.exa.qzz.io' \
  -H 'Range: bytes=0-1048575' \
  "$RAW_URL"
```

The first uncached request normally ends with:

```text
HTTP/2 206
x-teleplay-edge-cache: MISS
```

Repeat the same request. It should normally end with:

```text
HTTP/2 206
x-teleplay-edge-cache: HIT
```

A first-request `HIT` is also valid when the same canonical chunk was already
warmed by an authenticated signed stream URL.

## Security properties

- Public links remain revocable through their existing database hash.
- Redirect targets are short-lived and HMAC signed.
- `Cache-Control: no-store` prevents the 307 response from becoming a durable
  redirect.
- L1 cache keys ignore the temporary token and expiry, so repeated signed URLs
  reuse the same canonical media chunk.
- Google Drive L2 remains encrypted; Cloudflare L1 intentionally stores
  plaintext playable chunks.
- Disabling `workers_dev` removes the account-specific production endpoint from
  normal use. The custom domain becomes the production Worker address.

## Troubleshooting

### Custom-domain deployment fails because the hostname already exists

Delete the existing DNS record for the custom hostname and deploy again. A
Worker Custom Domain owns that hostname and Cloudflare creates the required DNS
record itself.

### Redirect still points to `workers.dev`

Render is still using the old value. Set:

```env
CLOUDFLARE_WORKER_BASE_URL=https://l1-media.exa.qzz.io
```

Then redeploy the backend. Existing signed URLs remain valid until their expiry;
fetch a fresh file response or reopen the player to obtain a new URL.

### Raw URL returns `200` or `206` instead of `307`

Check:

```env
PUBLIC_STREAM_EDGE_MODE=redirect
CACHE_MODE=cloudflare
```

or:

```env
CACHE_MODE=hybrid
```

Also verify all three Render Cloudflare secrets are configured.

### Redirect is correct but the Worker returns `403`

The signing secret differs between Render and Cloudflare, the URL expired, or
the signed URL was modified. Confirm the exact secret mapping and request a
fresh raw URL.

### Worker returns `502`

Check `RENDER_ORIGIN`, `ORIGIN_SECRET`, the backend health, and the private
`/api/stream/origin/{file_id}` request logs.

### Every repeated request stays `MISS`

Use the same byte range and test from the same network/location. Cloudflare
Cache API is data-center local and may not immediately share the object with a
different Cloudflare location.
