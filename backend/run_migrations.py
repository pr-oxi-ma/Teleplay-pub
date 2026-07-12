"""Run pending PostgreSQL/NeonDB migrations.

Usage from repository root:
    python backend/run_migrations.py

Or from backend folder:
    python run_migrations.py
"""
import asyncio
from pathlib import Path
import sys

# Make backend/app importable as `app`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.migration_runner import run_pending_migrations  # noqa: E402


async def main() -> None:
    applied = await run_pending_migrations()
    if applied:
        print("Applied migrations:")
        for version in applied:
            print(f"- {version}")
    else:
        print("No pending migrations.")


if __name__ == "__main__":
    asyncio.run(main())
