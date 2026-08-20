from __future__ import annotations

import aiosqlite


MIGRATIONS = [
    """
    CREATE TABLE users (
      telegram_user_id INTEGER PRIMARY KEY,
      chat_id INTEGER NOT NULL,
      username TEXT,
      display_name TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('pending','approved','denied','revoked','blocked')),
      is_admin INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE access_requests (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
      status TEXT NOT NULL CHECK(status IN ('pending','approved','denied')),
      decided_by INTEGER,
      created_at TEXT NOT NULL,
      decided_at TEXT
    );
    CREATE UNIQUE INDEX one_pending_request ON access_requests(telegram_user_id) WHERE status='pending';
    CREATE TABLE checks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      actor_user_id INTEGER NOT NULL,
      airport TEXT NOT NULL,
      window_hours INTEGER NOT NULL,
      status TEXT NOT NULL,
      request_count INTEGER NOT NULL DEFAULT 0,
      flight_count INTEGER NOT NULL DEFAULT 0,
      delayed_count INTEGER NOT NULL DEFAULT 0,
      error TEXT,
      started_at TEXT NOT NULL,
      finished_at TEXT
    );
    CREATE TABLE flight_observations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      check_id INTEGER NOT NULL REFERENCES checks(id),
      flight_id TEXT NOT NULL,
      origin TEXT,
      destination TEXT,
      scheduled_departure TEXT,
      estimated_departure TEXT,
      delay_minutes INTEGER,
      above_threshold INTEGER NOT NULL DEFAULT 0,
      alert_state TEXT NOT NULL DEFAULT 'not_sent',
      observed_at TEXT NOT NULL,
      UNIQUE(check_id, flight_id, scheduled_departure)
    );
    CREATE TABLE api_requests (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      check_id INTEGER REFERENCES checks(id),
      endpoint TEXT NOT NULL,
      status_code INTEGER,
      latency_ms INTEGER,
      retry_number INTEGER NOT NULL DEFAULT 0,
      error TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TABLE monitor_jobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      actor_user_id INTEGER NOT NULL,
      chat_id INTEGER NOT NULL,
      airport TEXT NOT NULL,
      window_hours INTEGER NOT NULL,
      interval_minutes INTEGER NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('active','stopped')),
      started_at TEXT NOT NULL,
      stopped_at TEXT
    );
    CREATE UNIQUE INDEX one_active_monitor ON monitor_jobs(status) WHERE status='active';
    """,
    """
    CREATE TABLE alerts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      check_id INTEGER NOT NULL REFERENCES checks(id),
      recipient_chat_id INTEGER NOT NULL,
      flight_id TEXT NOT NULL,
      scheduled_departure TEXT NOT NULL,
      delay_minutes INTEGER NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('pending','sent','error','duplicate')),
      error TEXT,
      created_at TEXT NOT NULL,
      sent_at TEXT,
      UNIQUE(flight_id, scheduled_departure, recipient_chat_id)
    );
    """,
    """
    CREATE TABLE audit_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      actor_user_id INTEGER,
      action TEXT NOT NULL,
      object_type TEXT,
      object_id TEXT,
      metadata_json TEXT,
      created_at TEXT NOT NULL
    );
    CREATE INDEX audit_events_created_at ON audit_events(created_at);
    """,
    """
    DROP INDEX IF EXISTS one_active_monitor;
    CREATE TABLE monitor_subscriptions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      job_id INTEGER NOT NULL REFERENCES monitor_jobs(id),
      telegram_user_id INTEGER NOT NULL,
      chat_id INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE(job_id, telegram_user_id, chat_id)
    );
    CREATE UNIQUE INDEX active_monitor_airport ON monitor_jobs(airport) WHERE status='active';
    CREATE INDEX monitor_subscriptions_job ON monitor_subscriptions(job_id);
    """,
    """
    CREATE TABLE user_aeroapi_credentials (
      telegram_user_id INTEGER PRIMARY KEY REFERENCES users(telegram_user_id),
      encrypted_api_key TEXT NOT NULL,
      key_suffix TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    """,
    """
    DROP INDEX IF EXISTS active_monitor_airport;
    CREATE UNIQUE INDEX active_monitor_user_airport ON monitor_jobs(actor_user_id, airport) WHERE status='active';
    """,
    """
    ALTER TABLE user_aeroapi_credentials ADD COLUMN needs_reauth INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE user_aeroapi_credentials ADD COLUMN invalid_at TEXT;
    """,
    """
    ALTER TABLE flight_observations ADD COLUMN flightaware_id TEXT;
    """,
    """
    ALTER TABLE flight_observations ADD COLUMN origin_timezone TEXT;
    ALTER TABLE flight_observations ADD COLUMN destination_timezone TEXT;
    """,
    """
    CREATE INDEX api_requests_created_at ON api_requests(created_at);
    CREATE INDEX flight_observations_observed_at ON flight_observations(observed_at);
    CREATE INDEX delayed_flight_observations_latest
      ON flight_observations(flight_id, scheduled_departure, id DESC)
      WHERE above_threshold=1;
    CREATE INDEX alerts_created_at ON alerts(created_at);
    CREATE INDEX stopped_monitor_jobs_stopped_at
      ON monitor_jobs(stopped_at) WHERE status='stopped';
    """,
]


async def migrate(db: aiosqlite.Connection) -> None:
    await db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    await db.commit()
    row = await (await db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")).fetchone()
    current = row[0]
    for version, sql in enumerate(MIGRATIONS, 1):
        if version <= current:
            continue
        await db.execute("BEGIN IMMEDIATE")
        try:
            # sqlite3.executescript() commits before executing its script, so
            # execute each DDL statement inside the migration transaction.
            for statement in (part.strip() for part in sql.split(";") if part.strip()):
                await db.execute(statement)
            await db.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))", (version,))
            await db.commit()
        except Exception:
            await db.rollback()
            raise
