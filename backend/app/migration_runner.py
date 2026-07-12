"""Tiny SQL migration runner for TelePlay production PostgreSQL/NeonDB.

It records applied files in schema_migrations and runs backend/migrations/*.sql
in lexical order. This keeps deploys repeatable without relying on manual copy
paste in Neon every time.
"""
from __future__ import annotations

from pathlib import Path
import re

from sqlalchemy import text
from sqlalchemy.engine import make_url

from .config import get_settings
from .database import engine

settings = get_settings()
MIGRATION_FILE_RE = re.compile(r"^\d{3}_.+\.sql$")


def migrations_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "migrations"


def _split_sql_statements(sql: str) -> list[str]:
    # The existing project migrations are simple SQL files. This splitter avoids
    # sending BEGIN/COMMIT as individual driver statements and handles comments.
    statements: list[str] = []
    current: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        current.append(raw_line)
        if line.endswith(";"):
            statement = "\n".join(current).strip().rstrip(";").strip()
            current = []
            if statement.upper() not in {"BEGIN", "COMMIT"}:
                statements.append(statement)
    if current:
        statement = "\n".join(current).strip().rstrip(";").strip()
        if statement:
            statements.append(statement)
    return statements


async def run_pending_migrations() -> list[str]:
    """Run pending migrations. Returns filenames that were applied."""
    db_url = make_url(settings.database_url)
    if not db_url.drivername.startswith("postgresql"):
        # SQLite/local dev still relies on SQLAlchemy create_all. The migration
        # SQL files are PostgreSQL/Neon focused.
        return []

    applied_now: list[str] = []
    migration_files = sorted(
        path for path in migrations_dir().glob("*.sql") if MIGRATION_FILE_RE.match(path.name)
    )

    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version VARCHAR(255) PRIMARY KEY, "
            "applied_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP"
            ")"
        ))
        result = await conn.execute(text("SELECT version FROM schema_migrations"))
        applied = {row[0] for row in result.fetchall()}

        for migration in migration_files:
            if migration.name in applied:
                continue
            for statement in _split_sql_statements(migration.read_text(encoding="utf-8")):
                await conn.execute(text(statement))
            await conn.execute(
                text("INSERT INTO schema_migrations(version) VALUES (:version)"),
                {"version": migration.name},
            )
            applied_now.append(migration.name)

    return applied_now
