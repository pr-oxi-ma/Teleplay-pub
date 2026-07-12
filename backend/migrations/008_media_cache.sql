-- Base managed Google Drive cache catalog.
-- Kept under the original filename for compatibility with deployments that
-- already recorded this migration before the production job/lease upgrade.
CREATE TABLE IF NOT EXISTS media_cache_entries (
    id BIGSERIAL PRIMARY KEY,
    cache_key VARCHAR(64) NOT NULL UNIQUE,
    file_unique_id VARCHAR(255) NOT NULL,
    source_message_id BIGINT NOT NULL,
    file_name VARCHAR(500) NOT NULL,
    mime_type VARCHAR(100),
    size_bytes BIGINT NOT NULL DEFAULT 0,
    drive_file_id VARCHAR(255) UNIQUE,
    status VARCHAR(32) NOT NULL DEFAULT 'uploading',
    pinned BOOLEAN NOT NULL DEFAULT FALSE,
    hit_count BIGINT NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    upload_started_at TIMESTAMP WITHOUT TIME ZONE,
    upload_completed_at TIMESTAMP WITHOUT TIME ZONE,
    last_access_at TIMESTAMP WITHOUT TIME ZONE,
    next_retry_at TIMESTAMP WITHOUT TIME ZONE,
    delete_after TIMESTAMP WITHOUT TIME ZONE,
    eviction_reason VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS ix_media_cache_entries_cache_key ON media_cache_entries (cache_key);
CREATE INDEX IF NOT EXISTS ix_media_cache_entries_file_unique_id ON media_cache_entries (file_unique_id);
CREATE INDEX IF NOT EXISTS ix_media_cache_entries_drive_file_id ON media_cache_entries (drive_file_id);
CREATE INDEX IF NOT EXISTS ix_media_cache_entries_status ON media_cache_entries (status);
CREATE INDEX IF NOT EXISTS ix_media_cache_entries_pinned ON media_cache_entries (pinned);
CREATE INDEX IF NOT EXISTS ix_media_cache_entries_upload_started_at ON media_cache_entries (upload_started_at);
CREATE INDEX IF NOT EXISTS ix_media_cache_entries_upload_completed_at ON media_cache_entries (upload_completed_at);
CREATE INDEX IF NOT EXISTS ix_media_cache_entries_last_access_at ON media_cache_entries (last_access_at);
CREATE INDEX IF NOT EXISTS ix_media_cache_entries_next_retry_at ON media_cache_entries (next_retry_at);
CREATE INDEX IF NOT EXISTS ix_media_cache_entries_delete_after ON media_cache_entries (delete_after);
CREATE INDEX IF NOT EXISTS idx_media_cache_status_access ON media_cache_entries (status, pinned, last_access_at);
CREATE INDEX IF NOT EXISTS idx_media_cache_delete_due ON media_cache_entries (status, delete_after);
