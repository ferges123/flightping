from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from .database import Database

log = logging.getLogger(__name__)

RETENTION_SQL = {
    "flight_observations": "DELETE FROM flight_observations WHERE observed_at < ?",
    "audit_events": "DELETE FROM audit_events WHERE created_at < ?",
    "api_requests": "DELETE FROM api_requests WHERE created_at < ?",
}


class Maintenance:
    def __init__(self, db: Database, observation_days: int, audit_days: int, api_request_days: int, backup_dir: Path, backup_count: int = 3):
        self.db = db
        self.observation_days = observation_days
        self.audit_days = audit_days
        self.api_request_days = api_request_days
        self.backup_dir = backup_dir
        self.backup_count = backup_count
        self.wakeup = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.stopping = False

    async def start(self) -> None:
        self.stopping = False
        self.task = asyncio.create_task(self._run(), name="flightping-maintenance")

    async def stop(self) -> None:
        if not self.task:
            return
        self.stopping = True
        self.wakeup.set()
        await self.task
        self.task = None

    async def _run(self) -> None:
        while True:
            delay = self._seconds_until_next_run()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=delay)
                self.wakeup.clear()
                if self.stopping:
                    return
            except asyncio.TimeoutError:
                try:
                    await self.run_once()
                except Exception:
                    log.exception("maintenance run failed")

    @staticmethod
    def _seconds_until_next_run() -> float:
        now = datetime.now(timezone.utc)
        next_run = (now + timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0)
        if now.hour < 3:
            next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
        return max(1.0, (next_run - now).total_seconds())

    async def run_once(self) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        await self._retain_data()
        await self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        await self.db.commit()
        backup_path = self.backup_dir / f"flightpingbot-{datetime.now(timezone.utc):%Y%m%d}.sqlite3"
        if backup_path.exists():
            backup_path.unlink()
        target = await aiosqlite.connect(backup_path)
        try:
            await self.db.conn.backup(target)
        finally:
            await target.close()
        backup_path.chmod(0o600)
        await self._retain_backups()

    async def _retain_backups(self) -> None:
        backups = sorted(self.backup_dir.glob("flightpingbot-*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
        for old in backups[self.backup_count:]:
            old.unlink()

    async def _retain_data(self) -> None:
        now = datetime.now(timezone.utc)
        cutoffs = {
            "flight_observations": now - timedelta(days=self.observation_days),
            "audit_events": now - timedelta(days=self.audit_days),
            "api_requests": now - timedelta(days=self.api_request_days),
        }
        for table, cutoff in cutoffs.items():
            await self.db.execute(RETENTION_SQL[table], (cutoff.isoformat(),))
        await self.db.commit()
