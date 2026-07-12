BEGIN;

CREATE INDEX IF NOT EXISTS idx_file_user_unique
ON files (user_id, file_unique_id);

COMMIT;
