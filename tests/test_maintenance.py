import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from flightpingbot.database import Database
from flightpingbot.maintenance import Maintenance


@pytest.mark.asyncio
async def test_backup_and_retention(tmp_path):
    db = await Database(tmp_path / "source.sqlite3").connect()
    try:
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        await db.execute("INSERT INTO audit_events(action, created_at) VALUES ('old', ?)", (old,))
        await db.execute("INSERT INTO api_requests(endpoint, created_at) VALUES ('old', ?)", (old,))
        await db.commit()
        maintenance = Maintenance(db, observation_days=30, audit_days=30, api_request_days=30, backup_dir=tmp_path / "backups", backup_count=3)
        await maintenance.run_once()
        backup = next((tmp_path / "backups").glob("flightpingbot-*.sqlite3"))
        connection = sqlite3.connect(backup)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM api_requests").fetchone()[0] == 0
        finally:
            connection.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_retention_removes_old_alerts_checks_and_stopped_jobs(tmp_path):
    db = await Database(tmp_path / "source.sqlite3").connect()
    try:
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        await db.execute("INSERT INTO checks(actor_user_id, airport, window_hours, status, started_at, finished_at) VALUES (100, 'WAW', 9, 'completed', ?, ?)", (old, old))
        check_id = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]
        await db.execute("INSERT INTO alerts(check_id, recipient_chat_id, flight_id, scheduled_departure, delay_minutes, status, created_at) VALUES (?, 100, 'W61234', '2026-08-19T14:15:00+00:00', 90, 'sent', ?)", (check_id, old))
        await db.execute("INSERT INTO monitor_jobs(actor_user_id, chat_id, airport, window_hours, interval_minutes, status, started_at, stopped_at) VALUES (100, 100, 'WAW', 9, 30, 'stopped', ?, ?)", (old, old))
        job_id = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]
        await db.execute("INSERT INTO monitor_subscriptions(job_id, telegram_user_id, chat_id, created_at) VALUES (?, 100, 100, ?)", (job_id, old))
        await db.commit()
        maintenance = Maintenance(db, 30, 30, 30, tmp_path / "backups", check_days=30, alert_days=30, monitor_job_days=30)
        await maintenance.run_once()
        for table in ("alerts", "checks", "monitor_subscriptions", "monitor_jobs"):
            row = await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            assert row[0] == 0
    finally:
        await db.close()
