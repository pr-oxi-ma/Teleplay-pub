-- TelePlay auth hardening v2 migration for PostgreSQL/NeonDB.
-- Run once before deploying the matching backend code.

BEGIN;

ALTER TABLE users
ADD COLUMN IF NOT EXISTS auth_version INTEGER NOT NULL DEFAULT 0;

ALTER TABLE login_codes
ALTER COLUMN code TYPE VARCHAR(64);

CREATE TABLE IF NOT EXISTS web_credentials (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    telegram_id BIGINT NOT NULL,
    username VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_web_credentials_username
ON web_credentials(username);

CREATE INDEX IF NOT EXISTS ix_web_credentials_telegram_id
ON web_credentials(telegram_id);

-- If an earlier test build accidentally created duplicate credentials for one
-- Telegram user, keep the newest row before adding the unique user index.
DELETE FROM web_credentials a
USING web_credentials b
WHERE a.user_id = b.user_id
  AND a.id < b.id;

-- One Telegram user can only have one web username/password login.
CREATE UNIQUE INDEX IF NOT EXISTS ux_web_credentials_user_id
ON web_credentials(user_id);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    telegram_id BIGINT NOT NULL,
    refresh_token_hash VARCHAR(128) NOT NULL,
    user_agent VARCHAR(500),
    ip_hash VARCHAR(128),
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    revoked_at TIMESTAMP WITHOUT TIME ZONE
);

CREATE INDEX IF NOT EXISTS ix_auth_sessions_session_id
ON auth_sessions(session_id);

CREATE INDEX IF NOT EXISTS ix_auth_sessions_telegram_id
ON auth_sessions(telegram_id);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_active
ON auth_sessions(user_id, revoked_at);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires
ON auth_sessions(expires_at);

COMMIT;
