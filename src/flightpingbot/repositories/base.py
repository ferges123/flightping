from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps

from ..database import Database


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def serialized_write(method):
    """Serialize a multi-statement write against the shared connection."""
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self.db.write_lock:
            return await method(self, *args, **kwargs)
    return wrapped


class BaseRepository:
    """Shared database handle for all domain repositories."""

    def __init__(self, db: Database):
        self.db = db
