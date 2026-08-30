from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from .migrations import migrate


class _ReadCursor:
    """Close SELECT cursors immediately after their result is consumed."""

    def __init__(self, cursor):
        self._cursor = cursor
        self._closed = False

    async def fetchone(self):
        try:
            return await self._cursor.fetchone()
        finally:
            await self.close()

    async def fetchall(self):
        try:
            return await self._cursor.fetchall()
        finally:
            await self.close()

    async def close(self) -> None:
        if not self._closed:
            await self._cursor.close()
            self._closed = True


class Database:
    """Single shared SQLite connection.

    Concurrency model
    -----------------
    All coroutines share one connection, so they also share one transaction
    state.  Multi-statement writes must hold ``write_lock`` (see
    ``repositories.base.serialized_write``); otherwise two writers could
    interleave their statements inside one transaction.

    Reads do NOT take the lock.  A single SELECT is atomic, but a SELECT
    issued between another coroutine's ``execute`` and ``commit`` will see
    that uncommitted data.  When several reads need a mutually consistent
    picture (e.g. dashboard aggregates), wrap them in ``consistent_reads()``.
    """

    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        # Repository writes can span several awaits.  They must not share one
        # SQLite transaction with another coroutine using this connection.
        self.write_lock = asyncio.Lock()

    @asynccontextmanager
    async def consistent_reads(self):
        """Hold the write lock across multiple reads.

        Prevents interleaving with an in-flight write transaction so the
        grouped reads observe a snapshot that never mixes pre- and post-write
        state.  Use sparingly: it delays writers while the section runs.
        """
        async with self.write_lock:
            yield

    async def connect(self) -> "Database":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000; PRAGMA synchronous=NORMAL;")
        await migrate(self.conn)
        self._secure_files()
        return self

    def _secure_files(self) -> None:
        for path in (
            self.path,
            self.path.with_name(self.path.name + "-wal"),
            self.path.with_name(self.path.name + "-shm"),
        ):
            if path.exists():
                path.chmod(0o600)

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    def _require_connection(self) -> aiosqlite.Connection:
        if not self.conn:
            raise RuntimeError("Database is not connected")
        return self.conn

    async def execute(self, sql: str, parameters=None):
        conn = self._require_connection()
        cursor = await (conn.execute(sql, parameters) if parameters is not None else conn.execute(sql))
        return _ReadCursor(cursor) if sql.lstrip().upper().startswith("SELECT") else cursor

    async def executemany(self, sql: str, parameters) -> None:
        await self._require_connection().executemany(sql, parameters)

    async def commit(self) -> None:
        await self._require_connection().commit()

    async def rollback(self) -> None:
        await self._require_connection().rollback()
