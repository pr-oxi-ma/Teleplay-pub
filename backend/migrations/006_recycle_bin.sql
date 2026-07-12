BEGIN;

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    recycle_bin_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    recycle_bin_retention_days INTEGER NOT NULL DEFAULT 30,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_recycle_bin_retention_days CHECK (
        recycle_bin_retention_days BETWEEN 1 AND 365
    )
);

ALTER TABLE files ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE files ADD COLUMN IF NOT EXISTS purge_after TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE files ADD COLUMN IF NOT EXISTS trash_root_id INTEGER;

ALTER TABLE folders ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE folders ADD COLUMN IF NOT EXISTS purge_after TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE folders ADD COLUMN IF NOT EXISTS trash_root_id INTEGER;

CREATE INDEX IF NOT EXISTS ix_files_deleted_at ON files (deleted_at);
CREATE INDEX IF NOT EXISTS ix_files_purge_after ON files (purge_after);
CREATE INDEX IF NOT EXISTS ix_files_trash_root_id ON files (trash_root_id);
CREATE INDEX IF NOT EXISTS ix_folders_deleted_at ON folders (deleted_at);
CREATE INDEX IF NOT EXISTS ix_folders_purge_after ON folders (purge_after);
CREATE INDEX IF NOT EXISTS ix_folders_trash_root_id ON folders (trash_root_id);

COMMIT;
