"""
Database models for TelePlay streaming app.
"""
from datetime import datetime
from typing import Optional, List
from sqlalchemy import BigInteger, String, Integer, Boolean, ForeignKey, DateTime, Text, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base
from .time_utils import utcnow


class User(Base):
    """Telegram user who uses the bot."""
    __tablename__ = "users"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255))
    first_name: Mapped[Optional[str]] = mapped_column(String(255))
    last_name: Mapped[Optional[str]] = mapped_column(String(255))
    auth_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_active: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    
    # Relationships
    folders: Mapped[List["Folder"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    files: Mapped[List["File"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    watch_progress: Mapped[List["WatchProgress"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    web_credentials: Mapped[List["WebCredential"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    auth_sessions: Mapped[List["AuthSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    preferences: Mapped[Optional["UserSettings"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
    )


class UserSettings(Base):
    """Server-side preferences that must apply across web, TV and bot clients."""
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    recycle_bin_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    recycle_bin_retention_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="preferences")


class WebCredential(Base):
    """Bot-created username/password credential for web login."""
    __tablename__ = "web_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True, unique=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="web_credentials")

    __table_args__ = (
        Index("idx_web_credential_user", user_id),
    )


class AuthSession(Base):
    """DB-backed browser/app session with refresh-token rotation."""
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    user_agent: Mapped[Optional[str]] = mapped_column(String(500))
    ip_hash: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    session_type: Mapped[str] = mapped_column(String(20), default="persistent", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    user: Mapped["User"] = relationship(back_populates="auth_sessions")

    __table_args__ = (
        Index("idx_auth_sessions_user_active", user_id, revoked_at),
        Index("idx_auth_sessions_expires", expires_at),
        Index("idx_auth_sessions_type_seen", session_type, last_seen_at),
        Index("idx_auth_sessions_user_seen", user_id, last_seen_at),
        Index("idx_auth_sessions_revoked", revoked_at),
    )


class Folder(Base):
    """User-created folder for organizing files."""
    __tablename__ = "folders"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    purge_after: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    # The top-level deleted folder owns a trash group. Descendant folders/files
    # keep this ID so the complete hierarchy can be restored as one unit.
    trash_root_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    
    # Relationships
    user: Mapped["User"] = relationship(back_populates="folders")
    parent: Mapped[Optional["Folder"]] = relationship(back_populates="children", remote_side=[id])
    children: Mapped[List["Folder"]] = relationship(back_populates="parent", cascade="all, delete-orphan")
    files: Mapped[List["File"]] = relationship(back_populates="folder")
    
    # Indexes
    __table_args__ = (
        Index("idx_folder_user_parent", user_id, parent_id),
    )


class File(Base):
    """File stored in Telegram, metadata in database."""
    __tablename__ = "files"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    folder_id: Mapped[Optional[int]] = mapped_column(ForeignKey("folders.id", ondelete="SET NULL"))
    
    # Telegram-specific identifiers
    file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    file_unique_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    channel_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    
    # File metadata
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(String(100))
    file_type: Mapped[str] = mapped_column(String(50), nullable=False)  # video, audio, document, image
    
    # Media-specific metadata
    duration: Mapped[Optional[int]] = mapped_column(Integer)  # seconds
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)
    thumbnail_file_id: Mapped[Optional[str]] = mapped_column(String(255))
    
    # Sharing
    public_hash: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    purge_after: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    trash_root_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    
    # Relationships
    user: Mapped["User"] = relationship(back_populates="files")
    folder: Mapped[Optional["Folder"]] = relationship(back_populates="files")
    watch_progress: Mapped[List["WatchProgress"]] = relationship(back_populates="file", cascade="all, delete-orphan")
    
    # Indexes
    __table_args__ = (
        Index("idx_file_user_folder", user_id, folder_id),
        Index("idx_file_user_unique", user_id, file_unique_id),
        Index("idx_file_type", file_type),
    )


class WatchProgress(Base):
    """Track video watch progress for continue watching feature."""
    __tablename__ = "watch_progress"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0)  # seconds
    duration: Mapped[Optional[int]] = mapped_column(Integer)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    
    # Relationships
    user: Mapped["User"] = relationship(back_populates="watch_progress")
    file: Mapped["File"] = relationship(back_populates="watch_progress")
    
    # Unique constraint
    __table_args__ = (
        Index("idx_watch_user_file", user_id, file_id, unique=True),
    )


class LoginCode(Base):
    """Temporary login code for TV/Web auth."""
    __tablename__ = "login_codes"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MediaCacheEntry(Base):
    """Durable catalog row for one immutable Drive cache object."""
    __tablename__ = "media_cache_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    cache_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    file_unique_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(String(100))
    file_type: Mapped[str] = mapped_column(String(50), nullable=False, default="document")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    drive_file_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True, index=True)
    legacy_drive_file_id: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    encryption_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    encryption_nonce_prefix: Mapped[Optional[str]] = mapped_column(String(32))
    encrypted_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="observed", index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    active_readers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    read_lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)

    edge_hit_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    drive_hit_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    telegram_hit_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    telegram_bytes_served: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    truncated_read_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    upload_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    upload_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    last_access_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    last_edge_access_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    last_drive_access_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    last_telegram_access_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    delete_after: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    eviction_reason: Mapped[Optional[str]] = mapped_column(String(100))
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)

    __table_args__ = (
        Index("idx_media_cache_status_access", status, pinned, last_access_at),
        Index("idx_media_cache_delete_due", status, delete_after),
        Index("idx_media_cache_source", file_unique_id, source_message_id),
    )


class MediaCacheJob(Base):
    """Restart-safe Google Drive fill job with a resumable-upload lease."""
    __tablename__ = "media_cache_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    cache_key: Mapped[str] = mapped_column(
        ForeignKey("media_cache_entries.cache_key", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, default="fill")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    resumable_upload_url: Mapped[Optional[str]] = mapped_column(Text)
    bytes_uploaded: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("idx_media_cache_job_claim", status, next_attempt_at, lease_expires_at),
    )


class MediaCacheLock(Base):
    """Database lease used to elect one cleanup/reconciliation worker."""
    __tablename__ = "media_cache_locks"

    lock_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class MediaCacheDailyUsage(Base):
    """Small per-day counter table for egress guardrails and observability."""
    __tablename__ = "media_cache_daily_usage"

    usage_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    drive_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    telegram_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    edge_hits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
