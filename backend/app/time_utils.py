"""Time helpers for consistent UTC timestamps."""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return a naive UTC datetime for existing DateTime columns.

    SQLAlchemy models in this project use naive DateTime columns. Python 3.12+
    deprecates datetime.utcnow(), so keep the DB representation stable while
    deriving it from an aware UTC timestamp.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
