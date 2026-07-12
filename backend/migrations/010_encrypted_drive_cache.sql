-- Encrypted Google Drive L2 cache and legacy plaintext migration metadata.
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS legacy_drive_file_id VARCHAR(255);
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS encryption_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS encryption_nonce_prefix VARCHAR(32);
ALTER TABLE media_cache_entries ADD COLUMN IF NOT EXISTS encrypted_size_bytes BIGINT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS ix_media_cache_entries_legacy_drive_file_id
    ON media_cache_entries (legacy_drive_file_id);
CREATE INDEX IF NOT EXISTS idx_media_cache_encryption_status
    ON media_cache_entries (encryption_version, status, last_verified_at);
