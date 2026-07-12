-- Adds indexes used by automatic auth-session cleanup and per-user caps.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_seen
ON auth_sessions(user_id, last_seen_at);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_revoked
ON auth_sessions(revoked_at);

COMMIT;
