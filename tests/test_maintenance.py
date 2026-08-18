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

