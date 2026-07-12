"""
Database setup with SQLAlchemy async support.
Supports both SQLite (for development) and PostgreSQL (for production).
"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Session, with_loader_criteria
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from pathlib import Path
from .config import get_settings

settings = get_settings()

# Convert database URL for async drivers and handle query params
url = make_url(settings.database_url)

if url.drivername == "postgresql":
    url = url.set(drivername="postgresql+asyncpg")
    # Remove 'schema' from query params if present (asyncpg doesn't support it in connect args)
    if "schema" in url.query:
        query = dict(url.query)
        del query["schema"]
        url = url.set(query=query)
elif url.drivername == "sqlite":
    # Ensure the SQLite directory exists before SQLAlchemy opens the DB file.
    # This fixes fresh Docker/PaaS starts where ./data has not been created yet.
    if url.database and url.database not in {":memory:", ""}:
        Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    url = url.set(drivername="sqlite+aiosqlite")

engine_kwargs = {
    "echo": False,
    "pool_pre_ping": True,
}

# Queue-pool options are useful for persistent databases, but SQLite should not
# get a huge connection pool. SQLite is typically local/single-node and too many
# connections add lock contention instead of throughput.
if "postgresql" in url.drivername:
    engine_kwargs.update({
        "pool_recycle": 1800,
        "pool_size": 20,
        "max_overflow": 10,
    })
elif url.drivername == "sqlite+aiosqlite" and url.database and url.database != ":memory:":
    engine_kwargs.update({
        "pool_recycle": 1800,
        "pool_size": 5,
        "max_overflow": 5,
    })

engine = create_async_engine(url, **engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


@event.listens_for(Session, "do_orm_execute")
def exclude_recycle_bin_items(execute_state):
    """Hide soft-deleted files/folders from every normal ORM SELECT.

    Recycle-bin code explicitly opts out with include_deleted=True. Keeping the
    default here prevents a missed filter in bot, TV, streaming or future routes
    from exposing deleted content.
    """
    if not execute_state.is_select or execute_state.execution_options.get("include_deleted"):
        return

    # Local import avoids a circular dependency while models import Base.
    from .models import File, Folder

    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(File, lambda model: model.deleted_at.is_(None), include_aliases=True),
        with_loader_criteria(Folder, lambda model: model.deleted_at.is_(None), include_aliases=True),
    )


async def get_db():
    """Dependency for getting database session."""
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Create all tables."""
    # Ensure model modules are registered even when init_db is called from a
    # migration/health script rather than through app.main imports.
    from . import models as _models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # SQLAlchemy create_all does not add columns to an existing SQLite DB.
        # Production PostgreSQL uses numbered migrations; this keeps local
        # SQLite installs upgradeable without an extra migration dependency.
        if conn.dialect.name == "sqlite":
            for table_name in ("files", "folders"):
                rows = await conn.execute(text(f"PRAGMA table_info({table_name})"))
                columns = {str(row[1]) for row in rows.fetchall()}
                additions = {
                    "deleted_at": "DATETIME",
                    "purge_after": "DATETIME",
                    "trash_root_id": "INTEGER",
                }
                for column, column_type in additions.items():
                    if column not in columns:
                        await conn.execute(
                            text(f"ALTER TABLE {table_name} ADD COLUMN {column} {column_type}")
                        )
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_files_deleted_at ON files (deleted_at)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_files_purge_after ON files (purge_after)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_files_trash_root_id ON files (trash_root_id)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_folders_deleted_at ON folders (deleted_at)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_folders_purge_after ON folders (purge_after)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_folders_trash_root_id ON folders (trash_root_id)"
            ))

            # Upgrade compatibility for the earlier media-cache prototype.
            cache_rows = await conn.execute(text("PRAGMA table_info(media_cache_entries)"))
            cache_columns = {str(row[1]) for row in cache_rows.fetchall()}
            if cache_columns:
                cache_additions = {
                    "cache_version": "INTEGER NOT NULL DEFAULT 2",
                    "file_type": "VARCHAR(50) NOT NULL DEFAULT 'document'",
                    "active_readers": "INTEGER NOT NULL DEFAULT 0",
                    "read_lease_until": "DATETIME",
                    "edge_hit_count": "BIGINT NOT NULL DEFAULT 0",
                    "drive_hit_count": "BIGINT NOT NULL DEFAULT 0",
                    "telegram_hit_count": "BIGINT NOT NULL DEFAULT 0",
                    "telegram_bytes_served": "BIGINT NOT NULL DEFAULT 0",
                    "truncated_read_count": "INTEGER NOT NULL DEFAULT 0",
                    "last_edge_access_at": "DATETIME",
                    "last_drive_access_at": "DATETIME",
                    "last_telegram_access_at": "DATETIME",
                    "last_verified_at": "DATETIME",
                }
                for column, column_type in cache_additions.items():
                    if column not in cache_columns:
                        await conn.execute(text(
                            f"ALTER TABLE media_cache_entries ADD COLUMN {column} {column_type}"
                        ))
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_media_cache_entries_read_lease_until "
                    "ON media_cache_entries (read_lease_until)"
                ))
