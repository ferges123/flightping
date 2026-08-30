from __future__ import annotations

from ..statuses import MonitorJobStatus, UserStatus
from .base import BaseRepository, serialized_write, utcnow


class MonitorJobRepository(BaseRepository):
    """Monitoring jobs and their per-chat subscriptions."""

    async def active_monitor_jobs(self):
        return await (await self.db.execute("""SELECT j.*, s.telegram_user_id, s.chat_id AS subscription_chat_id
            FROM monitor_jobs j JOIN monitor_subscriptions s ON s.job_id=j.id
            WHERE j.status=? ORDER BY j.id""", (MonitorJobStatus.ACTIVE,))).fetchall()

    async def monitor_jobs(self, limit: int | None = None, offset: int = 0):
        """Return active and finished monitoring jobs for the admin panel."""
        query = """SELECT * FROM monitor_jobs
            ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END,
                     COALESCE(stopped_at, started_at) DESC, id DESC"""
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            return await (await self.db.execute(query, (MonitorJobStatus.ACTIVE, limit, offset))).fetchall()
        return await (await self.db.execute(query, (MonitorJobStatus.ACTIVE,))).fetchall()

    async def monitor_job(self, job_id: int):
        return await (await self.db.execute(
            "SELECT * FROM monitor_jobs WHERE id=?", (job_id,)
        )).fetchone()

    @serialized_write
    async def recover_monitors_after_restart(self) -> None:
        """Stop persisted monitor rows whose owners no longer have access."""
        await self.db.execute("""UPDATE monitor_jobs
            SET status=?, stopped_at=?
            WHERE status=? AND NOT EXISTS (
                SELECT 1 FROM users
                WHERE users.telegram_user_id=monitor_jobs.actor_user_id
                  AND users.status=?
            )""", (MonitorJobStatus.STOPPED, utcnow(), MonitorJobStatus.ACTIVE, UserStatus.APPROVED))
        await self.db.commit()

    @serialized_write
    async def create_monitor_job(self, actor_user_id: int, chat_id: int, airport: str, window_hours: int, interval_minutes: int,
                                 max_active_airports: int = 3, min_delay_minutes: int | None = None,
                                 duration_hours: int | None = None) -> tuple[int, bool]:
        existing = await (await self.db.execute("SELECT id FROM monitor_jobs WHERE status=? AND actor_user_id=? AND airport=? LIMIT 1", (MonitorJobStatus.ACTIVE, actor_user_id, airport))).fetchone()
        if existing:
            cursor = await self.db.execute("INSERT OR IGNORE INTO monitor_subscriptions(job_id,telegram_user_id,chat_id,created_at) VALUES(?,?,?,?)", (existing[0], actor_user_id, chat_id, utcnow()))
            await self.db.commit()
            return existing[0], cursor.rowcount == 1
        active = await (await self.db.execute("SELECT COUNT(*) FROM monitor_jobs WHERE status=? AND actor_user_id=?", (MonitorJobStatus.ACTIVE, actor_user_id))).fetchone()
        if active[0] >= max_active_airports:
            raise RuntimeError(f"The maximum of {max_active_airports} active airports has been reached.")
        cursor = await self.db.execute("""INSERT INTO monitor_jobs
            (actor_user_id,chat_id,airport,window_hours,interval_minutes,min_delay_minutes,duration_hours,status,started_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (actor_user_id, chat_id, airport, window_hours, interval_minutes, min_delay_minutes, duration_hours, MonitorJobStatus.ACTIVE, utcnow()))
        job_id = cursor.lastrowid
        await self.db.execute("INSERT INTO monitor_subscriptions(job_id,telegram_user_id,chat_id,created_at) VALUES(?,?,?,?)", (job_id, actor_user_id, chat_id, utcnow()))
        await self.db.commit()
        return job_id, True

    @serialized_write
    async def remove_subscription(self, job_id: int, user_id: int, chat_id: int) -> bool:
        cursor = await self.db.execute("DELETE FROM monitor_subscriptions WHERE job_id=? AND telegram_user_id=? AND chat_id=?", (job_id, user_id, chat_id))
        await self.db.commit()
        return cursor.rowcount == 1

    async def job_subscriptions(self, job_id: int):
        return await (await self.db.execute("SELECT * FROM monitor_subscriptions WHERE job_id=?", (job_id,))).fetchall()

    async def user_monitor_jobs(self, user_id: int, chat_id: int):
        return await (await self.db.execute("SELECT j.* FROM monitor_jobs j JOIN monitor_subscriptions s ON s.job_id=j.id WHERE s.telegram_user_id=? AND s.chat_id=? AND j.status=?", (user_id, chat_id, MonitorJobStatus.ACTIVE))).fetchall()

    @serialized_write
    async def stop_monitor_job(self, job_id: int | None) -> None:
        if job_id is None:
            return
        await self.db.execute("UPDATE monitor_jobs SET status=?, stopped_at=? WHERE id=? AND status=?", (MonitorJobStatus.STOPPED, utcnow(), job_id, MonitorJobStatus.ACTIVE))
        await self.db.commit()
