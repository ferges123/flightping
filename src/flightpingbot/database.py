from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite

from .migrations import migrate


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        # Repository writes can span several awaits.  They must not share one
        # SQLite transaction with another coroutine using this connection.
        self.write_lock = asyncio.Lock()

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

    def __getattr__(self, name):
        if name in {"execute", "executemany", "executescript", "commit", "rollback"} and self.conn:
            return getattr(self.conn, name)
        raise AttributeError(name)
