import asyncio
import os
from pathlib import Path
import pytest

from flightpingbot.config import ConfigError, Settings
from flightpingbot.database import (
    Database,
    PostgresDatabase,
    SqliteDatabase,
    _PgCursor,
    mask_database_url,
    prepare_postgres_sql,
    qmark_to_dollar,
)
from flightpingbot.maintenance import Maintenance
from flightpingbot.repositories import Repository
from flightpingbot.statuses import CheckStatus, MonitorJobStatus, UserStatus

TEST_PG_URL = os.getenv("TEST_DATABASE_URL", "postgresql://postgres:secret@127.0.0.1:55432/flightping")


def test_qmark_to_dollar():
    assert qmark_to_dollar("SELECT * FROM users WHERE id=?") == "SELECT * FROM users WHERE id=$1"
    assert qmark_to_dollar("INSERT INTO t (a, b) VALUES (?, ?)") == "INSERT INTO t (a, b) VALUES ($1, $2)"
    assert qmark_to_dollar("SELECT 'what?', ? FROM foo WHERE x='can''t?' AND y=?") == "SELECT 'what?', $1 FROM foo WHERE x='can''t?' AND y=$2"
    assert qmark_to_dollar('SELECT "col?", ? FROM "table?" WHERE z=?') == 'SELECT "col?", $1 FROM "table?" WHERE z=$2'


def test_prepare_postgres_sql():
    sql = "INSERT OR IGNORE INTO monitor_subscriptions(job_id,telegram_user_id,chat_id,created_at) VALUES(?,?,?,?)"
    expected = "INSERT INTO monitor_subscriptions(job_id,telegram_user_id,chat_id,created_at) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING"
    assert prepare_postgres_sql(sql) == expected

    plain = "SELECT id, name FROM users WHERE id=?"
    assert prepare_postgres_sql(plain) == "SELECT id, name FROM users WHERE id=$1"


def test_mask_database_url():
    assert mask_database_url("postgresql://user:secret@localhost:5432/flightping") == "postgresql://user:***@localhost:5432/flightping"
    assert mask_database_url("postgresql://user@localhost:5432/flightping") == "postgresql://user@localhost:5432/flightping"
    assert mask_database_url("sqlite:///opt/flightping/state/db.sqlite3") == "sqlite:///opt/flightping/state/db.sqlite3"


def test_database_factory():
    db_sqlite_path = Database(Path("/tmp/test.sqlite3"))
    assert isinstance(db_sqlite_path, SqliteDatabase)
    assert db_sqlite_path.backend == "sqlite"

    db_sqlite_str = Database("sqlite:///tmp/test.sqlite3")
    assert isinstance(db_sqlite_str, SqliteDatabase)
    assert db_sqlite_str.backend == "sqlite"

    db_pg = Database("postgresql://user:secret@localhost:5432/db")
    assert isinstance(db_pg, PostgresDatabase)
    assert db_pg.backend == "postgres"

    db_pg_alias = Database("postgres://user:secret@localhost:5432/db")
    assert isinstance(db_pg_alias, PostgresDatabase)
    assert db_pg_alias.backend == "postgres"


def test_settings_database_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("FPB_BOT_TOKEN", "token")
    monkeypatch.setenv("FPB_ADMIN_USER_IDS", "123")
    monkeypatch.setenv("FPB_CREDENTIALS_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.setenv("FPB_STATE_DIR", str(tmp_path))

    # Default without FPB_DATABASE_URL
    monkeypatch.delenv("FPB_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings.from_env()
    assert settings.database_backend == "sqlite"
    assert settings.database_target == tmp_path / "flightpingbot.sqlite3"

    # PostgreSQL URL
    monkeypatch.setenv("FPB_DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    settings = Settings.from_env()
    assert settings.database_backend == "postgres"
    assert settings.database_target == "postgresql://user:pass@localhost:5432/testdb"

    # SQLite URL
    monkeypatch.setenv("FPB_DATABASE_URL", "sqlite:///custom/path.sqlite3")
    settings = Settings.from_env()
    assert settings.database_backend == "sqlite"
    assert settings.database_target == Path("/custom/path.sqlite3")

    # Invalid scheme
    monkeypatch.setenv("FPB_DATABASE_URL", "mysql://localhost/test")
    with pytest.raises(ConfigError, match="FPB_DATABASE_URL must be a PostgreSQL"):
        Settings.from_env()


@pytest.mark.asyncio
async def test_pg_cursor_behavior():
    rows = [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]
    cursor = _PgCursor(rows=rows, rowcount=2, lastrowid=1)
    assert cursor.lastrowid == 1
    assert cursor.rowcount == 2

    row1 = await cursor.fetchone()
    assert row1["name"] == "alice"
    remaining = await cursor.fetchall()
    assert len(remaining) == 1
    assert remaining[0]["name"] == "bob"

    assert await cursor.fetchone() is None
    await cursor.close()

    # Test async iteration
    cursor2 = _PgCursor(rows=rows)
    collected = []
    async for r in cursor2:
        collected.append(r["id"])
    assert collected == [1, 2]


class _MockConn:
    def __init__(self):
        self.executed = []
        self.fetched = []

    def transaction(self):
        return _MockTx()

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"

    async def fetch(self, query, *args):
        self.fetched.append((query, args))
        return [{"id": 42}]

    async def fetchval(self, query, *args):
        return "10 MB"

    async def executemany(self, query, params):
        self.executed.append((query, params))


class _MockTx:
    async def start(self):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


class _MockAcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def __await__(self):
        async def _coro():
            return self.conn
        return _coro().__await__()


class _MockPool:
    def __init__(self):
        self.conn = _MockConn()
        self.released = False
        self.closed = False

    def acquire(self):
        return _MockAcquireContext(self.conn)

    async def release(self, conn):
        self.released = True

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_postgres_database_unit_mocked():
    db = PostgresDatabase("postgresql://user:pass@localhost:5432/testdb")
    db.pool = _MockPool()

    # BEGIN IMMEDIATE starts a transaction
    await db.execute("BEGIN IMMEDIATE")
    assert db._tx_conn is not None

    # Write statement executed inside transaction
    cur = await db.execute("INSERT INTO checks (airport) VALUES (?)", ("WAW",))
    assert cur.lastrowid == 42
    assert db._last_insert_id == 42

    # SELECT last_insert_rowid() emulation
    cur_last = await db.execute("SELECT last_insert_rowid()")
    row = await cur_last.fetchone()
    assert row[0] == 42

    # executemany inside transaction
    await db.executemany("INSERT INTO checks (airport) VALUES (?)", [("TFS",), ("KRK",)])

    # Commit
    await db.commit()
    assert db._tx_conn is None

    # Rollback when no tx is safe no-op
    await db.rollback()

    # Consistent reads context manager
    async with db.consistent_reads():
        pass

    # Status text
    status = await db.status_text()
    assert "Backend: PostgreSQL" in status

    # Close with active transaction triggers rollback
    await db._ensure_transaction()
    await db.close()
    assert db.pool is None


async def _check_postgres_reachable() -> bool:
    try:
        import asyncpg
        conn = await asyncio.wait_for(asyncpg.connect(TEST_PG_URL), timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest.mark.asyncio
async def test_postgres_integration_suite():
    if not await _check_postgres_reachable():
        pytest.skip(f"PostgreSQL test database not available at {TEST_PG_URL}")

    # Reset test schema
    import asyncpg
    conn = await asyncpg.connect(TEST_PG_URL)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()

    db = await Database(TEST_PG_URL).connect()
    try:
        assert db.backend == "postgres"
        assert "postgresql://" in str(db.path)
        status_html = await db.status_text()
        assert "Backend: PostgreSQL" in status_html
        assert "Database:" in status_html

        repo = Repository(db)
        # Admin sync
        await repo.sync_admins(frozenset({100}))
        admin_user = await repo.user(100)
        assert admin_user["is_admin"] == 1

        # Access requests
        req_status, req_id = await repo.upsert_access_request(200, 200, "pilot", "Pilot")
        assert req_status == "requested"
        assert req_id is not None
        req_status_dup, req_id_dup = await repo.upsert_access_request(200, 200, "pilot", "Pilot")
        assert req_status_dup == "pending"
        assert req_id_dup == req_id

        # Access decision
        approved, decided_uid = await repo.decide_request(req_id, 100, True)
        assert approved is True
        assert decided_uid == 200
        assert (await repo.user(200))["status"] == "approved"

        # User settings
        await repo.update_user_settings(200, language="pl", window_hours=6, interval_minutes=15, min_delay_minutes=45, duration_hours=12)
        settings = await repo.user_settings(200)
        assert dict(settings) == {
            "telegram_user_id": 200,
            "language": "pl",
            "window_hours": 6,
            "interval_minutes": 15,
            "min_delay_minutes": 45,
            "duration_hours": 12,
        }

        # Monitor jobs
        job_id, is_new = await repo.create_monitor_job(200, 200, "TFS", 6, 15, min_delay_minutes=45)
        assert is_new is True
        active_jobs = await repo.active_monitor_jobs()
        assert len(active_jobs) == 1
        assert active_jobs[0]["airport"] == "TFS"

        # Checks and observations
        check_id = await repo.create_check(200, "TFS", 9)
        assert check_id is not None
        await repo.record_api_request(check_id, "airports/TFS", 200, 150, actor_user_id=200)

        flight = {"flight_id": "EWG253", "scheduled_departure": "2026-08-18T17:00:00Z", "delay_minutes": 100}
        claimed = await repo.claim_new_alerts(check_id, 200, [flight])
        assert len(claimed) == 1
        await repo.finish_alerts(check_id, 200, [flight], sent=True)

        # Batch observations
        flights = [
            {"flight_id": "W61234", "origin": "TFS", "destination": "WAW", "scheduled_departure": "2026-08-18T18:00:00Z", "delay_minutes": 70},
            {"flight_id": "W65678", "origin": "TFS", "destination": "KTW", "scheduled_departure": "2026-08-18T19:00:00Z", "delay_minutes": 30},
        ]
        await repo.record_observations(check_id, flights, threshold=60)
        obs = await repo.check_observations(check_id)
        assert len(obs) == 2

        # Delayed observations
        delayed = await repo.delayed_observations(limit=10)
        assert len(delayed) >= 1

        # Usage
        usage = await repo.usage("2026-08-01T00:00:00")
        assert usage["total"] == 1
        assert usage["success"] == 1

        usage_users = await repo.usage_by_user("2026-08-01T00:00:00")
        assert len(usage_users) == 1
        assert usage_users[0]["user_id"] == 200

        # Favorites
        assert await repo.add_favorite_airport(200, "WAW") == "added"
        assert await repo.add_favorite_airport(200, "WAW") == "exists"
        favs = await repo.favorite_airports(200)
        assert [f["airport"] for f in favs] == ["WAW"]
        assert await repo.remove_favorite_airport(200, "WAW") is True

        # Audit
        await repo.audit(100, "test_action", "user", "200", {"k": "v"})
        events = await repo.list_audit(days=1)
        assert len(events) >= 1
        assert events[0]["action"] == "test_action"

        # Maintenance retention on Postgres
        m = Maintenance(db, 30, 30, 30, Path("/tmp"), check_days=30, alert_days=30, monitor_job_days=30)
        await m.run_once()

    finally:
        await db.close()


@pytest.mark.asyncio
async def test_postgres_transaction_rollback():
    if not await _check_postgres_reachable():
        pytest.skip(f"PostgreSQL test database not available at {TEST_PG_URL}")

    db = await Database(TEST_PG_URL).connect()
    try:
        repo = Repository(db)
        # Attempting an invalid status raises and triggers rollback
        with pytest.raises(ValueError):
            await repo.set_user_status(99999, "invalid_status")

        # Rollback worked, connection still healthy
        cur = await db.execute("SELECT 1")
        assert (await cur.fetchone())[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_sqlite_to_postgres(tmp_path):
    if not await _check_postgres_reachable():
        pytest.skip(f"PostgreSQL test database not available at {TEST_PG_URL}")

    from scripts.migrate_sqlite_to_postgres import migrate_data

    # Setup a sample SQLite database
    sqlite_file = tmp_path / "sample.sqlite3"
    sqlite_db = await Database(sqlite_file).connect()
    try:
        repo = Repository(sqlite_db)
        await repo.upsert_access_request(987654, 987654, "migrated_user", "Migrated User")
        await repo.set_user_status(987654, UserStatus.APPROVED)
        check_id = await repo.create_check(987654, "KRK", 6)
        await repo.finish_check(
            check_id,
            status=CheckStatus.COMPLETED,
            request_count=1,
            flight_count=10,
            delayed_count=2,
        )
    finally:
        await sqlite_db.close()

    # Reset postgres schema
    import asyncpg
    conn = await asyncpg.connect(TEST_PG_URL)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()

    # Run migration
    await migrate_data(str(sqlite_file), TEST_PG_URL)

    # Verify rows in PostgreSQL
    pg_db = await Database(TEST_PG_URL).connect()
    try:
        pg_repo = Repository(pg_db)
        user = await pg_repo.user(987654)
        assert user is not None
        assert user["display_name"] == "Migrated User"

        checks = await pg_repo.recent_checks(limit=5)
        assert len(checks) == 1
        assert checks[0]["airport"] == "KRK"
        assert checks[0]["id"] == check_id

        # Verify sequence was advanced
        new_check_id = await pg_repo.create_check(987654, "GDN", 6)
        assert new_check_id > check_id
    finally:
        await pg_db.close()
