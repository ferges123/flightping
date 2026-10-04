from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import re
import urllib.parse
from typing import Any

import aiosqlite
import asyncpg

from .migrations import migrate

_INSERT_OR_IGNORE_RE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", re.IGNORECASE)
_AUTO_ID_TABLES = {
    "checks",
    "flight_observations",
    "api_requests",
    "monitor_jobs",
    "alerts",
    "audit_events",
    "monitor_subscriptions",
    "access_requests",
}


def qmark_to_dollar(sql: str) -> str:
    """Convert '?' parameter placeholders to '$1', '$2', ... for asyncpg,
    ignoring '?' inside single- or double-quoted string literals."""
    out = []
    param_idx = 1
    in_single_quote = False
    in_double_quote = False
    i = 0
    n = len(sql)
    while i < n:
        char = sql[i]
        if char == "'" and not in_double_quote:
            if in_single_quote and i + 1 < n and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_single_quote = not in_single_quote
            out.append(char)
        elif char == '"' and not in_single_quote:
            if in_double_quote and i + 1 < n and sql[i + 1] == '"':
                out.append('""')
                i += 2
                continue
            in_double_quote = not in_double_quote
            out.append(char)
        elif char == "?" and not in_single_quote and not in_double_quote:
            out.append(f"${param_idx}")
            param_idx += 1
        else:
            out.append(char)
        i += 1
    return "".join(out)


def prepare_postgres_sql(sql: str) -> str:
    """Translate SQLite-specific idioms to PostgreSQL equivalents."""
    if _INSERT_OR_IGNORE_RE.match(sql):
        sql = _INSERT_OR_IGNORE_RE.sub("INSERT INTO ", sql)
        if "ON CONFLICT" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return qmark_to_dollar(sql)


def mask_database_url(url: str) -> str:
    """Mask the password component in a database connection URL for safe display."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.password:
            netloc = f"{parsed.username}:***@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    except Exception:
        pass
    return url


class _ReadCursor:
    """Close SELECT cursors immediately after their result is consumed."""

    def __init__(self, cursor):
        self._cursor = cursor
        self._closed = False

    @property
    def lastrowid(self):
        return getattr(self._cursor, "lastrowid", None)

    @property
    def rowcount(self):
        return getattr(self._cursor, "rowcount", -1)

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

    async def __aiter__(self):
        while True:
            row = await self.fetchone()
            if row is None:
                break
            yield row


class _PgCursor:
    """Cursor-like wrapper over asyncpg result sets."""

    def __init__(self, rows: list | None = None, rowcount: int = 0, lastrowid: int | None = None):
        self._rows = list(rows) if rows is not None else []
        self._idx = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self._closed = False

    async def fetchone(self):
        if self._idx < len(self._rows):
            row = self._rows[self._idx]
            self._idx += 1
            return row
        return None

    async def fetchall(self):
        if self._idx == 0:
            self._idx = len(self._rows)
            return self._rows
        remaining = self._rows[self._idx:]
        self._idx = len(self._rows)
        return remaining

    async def close(self) -> None:
        self._closed = True

    async def __aiter__(self):
        while True:
            row = await self.fetchone()
            if row is None:
                break
            yield row


class Database:
    """Unified database factory and base class.

    Instantiating ``Database(target)`` returns a ``SqliteDatabase`` when
    ``target`` is a ``Path`` or SQLite path string, and a ``PostgresDatabase``
    when ``target`` is a PostgreSQL connection URL.
    """

    def __new__(cls, target: Path | str, *args, **kwargs):
        if cls is Database:
            target_str = str(target).strip()
            if target_str.startswith(("postgres://", "postgresql://", "postgresql+asyncpg://")):
                return super().__new__(PostgresDatabase)
            return super().__new__(SqliteDatabase)
        return super().__new__(cls)

    @classmethod
    def from_settings(cls, settings) -> "Database":
        return cls(getattr(settings, "database_target", getattr(settings, "database_path", "flightpingbot.sqlite3")))


class SqliteDatabase(Database):
    """Single shared SQLite connection.

    Concurrency model
    -----------------
    All coroutines share one connection, so they also share one transaction
    state. Multi-statement writes must hold ``write_lock`` (see
    ``repositories.base.serialized_write``); otherwise two writers could
    interleave their statements inside one transaction.

    Reads do NOT take the lock. A single SELECT is atomic, but a SELECT
    issued between another coroutine's ``execute`` and ``commit`` will see
    that uncommitted data. When several reads need a mutually consistent
    picture (e.g. dashboard aggregates), wrap them in ``consistent_reads()``.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.backend = "sqlite"
        self.conn: aiosqlite.Connection | None = None
        self.write_lock = asyncio.Lock()

    @asynccontextmanager
    async def consistent_reads(self):
        """Hold the write lock across multiple reads."""
        async with self.write_lock:
            yield

    async def connect(self) -> "SqliteDatabase":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000; PRAGMA synchronous=NORMAL;")
        await migrate(self)
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


class PostgresDatabase(Database):
    """PostgreSQL database connection pool with transaction-aware serialization."""

    def __init__(self, url: str, *, min_pool_size: int = 1, max_pool_size: int = 10):
        self.url = str(url)
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size
        self.backend = "postgres"
        self.pool: asyncpg.Pool | None = None
        self.write_lock = asyncio.Lock()
        self._tx_conn: asyncpg.Connection | None = None
        self._tx: Any | None = None
        self._tx_task: asyncio.Task | None = None
        self._last_insert_id: int | None = None

    @property
    def path(self) -> str:
        """Display path for admin status reporting."""
        return mask_database_url(self.url)

    @asynccontextmanager
    async def consistent_reads(self):
        """Hold the write lock across multiple reads."""
        async with self.write_lock:
            yield

    async def connect(self) -> "PostgresDatabase":
        clean_url = self.url
        if clean_url.startswith("postgresql+asyncpg://"):
            clean_url = "postgresql://" + clean_url[len("postgresql+asyncpg://"):]
        if "sslmode=" not in clean_url and "ssl=" not in clean_url:
            from urllib.parse import urlparse
            try:
                parsed = urlparse(clean_url)
                if parsed.hostname in ("127.0.0.1", "localhost", "::1"):
                    sep = "&" if "?" in clean_url else "?"
                    clean_url = f"{clean_url}{sep}sslmode=disable"
            except Exception:
                pass
        self.pool = await asyncpg.create_pool(
            clean_url,
            min_size=self.min_pool_size,
            max_size=self.max_pool_size,
        )
        await migrate(self)
        return self

    async def close(self) -> None:
        if self._tx_conn is not None:
            try:
                if self._tx is not None:
                    await self._tx.rollback()
            except Exception:
                pass
            try:
                if self.pool is not None:
                    await self.pool.release(self._tx_conn)
            except Exception:
                pass
            self._tx_conn = None
            self._tx = None
            self._tx_task = None
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    def _require_pool(self) -> asyncpg.Pool:
        if not self.pool:
            raise RuntimeError("Database is not connected")
        return self.pool

    async def _ensure_transaction(self) -> asyncpg.Connection:
        cur_task = asyncio.current_task()
        if self._tx_conn is not None and self._tx_task == cur_task:
            return self._tx_conn
        pool = self._require_pool()
        conn = await pool.acquire()
        tx = conn.transaction()
        await tx.start()
        self._tx_conn = conn
        self._tx = tx
        self._tx_task = cur_task
        return conn

    async def commit(self) -> None:
        cur_task = asyncio.current_task()
        if self._tx_conn is not None and self._tx_task == cur_task:
            tx = self._tx
            conn = self._tx_conn
            self._tx = None
            self._tx_conn = None
            self._tx_task = None
            try:
                if tx is not None:
                    await tx.commit()
            finally:
                if self.pool is not None and conn is not None:
                    await self.pool.release(conn)

    async def rollback(self) -> None:
        cur_task = asyncio.current_task()
        if self._tx_conn is not None and self._tx_task == cur_task:
            tx = self._tx
            conn = self._tx_conn
            self._tx = None
            self._tx_conn = None
            self._tx_task = None
            try:
                if tx is not None:
                    await tx.rollback()
            finally:
                if self.pool is not None and conn is not None:
                    await self.pool.release(conn)

    async def execute(self, sql: str, parameters=None):
        stripped = sql.strip().upper()
        if stripped in ("BEGIN IMMEDIATE", "BEGIN"):
            await self._ensure_transaction()
            return _PgCursor()
        if stripped == "SELECT LAST_INSERT_ROWID()":
            return _PgCursor(rows=[(self._last_insert_id,)])

        converted_sql = prepare_postgres_sql(sql)
        params = () if parameters is None else (tuple(parameters) if isinstance(parameters, (list, tuple)) else (parameters,))

        has_returning_id = False
        if stripped.startswith("INSERT INTO"):
            parts = sql.strip().split()
            if len(parts) >= 3:
                table = parts[2].lower().split("(")[0].strip('"')
                if table in _AUTO_ID_TABLES and "RETURNING" not in stripped:
                    converted_sql += " RETURNING id"
                    has_returning_id = True
                elif "RETURNING ID" in stripped:
                    has_returning_id = True

        cur_task = asyncio.current_task()
        if stripped.startswith(("INSERT", "UPDATE", "DELETE")):
            conn = await self._ensure_transaction()
        elif self._tx_conn is not None and self._tx_task == cur_task:
            conn = self._tx_conn
        else:
            conn = None

        if conn is not None:
            if has_returning_id or stripped.startswith("SELECT"):
                rows = await conn.fetch(converted_sql, *params)
                lastrowid = rows[0]["id"] if (has_returning_id and rows and "id" in rows[0]) else (rows[0][0] if (has_returning_id and rows) else None)
                if lastrowid is not None:
                    self._last_insert_id = lastrowid
                return _PgCursor(rows=rows, rowcount=len(rows), lastrowid=lastrowid)
            else:
                status = await conn.execute(converted_sql, *params)
                rowcount = int(status.split()[-1]) if status and status.split()[-1].isdigit() else 0
                return _PgCursor(rows=[], rowcount=rowcount)
        else:
            pool = self._require_pool()
            async with pool.acquire() as pconn:
                if has_returning_id or stripped.startswith("SELECT"):
                    rows = await pconn.fetch(converted_sql, *params)
                    lastrowid = rows[0]["id"] if (has_returning_id and rows and "id" in rows[0]) else (rows[0][0] if (has_returning_id and rows) else None)
                    if lastrowid is not None:
                        self._last_insert_id = lastrowid
                    return _PgCursor(rows=rows, rowcount=len(rows), lastrowid=lastrowid)
                else:
                    status = await pconn.execute(converted_sql, *params)
                    rowcount = int(status.split()[-1]) if status and status.split()[-1].isdigit() else 0
                    return _PgCursor(rows=[], rowcount=rowcount)

    async def executemany(self, sql: str, parameters) -> None:
        if not parameters:
            return
        converted_sql = prepare_postgres_sql(sql)
        conn = await self._ensure_transaction()
        await conn.executemany(converted_sql, parameters)

    async def status_text(self) -> str:
        """HTML status string for the Telegram /db_status administrative command."""
        if self.pool is None:
            return "<b>Database status</b>\n\nBackend: PostgreSQL (disconnected)"
        try:
            async with self.pool.acquire() as conn:
                db_name = await conn.fetchval("SELECT current_database()")
                db_size = await conn.fetchval("SELECT pg_size_pretty(pg_database_size(current_database()))")
                pg_version = await conn.fetchval("SHOW server_version")
                active_connections = await conn.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                )
            masked = self.path
            return (
                f"<b>Database status</b>\n\n"
                f"Backend: PostgreSQL {pg_version}\n"
                f"Database: {db_name}\n"
                f"Target: {masked}\n"
                f"Size: {db_size}\n"
                f"Connections: {active_connections}"
            )
        except Exception as exc:
            return f"<b>Database status</b>\n\nBackend: PostgreSQL\nError: {exc}"
