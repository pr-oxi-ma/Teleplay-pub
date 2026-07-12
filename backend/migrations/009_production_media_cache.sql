-- Production hybrid cache upgrade: durable fill jobs, leases, popularity, and usage counters.
-- Runs after 008_media_cache.sql on fresh installs and also upgrades databases
-- that already applied the earlier 008_media_cache.sql implementation.

CREATE TABLE IF NOT EXISTS media_cache_entries (
    id BIGSERIAL PRIMARY KEY,
    cache_key VARCHAR(64) NOT NULL UNIQUE,
    cache_version INTEGER NOT NULL DEFAULT 2,
    file_unique_id VARCHAR(255) NOT NULL,
    source_message_id BIGINT NOT NULL,
    file_name VARCHAR(500) NOT NULL,
    mime_type VARCHAR(100),
    file_type VARCHAR(50) NOT NULL DEFAULT 'document',
    size_bytes BIGINT NOT NULL DEFAULT 0,
    drive_file_id VARCHAR(255) UNIQUE,
    status VARCHAR(32) NOT NULL DEFAULT 'observed',
    pinned BOOLEAN NOT NULL DEFAULT FALSE,
    active_readers INTEGER NOT NULL DEFAULT 0,
    read_lease_until TIMESTAMP WITHOUT TIME ZONE,
    edge_hit_count BIGINT NOT NULL DEFAULT 0,
    drive_hit_count BIGINT NOT NULL DEFAULT 0,
    telegram_hit_count BIGINT NOT NULL DEFAULT 0,
    telegram_bytes_served BIGINT NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    truncated_read_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    upload_started_at TIMESTAMP WITHOUT TIME ZONE,
    upload_completed_at TIMESTAMP WITHOUT TIME ZONE,
    last_access_at TIMESTAMP WITHOUT TIME ZONE,
    last_edge_access_at TIMESTAMP WITHOUT TIME ZONE,
    last_drive_access_at TIMESTAMP WITHOUT TIME ZONE,
    last_telegram_access_at TIMESTAMP WITHOUT TIME ZONE,
    next_retry_at TIMESTAMP WITHOUT TIME ZONE,
    delete_after TIMESTAMP WITHOUT TIME ZONE,
    eviction_reason VARCHAR(100),
    last_verified_at TIMESTAMP WITHOUT TIME ZONE
);

-- Upgrade compatibility for installations that previously used the early
-- media_cache_entries prototype. Extra legacy columns are harmless.
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS cache_version INTEGER NOT NULL DEFAULT 2;
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS file_type VARCHAR(50) NOT NULL DEFAULT 'document';
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS active_readers INTEGER NOT NULL DEFAULT 0;
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS read_lease_until TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS edge_hit_count BIGINT NOT NULL DEFAULT 0;
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS drive_hit_count BIGINT NOT NULL DEFAULT 0;
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS telegram_hit_count BIGINT NOT NULL DEFAULT 0;
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS telegram_bytes_served BIGINT NOT NULL DEFAULT 0;
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS truncated_read_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS last_edge_access_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS last_drive_access_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS last_telegram_access_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMP WITHOUT TIME ZONE;

CREATE INDEX IF NOT EXISTS ix_media_cache_entries_cache_key ON media_cache_entries (cache_key);
CREATE INDEX IF NOT EXISTS ix_media_cache_entries_file_unique_id ON media_cache_entries (file_unique_id);
CREATE INDEX IF NOT EXISTS ix_media_cache_entries_drive_file_id ON media_cache_entries (drive_file_id);
CREATE INDEX IF NOT EXISTS ix_media_cache_entries_status ON media_cache_entries (status);
CREATE INDEX IF NOT EXISTS ix_media_cache_entries_read_lease_until ON media_cache_entries (read_lease_until);
CREATE INDEX IF NOT EXISTS idx_media_cache_status_access ON media_cache_entries (status, pinned, last_access_at);
CREATE INDEX IF NOT EXISTS idx_media_cache_delete_due ON media_cache_entries (status, delete_after);
CREATE INDEX IF NOT EXISTS idx_media_cache_source ON media_cache_entries (file_unique_id, source_message_id);

CREATE TABLE IF NOT EXISTS media_cache_jobs (
    id BIGSERIAL PRIMARY KEY,
    cache_key VARCHAR(64) NOT NULL UNIQUE REFERENCES media_cache_entries(cache_key) ON DELETE CASCADE,
    job_type VARCHAR(32) NOT NULL DEFAULT 'fill',
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    lease_owner VARCHAR(128),
    lease_expires_at TIMESTAMP WITHOUT TIME ZONE,
    resumable_upload_url TEXT,
    bytes_uploaded BIGINT NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP WITHOUT TIME ZONE,
    last_error TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_media_cache_jobs_cache_key ON media_cache_jobs (cache_key);
CREATE INDEX IF NOT EXISTS ix_media_cache_jobs_status ON media_cache_jobs (status);
CREATE INDEX IF NOT EXISTS ix_media_cache_jobs_lease_owner ON media_cache_jobs (lease_owner);
CREATE INDEX IF NOT EXISTS ix_media_cache_jobs_lease_expires_at ON media_cache_jobs (lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_media_cache_jobs_next_attempt_at ON media_cache_jobs (next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_media_cache_job_claim ON media_cache_jobs (status, next_attempt_at, lease_expires_at);

CREATE TABLE IF NOT EXISTS media_cache_locks (
    lock_key VARCHAR(64) PRIMARY KEY,
    owner VARCHAR(128) NOT NULL,
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_media_cache_locks_expires_at ON media_cache_locks (expires_at);

CREATE TABLE IF NOT EXISTS media_cache_daily_usage (
    usage_date VARCHAR(10) PRIMARY KEY,
    drive_bytes BIGINT NOT NULL DEFAULT 0,
    telegram_bytes BIGINT NOT NULL DEFAULT 0,
    edge_hits BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
