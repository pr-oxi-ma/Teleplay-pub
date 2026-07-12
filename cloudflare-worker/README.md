# TelePlay Cloudflare L1 media cache

This Worker stores fixed aligned plaintext media chunks in Cloudflare Cache API.
Google Drive L2 is encrypted; the TelePlay backend decrypts only the requested
blocks on an L1 miss.

```text
L1 HIT  -> Worker -> viewer
L1 MISS -> Worker -> Render -> encrypted Drive L2 or Telegram
                           -> Worker cache + viewer simultaneously
```

The miss path uses `ReadableStream.tee()` and does not wait for an entire 4 MiB
object before playback begins.

## Bundled production target

`wrangler.production.toml` is configured for:

```text
Worker name:   teleplay-media-edge
Custom domain: https://l1-media.exa.qzz.io
Backend origin:https://api.telxstream.exa.qzz.io
Frontend CORS: https://telxstream.exa.qzz.io
workers.dev:   disabled
```

The custom domain configuration is:

```toml
workers_dev = false

[[routes]]
pattern = "l1-media.exa.qzz.io"
custom_domain = true
```

Cloudflare creates the DNS record and edge certificate. Remove any conflicting
DNS record for `l1-media.exa.qzz.io` before the first deployment.

## Backend values for the production redirect profile

```env
CACHE_MODE=hybrid
CLOUDFLARE_WORKER_BASE_URL=https://l1-media.exa.qzz.io
PUBLIC_STREAM_EDGE_MODE=redirect
```

A public readable API link stays stable, but Render returns a temporary signed
redirect to the custom Worker domain. The Worker then decides locally whether
the chunk is an L1 HIT or MISS.

## Open-source configuration

Start from `wrangler.toml.example`.

### Without a domain

Keep:

```toml
workers_dev = true
```

Deploy and set Render to the generated `https://<worker>.<subdomain>.workers.dev`
origin. `PUBLIC_STREAM_EDGE_MODE=redirect` works, but the account subdomain is
visible after the redirect.

### With a clean custom domain

Set:

```toml
workers_dev = false

[[routes]]
pattern = "l1-media.example.com"
custom_domain = true
```

Set Render to:

```env
CLOUDFLARE_WORKER_BASE_URL=https://l1-media.example.com
PUBLIC_STREAM_EDGE_MODE=redirect
```

The API hostname does not need to be Cloudflare proxied in redirect mode.

### Same-URL proxy mode

Proxy mode is optional and separate. Route the readable API path to the Worker,
orange-cloud the API hostname, and set `PUBLIC_STREAM_EDGE_MODE=proxy`. See
`docs/PUBLIC_STREAM_EDGE.md` for the complete comparison.

## Automatic GitHub deployment

The workflow is `.github/workflows/cloudflare-worker-deploy.yml`.

Always create:

```text
CLOUDFLARE_API_TOKEN
```

For the first deployment, also create:

```text
EDGE_SIGNING_SECRET
ORIGIN_SECRET
TOUCH_SECRET
```

They must equal the corresponding Render values:

```text
EDGE_SIGNING_SECRET = CLOUDFLARE_EDGE_SIGNING_SECRET
ORIGIN_SECRET       = CLOUDFLARE_ORIGIN_SECRET
TOUCH_SECRET        = CLOUDFLARE_TOUCH_SECRET
```

The workflow detects deployed secret-binding names. Existing Workers with all
bindings deploy using only the API token. A brand-new Worker or missing binding
is bootstrapped from the matching GitHub secret.

The workflow intentionally does not use a `CLOUDFLARE_ACCOUNT_ID` repository
secret. Scope the API token to one Cloudflare account and the required zone. The
job runs `wrangler whoami` before deployment and stops early if account
inference is not possible.

## Manual deployment

```bash
npm install --no-audit --no-fund --no-package-lock
npm run check
npm test
npx wrangler deploy --config wrangler.production.toml
```

For a brand-new manual deployment, first create the secret bindings:

```bash
npx wrangler secret put EDGE_SIGNING_SECRET --config wrangler.production.toml
npx wrangler secret put ORIGIN_SECRET --config wrangler.production.toml
npx wrangler secret put TOUCH_SECRET --config wrangler.production.toml
```

Never commit secret values to Wrangler files.

## Verification

Worker root returning `404` is expected because only media paths are handled.

Test a signed media URL with a byte range:

```bash
curl -sS -D - -o /dev/null \
  -H 'Origin: https://telxstream.exa.qzz.io' \
  -H 'Range: bytes=0-1048575' \
  "$SIGNED_URL"
```

Expected first request: `HTTP 206` and `X-TelePlay-Edge-Cache: MISS`.
Expected repeated request: `HTTP 206` and `X-TelePlay-Edge-Cache: HIT`.

See `docs/CACHING.md`, `docs/PUBLIC_STREAM_EDGE.md`, and
`docs/CACHE_TESTING.md` for the complete cache and deployment checks.
