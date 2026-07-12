# **This repo is a forked of** [https://github.com/subinps/TelePlay](https://github.com/subinps/TelePlay)

**Credit** : [https://github.com/subinps](https://github.com/subinps)

# 📺 TelePlay

**Your personal, self-hosted media server — powered by Telegram.**

![TelePlay Banner](files/app_banner.png)

Stream and manage your Telegram files on any device — TV, Mobile, or Browser — **without downloading the entire file**. TelePlay uses Telegram as unlimited cloud storage and streams content on-demand at high speed using its **multi-client parallel download** technology. Upload via a Telegram Bot, organize through a Web App, and watch anywhere.

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11+-green.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-teal.svg)
![React](https://img.shields.io/badge/React-18-61dafb.svg)
![Kotlin](https://img.shields.io/badge/Kotlin-Compose_TV-7f52ff.svg)

---

## ✨ Features

### 🤖 Telegram Bot — [Full Command List](docs/SETUP.md#part-2-using-the-telegram-bot)

- Upload any file type (video, audio, documents, photos)
- Organize files into folders with inline buttons
- Rename, move, and delete files via chat commands
- Search your library with `/myfiles`
- Get an auto-login web link with `/web`
- Detect duplicate Telegram media before forwarding, with an explicit keep-another-copy choice

### 🌐 Web App — [Login Methods](docs/SETUP.md#31-web-interface)

- Full file browser with folder navigation
- Multi-select, batch delete, rename, and move operations
- Context menu (right-click) on files
- Inline video/audio player with seeking
- Secure username/password, six-digit code, and one-time Telegram link login
- HttpOnly-cookie sessions with refresh rotation and device/session management
- Recycle Bin with folder-safe restore, bulk restore, permanent delete, and per-user retention
- Eight-second Undo action after recoverable deletes
- Storage Analytics with type breakdown, upload activity, largest files, and Recycle Bin usage
- Responsive — works on desktop and mobile

### 📺 Android TV & Mobile App — [Installation Guide](docs/SETUP.md#32-android-tv--mobile)

- Designed for TV with D-Pad / remote control navigation
- **Continue Watching** and **Recently Added** rows on the home screen
- Full-screen ExoPlayer playback with transport controls
- Download files for offline playback (Mobile)
- Picture-in-Picture mode (Mobile)
- Watch progress automatically synced with the server

### ⚡ Platform — [Architecture Overview](docs/ARCHITECTURE.md)

- **Zero local storage** — all files live on Telegram's unlimited cloud
- **Multi-user** — each Telegram user gets an isolated library
- **High-speed streaming** — optional [multi-bot parallel downloads](docs/ARCHITECTURE.md#multi-client-mode-parallel-downloads)
- **Restricted access** — [whitelist allowed users](docs/SETUP.md#-advanced-features) with `AUTH_USERS`
- **Public sharing** — generate high-entropy links that remain valid until revoked
- **One-command deploy** — [Docker Compose, Railway, Render, or CapRover](docs/DEPLOYMENT.md)
- **Hybrid encrypted cache** — plaintext Cloudflare L1 plus unreadable AES-256-GCM Google Drive L2, durable migration, reconciliation, and Telegram fallback
- **Clean public edge links** — readable API share URLs can 307 to a signed `l1-media` Worker custom domain without exposing the account-specific `workers.dev` hostname

---

## 🏗️ How It Works

```
  You                Telegram Cloud              Your Server              Your Devices
  ───                ──────────────              ───────────              ────────────
   │                                                  │
   │  1. Send file to Bot ──────────────────────────► │
   │                         2. Bot forwards to  ───► │ (Private Channel)
   │                            Storage Channel       │
   │                                                  │ 3. Saves metadata
   │                                                  │    to Database
   │                                                  │
   │  4. Open Web / TV App ◄──────────────────────────│
   │                                                  │
   │  5. Press Play ──────────────────────────────► │
   │                         6. Fetches chunks   ◄──  │ (from Telegram)
   │  7. Streams to you ◄────────────────────────── │
   │                                                  │
```

Original files are **never stored permanently on your server** — TelePlay streams them from Telegram on demand. If Telegram has no card thumbnail for an image, TelePlay downloads the original to an OS temporary file once, immediately deletes it after resizing, and retains only a small generated WebP thumbnail.

### Recycle Bin

Recycle Bin is enabled by default. Deleting a file or folder keeps its Telegram message and hierarchy recoverable for 30 days instead of removing it immediately.

- Choose 3, 7, 14, 30, 60, 90, 180, 365, or a custom 1–365 day retention period.
- Retention changes recalculate the expiry date of existing Recycle Bin items as well as future deletions.
- Browse deleted folders in read-only mode, preview/play media, and restore individual children from inside a deleted folder.
- Restore one item, select and restore many items, delete selected items forever, or empty the bin.
- Folder restore preserves descendants. Missing paths are safely recreated for selective child restores.
- Disabling Recycle Bin makes future deletes permanent; it does not silently purge items already in the bin.

Expired items are purged from Telegram storage and the database by the backend cleanup task.

---

## 📸 Screenshots

### 🌐 Web Interface

<p align="center">
  <img src="files/web_app_home_screen.png" width="32%" />
  <img src="files/web_app_player.png" width="32%" />
  <img src="files/web_app_login_screen.png" width="32%" />
</p>

### 📺 Android TV

<p align="center">
  <img src="files/tv_home_screen.png" width="32%" />
  <img src="files/tv_player_screen.png" width="32%" />
  <img src="files/tv_login_screen.png" width="32%" />
</p>

### 📱 Mobile App

<p align="center">
  <img src="files/mobile_home_screen.png" width="32%" />
  <img src="files/mobile_player_screen.png" width="32%" />
  <img src="files/mobile_downloads_screen.png" width="32%" />
</p>

---

## � Quick Start

### Prerequisites — [Detailed Steps](docs/DEPLOYMENT.md#-step-1-get-telegram-credentials)

| Requirement            | How to get it                                          |
| :--------------------- | :----------------------------------------------------- |
| **Telegram Bot Token** | Create via [@BotFather](https://t.me/BotFather)        |
| **API ID & Hash**      | Register at [my.telegram.org](https://my.telegram.org) |
| **Storage Channel**    | Create a private channel, add your bot as admin        |
| **Docker**             | [Install Docker](https://docs.docker.com/get-docker/)  |

### 1. Clone & Configure

```bash
git clone https://github.com/YOUR_USERNAME/teleplay.git
cd teleplay
cp .env.example .env
```

Edit `.env` with your credentials:

```env
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_STORAGE_CHANNEL_ID=-100xxxxxxxxxx
JWT_SECRET=your-super-secret-key-at-least-32-characters

# Use PostgreSQL (recommended) or SQLite (no setup needed):
DATABASE_URL=sqlite:///./data/teleplay.db
# DATABASE_URL=postgresql://postgres:password@db:5432/teleplay
```

### 2. Deploy

```bash
docker compose up -d
```

That's it! Your services are now running:

| Service         | URL                   |
| :-------------- | :-------------------- |
| **Web App**     | http://localhost      |
| **Backend API** | http://localhost:8000 |

### 3. Start Using

1. Open Telegram and send a video file to your bot.
2. Send `/web` to get a link to your Web App.
3. Stream your files! 🎬

> **For detailed setup, usage, and login instructions**, see the **[Setup & Usage Guide](docs/SETUP.md)**.
>
> **For VPS, Railway, Render, and CapRover deployments**, see the **[Deployment Guide](docs/DEPLOYMENT.md)**.

---

## 📱 Android TV & Mobile App

**Download the APK** from the [Releases](../../releases) page:

| APK         | Best For                               |
| :---------- | :------------------------------------- |
| `arm64-v8a` | Modern TV boxes, phones, NVIDIA Shield |
| `universal` | Any device (if unsure, use this one)   |

**Setup:**

1. Install the APK on your device.
2. Enter your Server URL (e.g., `http://192.168.1.100`).
3. A 6-digit code will appear — send `/login CODE` to your bot.
4. Done! Browse and stream your library.

> **For APK signing and release automation**, see the **[Releasing Guide](docs/RELEASING.md)**.

---

## ⚙️ Environment Variables

| Variable                      | Required | Description                                                                                          |
| :---------------------------- | :------: | :--------------------------------------------------------------------------------------------------- |
| `TELEGRAM_API_ID`             |    ✅    | From [my.telegram.org](https://my.telegram.org)                                                      |
| `TELEGRAM_API_HASH`           |    ✅    | From [my.telegram.org](https://my.telegram.org)                                                      |
| `TELEGRAM_BOT_TOKEN`          |    ✅    | From [@BotFather](https://t.me/BotFather)                                                            |
| `TELEGRAM_STORAGE_CHANNEL_ID` |    ✅    | Private channel ID (starts with `-100`)                                                              |
| `JWT_SECRET`                  |    ✅    | Secret key for JWT signing (min 32 chars)                                                            |
| `DATABASE_URL`                |    ✅    | Database connection URL (see below)                                                                  |
| `WEB_BASE_URL`                |    ❌    | Public URL of the web app                                                                            |
| `TELEGRAM_HELPER_BOT_TOKENS`  |    ❌    | Extra bot tokens for [parallel downloads](docs/ARCHITECTURE.md#multi-client-mode-parallel-downloads) |
| `AUTH_USERS`                  |    ❌    | Comma-separated Telegram IDs for restricted access                                                   |
| `REDIS_URL`                   |    ❌    | Optional external Redis URL for tiny first-chunk stream cache / previous-next prefetch                |
| `REDIS_CACHE_STREAM_CHUNKS`   |    ❌    | Optional, default `true`; cache only the first stream chunks when Redis is configured                  |
| `REDIS_STREAM_CHUNK_CACHE_CHUNKS` | ❌ | Optional, default `2`; max first chunks cached per media, never full videos                            |
| `REDIS_STREAM_CHUNK_TTL_SECONDS` | ❌ | Optional, default `300`; short Redis chunk cache TTL                                                  |
| `REDIS_PREFETCH_ENABLED`      |    ❌    | Optional, default `true`; warm previous/next media only when Redis is configured                       |
| `THUMBNAIL_CACHE_DIR`         |    ❌    | Directory for generated small WebP card thumbnails; originals are never retained there                |
| `THUMBNAIL_MAX_DIMENSION`     |    ❌    | Optional, default `320`; maximum generated thumbnail width/height                                      |
| `THUMBNAIL_WEBP_QUALITY`      |    ❌    | Optional, default `68`; generated WebP quality                                                          |
| `THUMBNAIL_GENERATION_CONCURRENCY` | ❌ | Optional, default `2`; limits simultaneous original-image downloads for missing thumbnails             |
| `CACHE_MODE`                  |    ❌    | `off`, `gdrive`, `cloudflare`, or `hybrid`; production recommendation is `hybrid`                      |
| `MEDIA_CACHE_MASTER_KEY_BASE64` | ❌ | Required when Drive L2 is enabled; one stable 32-byte Base64 key                                        |
| `CLOUDFLARE_WORKER_BASE_URL` | ❌ | Absolute Worker origin, for example `https://l1-media.example.com`                                      |
| `PUBLIC_STREAM_EDGE_MODE` | ❌ | `off`, `redirect`, or `proxy`; bundled clean-domain production uses `redirect`                              |

> **💡 DATABASE_URL Options:**
>
> - **PostgreSQL (recommended):** `postgresql://postgres:password@localhost:5432/teleplay`
> - **SQLite (no setup needed):** `sqlite:///./data/teleplay.db`
>
> Use SQLite if you don't want to set up PostgreSQL — it works out of the box for small deployments.
>
> **Optional Redis:** leave `REDIS_URL` empty to skip Redis. On small hosts like Render free, use an external Redis service; do not run Redis inside the same 512 MB web service. TelePlay caches only tiny first stream chunks, never full videos. For local Docker Redis, you may set `REDIS_PASSWORD` and use `REDIS_URL=redis://:<password>@redis:6379/0`.

---

## 🛠️ Tech Stack

| Layer        | Technology                                       |
| :----------- | :----------------------------------------------- |
| **Backend**  | Python 3.11+, FastAPI, Uvicorn                   |
| **Telegram** | PyroTGFork (MTProto)                             |
| **Database** | PostgreSQL (prod) / SQLite (dev), SQLAlchemy 2.0 |
| **Auth**     | JWT (Access + Refresh Tokens)                    |
| **Web**      | React 18, TypeScript, Vite                       |
| **Android**  | Kotlin, Jetpack Compose for TV, ExoPlayer        |
| **Deploy**   | Docker, Docker Compose, Nginx                    |

---

## 📁 Project Structure — [Full Breakdown](docs/ARCHITECTURE.md#-project-structure)

```
teleplay/
├── backend/                  # Python backend (FastAPI + Bot)
│   ├── app/
│   │   ├── routers/          # API endpoints (auth, files, folders, streaming, trash, tv)
│   │   ├── bot.py            # Telegram bot command handlers
│   │   ├── recycle_bin.py    # Soft delete, restore, permanent purge, cleanup
│   │   ├── streaming.py      # Multi-client parallel streaming engine
│   │   ├── models.py         # SQLAlchemy ORM models
│   │   └── main.py           # FastAPI app entry point
│   ├── Dockerfile
│   └── requirements.txt
├── web/                      # React web interface
│   ├── src/
│   │   ├── components/       # UI components
│   │   ├── lib/api.ts        # API client & hooks
│   │   └── App.tsx           # Main app with routing
│   └── Dockerfile
├── android/                  # Android TV & Mobile app
│   └── app/src/main/java/    # Kotlin (Compose + ExoPlayer)
├── docs/                     # Documentation
│   ├── ARCHITECTURE.md       # Technical deep-dive
│   ├── DEPLOYMENT.md         # Deployment guide
│   ├── SETUP.md              # Setup & usage guide
│   ├── CACHING.md            # Hybrid L1/L2 cache internals
│   ├── PUBLIC_STREAM_EDGE.md # Public link off/redirect/proxy setup
│   ├── CACHE_TESTING.md      # Production cache verification
│   └── RELEASING.md          # APK release process
├── docker-compose.yml
└── .env.example
```

---

## 🔧 Development

### Backend

```bash
cd backend
python -m venv venv
venv\Scripts\activate        # Linux/Mac: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # Edit with your credentials
python3 run.py
```

### Web App

```bash
cd web
npm install
npm run dev
```

### Android

Open the `android/` folder in Android Studio and build.

---

## 🔒 Security — [Details](docs/ARCHITECTURE.md#-security)

- **JWT Authentication** — Access/refresh tokens with [rotation and database-backed sessions](docs/ARCHITECTURE.md#authentication-and-sessions)
- **Browser Session Protection** — HttpOnly cookies, CSRF header/origin checks, and login-page guards
- **Database-backed Sessions** — Persistent/temporary sessions, heartbeat, device list, and revocation
- **User Authorization** — Optional [`AUTH_USERS`](docs/SETUP.md#-advanced-features) whitelist
- **Rate Limiting** — SlowAPI middleware on all endpoints
- **CORS Protection** — Restricted to configured origins
- **Input Validation** — Pydantic schemas prevent injection attacks
- **Deleted-content Isolation** — Trashed files stay hidden from normal browse/search/public routes and are exposed only through authenticated Recycle Bin read-only routes
- **Security Headers** — CSP, framing, MIME, referrer, and related headers on all responses

---

## 📚 Documentation

| Guide                                    | Description                                                           |
| :--------------------------------------- | :-------------------------------------------------------------------- |
| **[Setup & Usage](docs/SETUP.md)**       | How the app works, bot commands, login methods, and troubleshooting   |
| **[Deployment](docs/DEPLOYMENT.md)**     | Docker, VPS, Railway, Render, and CapRover deployment                 |
| **[Architecture](docs/ARCHITECTURE.md)** | Technical deep-dive: streaming engine, API endpoints, database models |
| **[Releasing](docs/RELEASING.md)**       | APK build automation and signing via GitHub Actions                   |
| **[Production Cache](docs/CACHING.md)**  | Cloudflare L1, encrypted Drive L2, migration, reconciliation          |
| **[Public Stream Edge](docs/PUBLIC_STREAM_EDGE.md)** | Off, redirect, proxy, and clean Worker custom-domain deployment |
| **[Cache Testing](docs/CACHE_TESTING.md)** | Small/large L1 and encrypted L2 production verification             |

---

## 🤝 Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- [PyroTGFork](https://github.com/TelegramPlayGround/pyrogram) — Telegram MTProto library
- [FastAPI](https://fastapi.tiangolo.com/) — Modern Python web framework
- [React](https://react.dev/) — Frontend library
- [Jetpack Compose for TV](https://developer.android.com/training/tv/compose) — Android TV UI toolkit
- [ExoPlayer](https://github.com/google/ExoPlayer) — Android media player


## Hybrid media caching

Production Google Drive + Cloudflare caching, encrypted Drive objects, manual-deletion reconciliation, legacy migration, durable jobs, fallback, and eviction are documented in [`docs/CACHING.md`](docs/CACHING.md).

The bundled clean-domain profile uses:

```env
CLOUDFLARE_WORKER_BASE_URL=https://l1-media.exa.qzz.io
PUBLIC_STREAM_EDGE_MODE=redirect
```

Readable `api.../api/stream/s/...` links return a temporary signed redirect to the clean Worker custom domain. `workers_dev` is disabled in production, so the account-specific Cloudflare hostname is not exposed. See [`docs/PUBLIC_STREAM_EDGE.md`](docs/PUBLIC_STREAM_EDGE.md).
