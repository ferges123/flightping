from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps

from ..database import Database


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def serialized_write(method):
    """Serialize a multi-statement write against the shared connection.

    If the method fails after executing statements but before committing, the
    open transaction is rolled back so a later writer's commit cannot flush
    the partial data.
    """
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self.db.write_lock:
            try:
                return await method(self, *args, **kwargs)
            except BaseException:
                await self.db.rollback()
                raise
    return wrapped


class BaseRepository:
    """Shared database handle for all domain repositories."""

    def __init__(self, db: Database):
        self.db = db
