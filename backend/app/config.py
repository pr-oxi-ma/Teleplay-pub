"""
Configuration settings loaded from environment variables.

Every setting below has a safe local/testing fallback. For production,
prefer environment variables or a .env file over editing these defaults.
"""
from functools import lru_cache
from ipaddress import ip_address
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram
    telegram_api_id: int = Field(0, alias="TELEGRAM_API_ID")
    telegram_api_hash: str = Field("", alias="TELEGRAM_API_HASH")
    telegram_bot_token: str = Field("", alias="TELEGRAM_BOT_TOKEN")

    # Use string fields to avoid JSON parsing issues with comma-separated env vars
    telegram_helper_bot_tokens_str: str = Field("", alias="TELEGRAM_HELPER_BOT_TOKENS")
    auth_users_str: str = Field("", alias="AUTH_USERS")

    @property
    def auth_users(self) -> list[int]:
        v = self.auth_users_str
        if not v:
            return []
        try:
            return [int(u.strip()) for u in v.split(",") if u.strip()]
        except ValueError as exc:
            raise ValueError("AUTH_USERS must be comma-separated integer Telegram IDs") from exc

    @property
    def telegram_helper_bot_tokens(self) -> list[str]:
        v = self.telegram_helper_bot_tokens_str
        if not v:
            return []
        return [t.strip() for t in v.split(",") if t.strip()]

    @property
    def all_bot_tokens(self) -> list[str]:
        return [self.telegram_bot_token] + self.telegram_helper_bot_tokens

    telegram_storage_channel_id: int = Field(0, alias="TELEGRAM_STORAGE_CHANNEL_ID")

    # Database
    database_url: str = Field("sqlite:///./data/teleplay.db", alias="DATABASE_URL")

    # JWT / Sessions
    jwt_secret: str = Field("change-me-in-production-minimum-32-characters", alias="JWT_SECRET")
    allow_insecure_dev_jwt_secret: bool = Field(False, alias="ALLOW_INSECURE_DEV_JWT_SECRET")
    jwt_expiry_minutes: int = Field(10080, alias="JWT_EXPIRY_MINUTES")  # 7 days
    jwt_refresh_expiry_days: int = Field(90, alias="JWT_REFRESH_EXPIRY_DAYS")

    # Cookie session settings for web clients.
    # CORS origin and Secure are derived from WEB_BASE_URL.
    # Cookie Domain is intentionally omitted by default (host-only), so cookies
    # are scoped to the backend host that sets them. This is required for
    # deployments such as Vercel frontend + Render backend.
    session_access_cookie_name: str = Field("tp_access", alias="SESSION_ACCESS_COOKIE_NAME")
    session_refresh_cookie_name: str = Field("tp_refresh", alias="SESSION_REFRESH_COOKIE_NAME")
    session_cookie_samesite: str = Field("lax", alias="SESSION_COOKIE_SAMESITE")
    session_csrf_enabled: bool = Field(True, alias="SESSION_CSRF_ENABLED")
    auth_session_ip_hash_secret: str = Field("", alias="AUTH_SESSION_IP_HASH_SECRET")

    # DB-backed auth-session cleanup. This prevents abandoned incognito/browser
    # sessions from piling up forever when users close a browser without logging
    # out or manually revoking the session in Settings.
    auth_session_cleanup_enabled: bool = Field(True, alias="AUTH_SESSION_CLEANUP_ENABLED")
    auth_session_max_active_per_user: int = Field(7, alias="AUTH_SESSION_MAX_ACTIVE_PER_USER")
    auth_session_inactive_days: int = Field(14, alias="AUTH_SESSION_INACTIVE_DAYS")
    auth_session_revoked_retention_days: int = Field(3, alias="AUTH_SESSION_REVOKED_RETENTION_DAYS")

    trusted_proxy_ips_str: str = Field("127.0.0.1,::1", alias="TRUSTED_PROXY_IPS")

    # Rate limits reuse REDIS_URL when Redis is configured.
    # Leave REDIS_URL empty to use in-memory limits.

    # Username/password login rules
    web_username_min_length: int = Field(3, alias="WEB_USERNAME_MIN_LENGTH")
    web_username_max_length: int = Field(16, alias="WEB_USERNAME_MAX_LENGTH")


    # Server
    server_host: str = Field("0.0.0.0", alias="SERVER_HOST")
    server_port: int = Field(5001, alias="SERVER_PORT")

    # Static files are served from the bundled frontend build.

    # Concurrency
    telegram_client_concurrency: int = Field(3, alias="TELEGRAM_CLIENT_CONCURRENCY")

    # On-demand card thumbnails. Missing Telegram thumbnails are generated once
    # from the original image; only the small WebP result is retained.
    thumbnail_cache_dir: str = Field("", alias="THUMBNAIL_CACHE_DIR")
    thumbnail_max_dimension: int = Field(320, alias="THUMBNAIL_MAX_DIMENSION", ge=96, le=1024)
    thumbnail_webp_quality: int = Field(68, alias="THUMBNAIL_WEBP_QUALITY", ge=35, le=90)
    thumbnail_generation_concurrency: int = Field(2, alias="THUMBNAIL_GENERATION_CONCURRENCY", ge=1, le=8)

    # Optional Redis cache
    # Leave REDIS_URL empty to run without Redis. When provided, TelePlay uses it
    # only for tiny hot stream chunks / prefetch cache, never full videos.
    redis_url: str = Field("", alias="REDIS_URL")
    redis_cache_stream_chunks: bool = Field(True, alias="REDIS_CACHE_STREAM_CHUNKS")
    redis_stream_chunk_cache_chunks: int = Field(2, alias="REDIS_STREAM_CHUNK_CACHE_CHUNKS")
    redis_stream_chunk_ttl_seconds: int = Field(300, alias="REDIS_STREAM_CHUNK_TTL_SECONDS")
    redis_prefetch_enabled: bool = Field(True, alias="REDIS_PREFETCH_ENABLED")
    redis_prefetch_chunk_count: int = Field(2, alias="REDIS_PREFETCH_CHUNK_COUNT")

    # Managed media cache modes:
    #   off         -> Telegram only
    #   gdrive      -> Google Drive L2 cache + Telegram fallback
    #   cloudflare  -> Cloudflare L1 cache + Telegram fallback
    #   hybrid      -> Cloudflare L1 -> Google Drive L2 -> Telegram
    cache_mode: str = Field("off", alias="CACHE_MODE")
    media_cache_key_version: int = Field(2, alias="MEDIA_CACHE_KEY_VERSION", ge=1, le=99)

    # Google Drive OAuth credentials and bounded cache policy. Drive is a cache,
    # never the source of truth; Telegram remains the final fallback.
    google_drive_client_id: str = Field("", alias="GOOGLE_DRIVE_CLIENT_ID")
    google_drive_client_secret: str = Field("", alias="GOOGLE_DRIVE_CLIENT_SECRET")
    google_drive_refresh_token: str = Field("", alias="GOOGLE_DRIVE_REFRESH_TOKEN")
    google_drive_cache_folder_id: str = Field("", alias="GOOGLE_DRIVE_CACHE_FOLDER_ID")
    media_cache_master_key_base64: str = Field("", alias="MEDIA_CACHE_MASTER_KEY_BASE64")
    google_drive_cache_budget_gb: int = Field(4000, alias="GDRIVE_CACHE_BUDGET_GB", ge=1)
    google_drive_max_cache_file_gb: int = Field(5, alias="GDRIVE_MAX_CACHE_FILE_GB", ge=0)
    google_drive_daily_egress_soft_limit_gb: int = Field(800, alias="GDRIVE_DAILY_EGRESS_SOFT_LIMIT_GB", ge=0)
    google_drive_high_watermark: float = Field(0.80, alias="GDRIVE_HIGH_WATERMARK")
    google_drive_low_watermark: float = Field(0.65, alias="GDRIVE_LOW_WATERMARK")
    google_drive_never_reused_ttl_days: int = Field(3, alias="GDRIVE_NEVER_REUSED_TTL_DAYS", ge=1)
    google_drive_cold_ttl_days: int = Field(30, alias="GDRIVE_COLD_TTL_DAYS", ge=1)
    google_drive_eviction_grace_hours: int = Field(6, alias="GDRIVE_EVICTION_GRACE_HOURS", ge=0)
    google_drive_cleanup_interval_seconds: int = Field(21600, alias="GDRIVE_CLEANUP_INTERVAL_SECONDS", ge=900)
    google_drive_cleanup_start_delay_seconds: int = Field(30, alias="GDRIVE_CLEANUP_START_DELAY_SECONDS", ge=0)
    google_drive_cleanup_batch_size: int = Field(25, alias="GDRIVE_CLEANUP_BATCH_SIZE", ge=1, le=500)
    google_drive_upload_chunk_mb: int = Field(8, alias="GDRIVE_UPLOAD_CHUNK_MB", ge=1, le=64)
    google_drive_max_concurrent_fills: int = Field(1, alias="GDRIVE_MAX_CONCURRENT_FILLS", ge=1, le=4)
    google_drive_fill_telegram_concurrency: int = Field(1, alias="GDRIVE_FILL_TELEGRAM_CONCURRENCY", ge=1, le=4)
    google_drive_job_poll_seconds: int = Field(5, alias="GDRIVE_JOB_POLL_SECONDS", ge=1, le=60)
    google_drive_job_lease_seconds: int = Field(180, alias="GDRIVE_JOB_LEASE_SECONDS", ge=60, le=1800)
    google_drive_job_max_attempts: int = Field(12, alias="GDRIVE_JOB_MAX_ATTEMPTS", ge=1, le=50)
    google_drive_access_touch_seconds: int = Field(3600, alias="GDRIVE_ACCESS_TOUCH_SECONDS", ge=60)
    google_drive_read_lease_seconds: int = Field(900, alias="GDRIVE_READ_LEASE_SECONDS", ge=120, le=7200)
    google_drive_read_lease_renew_seconds: int = Field(120, alias="GDRIVE_READ_LEASE_RENEW_SECONDS", ge=30, le=1800)
    google_drive_admission_small_file_mb: int = Field(25, alias="GDRIVE_ADMISSION_SMALL_FILE_MB", ge=1)
    google_drive_admission_bytes_mb: int = Field(128, alias="GDRIVE_ADMISSION_BYTES_MB", ge=1)
    google_drive_admission_ratio: float = Field(0.20, alias="GDRIVE_ADMISSION_RATIO")
    google_drive_circuit_failure_threshold: int = Field(3, alias="GDRIVE_CIRCUIT_FAILURE_THRESHOLD", ge=1)
    google_drive_circuit_open_seconds: int = Field(120, alias="GDRIVE_CIRCUIT_OPEN_SECONDS", ge=30)

    # Cloudflare Worker edge cache. These are separate secrets so URLs, private
    # origin access, and sampled popularity touches can be rotated independently.
    cloudflare_worker_base_url: str = Field("", alias="CLOUDFLARE_WORKER_BASE_URL")
    cloudflare_edge_signing_secret: str = Field("", alias="CLOUDFLARE_EDGE_SIGNING_SECRET")
    cloudflare_origin_secret: str = Field("", alias="CLOUDFLARE_ORIGIN_SECRET")
    cloudflare_touch_secret: str = Field("", alias="CLOUDFLARE_TOUCH_SECRET")
    cloudflare_edge_url_ttl_seconds: int = Field(7200, alias="CLOUDFLARE_EDGE_URL_TTL_SECONDS", ge=300)
    cloudflare_edge_touch_max_skew_seconds: int = Field(300, alias="CLOUDFLARE_EDGE_TOUCH_MAX_SKEW_SECONDS", ge=30)

    # Public readable /api/stream/s/... behavior:
    #   off      -> current direct backend stream (Drive L2/Telegram only)
    #   redirect -> temporary 307 to the signed Worker media URL
    #   proxy    -> same public URL is served by a Cloudflare Worker Route
    public_stream_edge_mode: str = Field("off", alias="PUBLIC_STREAM_EDGE_MODE")

    # Web
    # Used as the public frontend URL and the production CORS origin.
    web_base_url: str = Field("http://localhost:5000", alias="WEB_BASE_URL")

    # Login code
    login_code_length: int = Field(6, alias="LOGIN_CODE_LENGTH")
    login_code_expiry_minutes: int = Field(5, alias="LOGIN_CODE_EXPIRY_MINUTES")
    web_login_code_length: int = Field(32, alias="WEB_LOGIN_CODE_LENGTH")

    # Temporary session settings for one-time code/link logins.
    temp_session_heartbeat_seconds: int = Field(60, alias="TEMP_SESSION_HEARTBEAT_SECONDS")
    temp_session_timeout_seconds: int = Field(300, alias="TEMP_SESSION_TIMEOUT_SECONDS")

    @property
    def trusted_proxy_ips(self) -> list[str]:
        """Direct proxy IPs/CIDRs allowed to provide X-Forwarded-For."""
        return [value.strip() for value in self.trusted_proxy_ips_str.split(",") if value.strip()]

    @property
    def rate_limit_storage_uri(self) -> str | None:
        value = self.redis_url.strip()
        return value or None

    @property
    def normalized_cache_mode(self) -> str:
        value = self.cache_mode.strip().lower()
        return value if value in {"off", "gdrive", "cloudflare", "hybrid"} else "off"

    @property
    def normalized_public_stream_edge_mode(self) -> str:
        value = self.public_stream_edge_mode.strip().lower()
        return value if value in {"off", "redirect", "proxy"} else "off"

    @property
    def google_drive_cache_enabled(self) -> bool:
        mode_enabled = self.normalized_cache_mode in {"gdrive", "hybrid"}
        return bool(
            mode_enabled
            and self.google_drive_client_id.strip()
            and self.google_drive_client_secret.strip()
            and self.google_drive_refresh_token.strip()
        )

    @property
    def cloudflare_cache_enabled(self) -> bool:
        mode_enabled = self.normalized_cache_mode in {"cloudflare", "hybrid"}
        return bool(
            mode_enabled
            and self.cloudflare_worker_base_url.strip()
            and self.cloudflare_edge_signing_secret.strip()
            and self.cloudflare_origin_secret.strip()
            and self.cloudflare_touch_secret.strip()
        )

    @property
    def cloudflare_worker_origin(self) -> str:
        """Normalized absolute origin used to build signed edge URLs."""
        return self._normalize_origin(self.cloudflare_worker_base_url)

    @property
    def static_dir(self) -> str:
        """Bundled frontend build directory; hardcode here if you test another path."""
        return "app/static"

    @staticmethod
    def _normalize_origin(value: str) -> str:
        """Return scheme://host[:port] without trailing slash/path."""
        value = value.strip()
        if not value:
            return ""
        parsed = urlparse(value)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        return value.rstrip("/")

    @staticmethod
    def _host_from_url(value: str) -> str:
        parsed = urlparse(value.strip())
        return (parsed.hostname or "").strip(".").lower()

    @staticmethod
    def _is_ip_or_localhost(host: str) -> bool:
        if not host or host == "localhost" or host.endswith(".localhost"):
            return True
        try:
            ip_address(host)
            return True
        except ValueError:
            return False

    @property
    def web_origin(self) -> str:
        return self._normalize_origin(self.web_base_url)

    @property
    def allowed_cors_origins(self) -> list[str]:
        """Return WEB_BASE_URL plus local/dev origins.

        Cookie auth cannot use a wildcard CORS origin, so production CORS is
        derived from WEB_BASE_URL instead of a second env variable.
        """
        origins = [
            self.web_origin,
            "http://localhost:5000",
            "http://localhost:5173",
            "http://127.0.0.1:5000",
            "http://127.0.0.1:5173",
        ]

        seen: set[str] = set()
        cleaned: list[str] = []
        for origin in origins:
            if origin and origin != "*" and origin not in seen:
                seen.add(origin)
                cleaned.append(origin)
        return cleaned

    @property
    def cookie_domain(self) -> str | None:
        """Omit Cookie Domain by default.

        The cookie is set by the backend response, so a host-only cookie is
        scoped to the backend host automatically, e.g.:
        - teleplay-fmx9.onrender.com for Render
        - api.example.com for an API subdomain

        Do not derive this from WEB_BASE_URL. If WEB_BASE_URL is a Vercel app,
        deriving would create invalid/unsafe values like .vercel.app, and the
        Render backend cannot set cookies for a Vercel-owned domain anyway.
        Host-only cookies work for same-origin, subdomain, and cross-site API
        requests as long as CORS/withCredentials/SameSite are configured.
        """
        return None

    @property
    def cookie_samesite(self) -> str:
        value = self.session_cookie_samesite.strip().lower()
        return value if value in {"lax", "strict", "none"} else "lax"

    @property
    def cookie_secure(self) -> bool:
        # Automatically secure cookies when the public web app is HTTPS.
        # Browsers also require Secure for SameSite=None cookies.
        return self.web_origin.lower().startswith("https://") or self.cookie_samesite == "none"

    @model_validator(mode="after")
    def validate_secure_secrets(self):
        insecure_defaults = {
            "change-me-in-production-minimum-32-characters",
            "change-me-in-production",
            "your-super-secret-key-change-in-production",
            "your-super-secret-key-at-least-32-characters",
        }
        if self.jwt_secret in insecure_defaults and not self.allow_insecure_dev_jwt_secret:
            raise ValueError(
                "JWT_SECRET is using an insecure default. Set a strong random JWT_SECRET "
                "or set ALLOW_INSECURE_DEV_JWT_SECRET=true only for local throwaway development."
            )
        if len(self.jwt_secret) < 32 and not self.allow_insecure_dev_jwt_secret:
            raise ValueError("JWT_SECRET must be at least 32 characters long.")
        if not 0 < self.google_drive_low_watermark < self.google_drive_high_watermark < 1:
            raise ValueError("GDRIVE watermarks must satisfy 0 < LOW < HIGH < 1.")
        if not 0 < self.google_drive_admission_ratio <= 1:
            raise ValueError("GDRIVE_ADMISSION_RATIO must be greater than 0 and at most 1.")
        if self.google_drive_read_lease_renew_seconds >= self.google_drive_read_lease_seconds:
            raise ValueError(
                "GDRIVE_READ_LEASE_RENEW_SECONDS must be lower than GDRIVE_READ_LEASE_SECONDS."
            )
        if self.normalized_cache_mode in {"gdrive", "hybrid"}:
            values = [
                self.google_drive_client_id.strip(),
                self.google_drive_client_secret.strip(),
                self.google_drive_refresh_token.strip(),
            ]
            if not all(values):
                raise ValueError(
                    "CACHE_MODE requires Google Drive, but GOOGLE_DRIVE_CLIENT_ID, "
                    "GOOGLE_DRIVE_CLIENT_SECRET and GOOGLE_DRIVE_REFRESH_TOKEN are not all set."
                )
            from .media_cache_crypto import decode_master_key
            decode_master_key(self.media_cache_master_key_base64)
        if self.public_stream_edge_mode.strip().lower() not in {"off", "redirect", "proxy"}:
            raise ValueError("PUBLIC_STREAM_EDGE_MODE must be off, redirect or proxy.")
        if self.normalized_cache_mode in {"cloudflare", "hybrid"}:
            values = [
                self.cloudflare_worker_base_url.strip(),
                self.cloudflare_edge_signing_secret.strip(),
                self.cloudflare_origin_secret.strip(),
                self.cloudflare_touch_secret.strip(),
            ]
            if not all(values):
                raise ValueError(
                    "CACHE_MODE requires Cloudflare, but CLOUDFLARE_WORKER_BASE_URL and all "
                    "three CLOUDFLARE_* secrets are not set."
                )
            parsed_worker_url = urlparse(values[0])
            if (
                parsed_worker_url.scheme not in {"http", "https"}
                or not parsed_worker_url.netloc
                or parsed_worker_url.username
                or parsed_worker_url.password
                or parsed_worker_url.query
                or parsed_worker_url.fragment
                or parsed_worker_url.path not in {"", "/"}
            ):
                raise ValueError(
                    "CLOUDFLARE_WORKER_BASE_URL must be an absolute origin such as "
                    "https://l1-media.example.com, without a path, query or credentials."
                )
            if any(len(secret) < 32 for secret in values[1:]):
                raise ValueError("Cloudflare cache secrets must each be at least 32 characters.")
        if self.normalized_public_stream_edge_mode in {"redirect", "proxy"} and not self.cloudflare_cache_enabled:
            raise ValueError(
                "PUBLIC_STREAM_EDGE_MODE=redirect/proxy requires CACHE_MODE=cloudflare or hybrid "
                "and the existing Cloudflare Worker URL/secrets."
            )
        return self

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
