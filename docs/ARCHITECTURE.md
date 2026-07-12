# TelePlay Architecture

TelePlay is a self-hosted, multi-user media server. Telegram stores the file bytes; TelePlay stores searchable metadata, folders, authentication sessions, watch progress, and recycle-bin state in a relational database.

## System Overview

```text
Telegram user ──uploads──► Bot/MTProto ──copies──► Private storage channel
                               │
                               └──metadata──────► PostgreSQL or SQLite

Web / Android TV / Android Mobile ──REST + Range requests──► FastAPI
                                                               │
                                               bounded chunk fetches from Telegram
                                                               │
                                                    streamed HTTP response
```

The backend, Telegram bot, streaming engine, scheduled cleanup, and REST API run in the same FastAPI process. The web app can be served separately through Nginx or bundled into the backend image. Android TV and Mobile share one Kotlin project but have separate launcher activities and Compose navigation.

## Technology Stack

| Layer | Implementation |
| --- | --- |
| Backend | Python 3.11+, FastAPI, Uvicorn |
| Telegram | PyroTGFork over MTProto, optional helper-bot pool |
| Database | SQLAlchemy 2 async, PostgreSQL or SQLite |
| Authentication | JWT access/refresh tokens plus database-backed sessions |
| Optional cache | Redis for rate limits and a small number of initial stream chunks |
| Web | React 18, TypeScript, Vite, React Query, Zustand |
| Android | Kotlin, Jetpack Compose, Media3/ExoPlayer, Hilt |
| Deployment | Docker Compose, Nginx, or monolithic PaaS image |

## Repository Structure

```text
teleplay/
├── backend/
│   ├── app/
│   │   ├── routers/
│   │   │   ├── auth.py        # login, refresh, credentials, sessions
│   │   │   ├── files.py       # file CRUD, progress, sharing
│   │   │   ├── folders.py     # hierarchical folder operations
│   │   │   ├── streaming.py   # authenticated/public Range responses
│   │   │   ├── trash.py       # recycle settings, restore and purge actions
│   │   │   └── tv.py          # TV-optimized browse/search responses
│   │   ├── auth.py            # JWT/cookie/session helpers
│   │   ├── bot.py             # Telegram commands and upload handlers
│   │   ├── database.py        # async engine and default soft-delete filter
│   │   ├── models.py          # SQLAlchemy models
│   │   ├── recycle_bin.py     # shared trash/restore/cleanup logic
│   │   ├── streaming.py       # bounded parallel Telegram chunk fetching
│   │   └── telegram.py        # Telegram client pool and channel helpers
│   └── migrations/            # ordered PostgreSQL migrations
├── web/src/
│   ├── components/            # browser, player, settings, Recycle Bin
│   ├── lib/api.ts             # credentialed API client and React Query hooks
│   ├── lib/store.ts           # local UI/player preferences
│   └── App.tsx                # auth callback, guards, routes
├── android/                   # TV and Mobile clients
├── docker-compose.yml
└── Dockerfile                 # combined backend/web PaaS image
```

## Backend Lifecycle

FastAPI's lifespan performs these operations in order:

1. Create missing database tables.
2. Apply pending numbered PostgreSQL migrations. Existing SQLite installations receive the recycle-bin columns during local initialization.
3. Start the main Telegram client and optional helper clients.
4. Start the recycle-bin cleanup loop.
5. Serve API, bot, and streaming requests.

On shutdown the cleanup task is cancelled, Telegram clients stop, and the optional Redis connection closes.

## Upload and Catalog Flow

1. A user sends video, audio, image, or document media to the bot.
2. The main Telegram client copies it to the configured private storage channel.
3. The backend creates a `File` row containing the owner, Telegram channel message ID, file identifiers, name, size, MIME type, media metadata, and optional folder.
4. Clients list and search the database. Telegram is contacted only when bytes or a thumbnail are requested.

All normal file and folder operations are scoped by the authenticated `user_id`.

Before a bot upload is forwarded, TelePlay compares Telegram's stable `file_unique_id` within that user's library, including Recycle Bin rows. A match pauses the upload and shows the existing name, ID, size, and location. The user can cancel without using more Telegram storage or explicitly keep another copy. The `(user_id, file_unique_id)` lookup is covered by migration `007_duplicate_detection_index.sql`.

## Authentication and Sessions

### Login methods

- **Username/password:** credentials are created through the bot and passwords are bcrypt-hashed. This creates a persistent session.
- **Six-digit code:** Web/TV generates a code and the user approves it through Telegram. Code sessions are temporary.
- **One-time web link:** `/web` creates a high-entropy, one-use code. The browser exchanges it at `/api/auth/link/exchange`; JWTs are never placed in the URL.

### Token transport

- Web receives access and refresh tokens in `HttpOnly` cookies. JavaScript keeps only a non-sensitive session hint.
- Android TV/Mobile use bearer tokens.
- Refresh tokens are rotated and hashed in `AuthSession` rows.
- Sessions can be listed, individually revoked, revoked on other devices, or revoked globally.
- Temporary sessions send heartbeats and close automatically after inactivity.

The web `ProtectedRoute` always validates the real cookie session, even if local storage was cleared. A `PublicOnlyRoute` prevents a user with a valid access or refresh session from reopening `/login`, `/login/password`, `/login/code`, or `/auth` and redirects them home.

### Browser request protection

Cookie-authenticated state-changing requests must include `X-TelePlay-CSRF: 1`. The backend also validates `Origin`/`Referer` against `WEB_BASE_URL` and configured development origins. Bearer-token Android clients are unaffected by the cookie CSRF check.

## Recycle Bin

Recycle Bin is enabled by default with a 30-day retention period. Each user can disable it or select 3, 7, 14, 30, 60, 90, 180, 365, or a custom value from 1–365 days. Changing the duration recalculates `purge_after` for existing deleted files/folders from their original `deleted_at` timestamp, so the displayed remaining days immediately follow the user's current policy.

### Soft delete

`File` and `Folder` have `deleted_at`, `purge_after`, and `trash_root_id` fields. A normal ORM SELECT automatically excludes rows with `deleted_at`, unless recycle-bin code explicitly requests `include_deleted=True`. Deleted content stays unavailable to normal file lists, TV browse/search, bot actions, and public links. Authenticated owner-only Recycle Bin routes expose read-only folder browsing, streaming, thumbnails, downloads, and resume-progress updates so preview/playback behaves like the normal library without exposing write operations such as rename, move, upload, or sharing.

Deleting a file:

- stores its deletion and purge timestamps;
- revokes its public hash;
- keeps its Telegram channel message until permanent purge.

Deleting a folder:

- marks the selected folder, active descendants, and active contained files;
- assigns the selected folder ID as `trash_root_id`;
- preserves the hierarchy as one restore unit.

If Recycle Bin is disabled, new deletes immediately remove the Telegram message and database rows. Items already in Recycle Bin are not affected by disabling the feature.

### Restore and permanent purge

- Files can be restored individually or in a bulk selection, including files selected from inside a deleted folder.
- Deleted folders are browsable in read-only mode; restoring a nested file recreates/reuses the required active folder path while the unselected items remain in Recycle Bin.
- A folder selection restores that selected folder subtree.
- If the original parent no longer exists, the restored item moves to root.
- If a folder name now conflicts, the restored root receives a safe `(restored)` suffix.
- Users can delete selected items forever or empty the entire bin.
- The background cleanup runs at startup and every six hours, purging expired Telegram messages and metadata.
- Web deletes show an eight-second Undo action. Undo calls the same bulk-restore endpoint as the Recycle Bin, so server state—not a browser-only rollback—remains authoritative.

## REST API

All paths below are under `/api`.

### Authentication

```text
GET    /auth/bot/info
POST   /auth/password/login
POST   /auth/password/change
GET    /auth/web-credential
GET    /auth/username/check
PATCH  /auth/username
POST   /auth/code/generate
POST   /auth/code/poll
POST   /auth/code/verify
POST   /auth/link/exchange
POST   /auth/refresh
POST   /auth/logout
POST   /auth/logout-all
GET    /auth/me
GET    /auth/sessions
DELETE /auth/sessions
DELETE /auth/sessions/{session_id}
POST   /auth/session/heartbeat
POST   /auth/session/close
```

Legacy aliases remain for older clients where they are declared in `routers/auth.py`.

### Files and folders

```text
GET    /files
GET    /files/recent
GET    /files/continue-watching
GET    /files/storage
GET    /files/analytics
GET    /files/{id}
PATCH  /files/{id}
DELETE /files/{id}                 # recycle or permanent based on user setting
POST   /files/batch-delete
POST   /files/batch-move
GET    /files/{id}/progress
POST   /files/{id}/progress
DELETE /files/progress
POST   /files/{id}/share
DELETE /files/{id}/share

GET    /folders
GET    /folders/tree
GET    /folders/{id}
POST   /folders
PATCH  /folders/{id}
DELETE /folders/{id}
POST   /folders/batch-delete
POST   /folders/batch-move
```

### Recycle Bin

```text
GET    /trash
GET    /trash/settings
PUT    /trash/settings
GET    /trash/folders/{id}/children # read-only nested browsing
GET    /trash/files/{id}/stream     # owner-only preview/playback/download
GET    /trash/files/{id}/thumbnail  # Telegram thumb or generated WebP fallback
POST   /trash/files/{id}/restore
POST   /trash/folders/{id}/restore
POST   /trash/bulk-restore
DELETE /trash/files/{id}
DELETE /trash/folders/{id}
POST   /trash/bulk-delete
DELETE /trash                       # empty bin
```

### Streaming and TV

```text
POST /stream/prefetch
GET  /stream/{file_id}
GET  /stream/{file_id}/thumbnail
GET  /stream/s/{public_hash}

GET  /tv/browse
GET  /tv/continue
GET  /tv/recent
GET  /tv/search
GET  /tv/folder/{folder_id}
```

## Streaming Engine

Clients send normal HTTP `Range` headers. The streaming router validates the requested range, returns `206 Partial Content`, and exposes `Content-Range`, `Accept-Ranges`, and content metadata.

The engine converts the byte range to Telegram-sized chunk indexes and fetches a small bounded window. Chunks may be fetched concurrently across the available client pool but are yielded in order. Only the current window is held in memory; the full video/audio file is never downloaded to server storage.

For image cards, TelePlay first uses a small thumbnail already supplied by Telegram. When Telegram provides none, the original image is downloaded once into the operating-system temporary directory, resized to a bounded WebP (320 px by default), and immediately removed. Only the generated WebP is retained in the thumbnail cache. Browser lazy loading plus a backend concurrency limit prevents a large image folder from downloading every original simultaneously.

When Redis is configured, TelePlay may cache only the first configured chunks for a short TTL and may warm the previous/next items, including owner-authenticated Recycle Bin playback queues. Redis is optional and never stores complete videos.

### Production hybrid media cache

The optional production cache is independent of the small Redis prefetch cache:

```text
Viewer -> Cloudflare L1 plaintext chunk
              | MISS
              v
          TelePlay backend
              +-> encrypted Google Drive L2 range
              +-> Telegram fallback
```

Cloudflare uses a canonical `cache_key + chunk_index` identity, so fresh signed
URLs and public redirects reuse the same L1 object. Google Drive stores full
media objects as independent authenticated AES-256-GCM blocks with random names.
The backend decrypts only the blocks needed for the requested byte range.

For the bundled production profile, readable `/api/stream/s/...` links are
validated by Render and return a temporary 307 to
`https://l1-media.exa.qzz.io/media/...`. The Worker custom domain hides the
account-specific `workers.dev` hostname. The API hostname remains a normal
Render origin and does not need a Worker Route in redirect mode.

Durable SQL rows track cache entries, fill/migration jobs, reader leases,
resumable upload offsets, encryption metadata, access statistics and Drive
reconciliation. Media bytes are never stored in the SQL database.

## Database Models

| Model | Purpose |
| --- | --- |
| `User` | Telegram identity and authentication version |
| `WebCredential` | Per-user username and bcrypt password hash |
| `AuthSession` | Hashed refresh token, session type, expiry, heartbeat and revocation |
| `UserSettings` | Recycle enable flag and future-deletion retention period |
| `Folder` | Hierarchy plus recycle timestamps/group |
| `File` | Telegram identifiers, media metadata, sharing and recycle state |
| `WatchProgress` | Per-user/file resume position |
| `LoginCode` | Short-lived code or one-time link exchange state |
| `MediaCacheEntry` | Canonical L2 identity, Drive pointer, encryption state and access counters |
| `MediaCacheJob` | Durable fill/migration lease, retry and resumable upload state |
| `MediaCacheReadLease` | Prevents cleanup/migration from deleting an object during active reads |
| `MediaCacheDailyUsage` | Drive/Telegram byte counters and sampled edge activity |

PostgreSQL migrations run lexically and are recorded in `schema_migrations`. Migration `006_recycle_bin.sql` adds the recycle settings table, soft-delete columns, constraints, and indexes; migration `007_duplicate_detection_index.sql` adds the per-user duplicate lookup index.

## Web Application

The web app uses React Query for server state and Zustand for local navigation/player preferences. Axios sends cookies with every API request, adds the CSRF header to unsafe methods, rotates expired sessions through `/auth/refresh`, and redirects to password login only after refresh fails.

The theme-matched Recycle Bin is a sidebar section with:

- current item count;
- search and responsive cards;
- lazy-loaded Telegram/generated image thumbnails;
- normal-player media highlighting, queue navigation, resume progress, and download controls;
- expiry date and remaining-day label;
- nested read-only folder browsing plus single or bulk restore;
- selected permanent deletion with confirmation;
- Empty Bin with a separate irreversible-action confirmation.

Recycle enable/retention settings live on the backend so they apply to web and bot deletes across devices. Visual/player preferences remain in browser local storage.

The theme-matched Storage Analytics sidebar section reports active and Recycle Bin bytes separately, active file/folder counts, storage by media type, 14-day upload activity, and the eight largest active files. It is computed per authenticated user and does not download or probe Telegram media.

## Android Clients

`MainActivity` is the TV/Leanback entry point and `MobileMainActivity` is the phone launcher. Both use Retrofit repositories and encrypted token storage. Mobile additionally supports downloads, background audio, notifications, and picture-in-picture. Existing clients continue to use the same file/folder API; deleted objects disappear automatically because filtering happens in the backend.

## Security Boundaries

- Every private query checks the authenticated owner.
- Deleted rows are excluded centrally from normal/public routes; only the authenticated owner can stream or thumbnail them through dedicated Recycle Bin routes, and they cannot be publicly shared.
- Public shares use high-entropy, revocable hashes; they do not expire automatically.
- Passwords use bcrypt; refresh tokens are stored as hashes.
- Cookies are host-only by default, `HttpOnly`, and `Secure` for HTTPS/SameSite=None deployments.
- CORS is credential-aware and derived from the configured public web origin.
- CSP, frame, MIME-sniffing, referrer, and legacy XSS headers are applied globally.
- `AUTH_USERS` can restrict Telegram bot access to an allowlist.

## Deployment Notes

Docker Compose starts PostgreSQL, backend, and web; Redis is opt-in through a profile. Production must set a random `JWT_SECRET` of at least 32 characters. Every helper bot must be an administrator of the storage channel. Database and Telegram session volumes must be persistent.

See [DEPLOYMENT.md](DEPLOYMENT.md), [SETUP.md](SETUP.md), and [RELEASING.md](RELEASING.md) for operational instructions.
