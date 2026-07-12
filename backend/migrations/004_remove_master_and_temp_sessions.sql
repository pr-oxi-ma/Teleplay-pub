-- TelePlay auth v4.5 migration.
-- Removes master-login dependency in code and adds temporary-session metadata
-- for one-time code/link logins with heartbeat auto-revoke.

BEGIN;

ALTER TABLE auth_sessions
ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP WITHOUT TIME ZONE;

UPDATE auth_sessions
SET last_seen_at = COALESCE(last_seen_at, last_used_at, created_at, CURRENT_TIMESTAMP)
WHERE last_seen_at IS NULL;

ALTER TABLE auth_sessions
ALTER COLUMN last_seen_at SET DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE auth_sessions
ADD COLUMN IF NOT EXISTS session_type VARCHAR(20) NOT NULL DEFAULT 'persistent';

UPDATE auth_sessions
SET session_type = 'persistent'
WHERE session_type IS NULL OR session_type NOT IN ('persistent', 'temporary');

CREATE INDEX IF NOT EXISTS idx_auth_sessions_type_seen
ON auth_sessions(session_type, last_seen_at);

COMMIT;
