from __future__ import annotations

from ..statuses import CheckStatus
from .base import BaseRepository, serialized_write, utcnow


class CheckRepository(BaseRepository):
    """Flight checks, their observations and AeroAPI request accounting."""

    @serialized_write
    async def create_check(self, actor: int, airport: str, window_hours: int) -> int:
        now = utcnow()
        cursor = await self.db.execute("INSERT INTO checks(actor_user_id,airport,window_hours,status,started_at) VALUES(?,?,?,?,?)", (actor, airport, window_hours, CheckStatus.RUNNING, now))
        await self.db.commit()
        return cursor.lastrowid

    @serialized_write
    async def record_api_request(self, check_id: int | None, endpoint: str, status_code: int | None, latency_ms: int, retry_number: int = 0, error: str | None = None, *, actor_user_id: int | None = None) -> None:
        await self.db.execute("INSERT INTO api_requests(check_id,actor_user_id,endpoint,status_code,latency_ms,retry_number,error,created_at) VALUES(?,?,?,?,?,?,?,?)", (check_id, actor_user_id, endpoint, status_code, latency_ms, retry_number, error, utcnow()))
        await self.db.commit()

    @serialized_write
    async def record_observation(self, check_id: int, flight: dict, threshold: int) -> None:
        await self.db.execute("""INSERT OR IGNORE INTO flight_observations
            (check_id,flight_id,origin,destination,scheduled_departure,estimated_departure,delay_minutes,above_threshold,observed_at,flightaware_id,origin_timezone,destination_timezone)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (check_id, flight["flight_id"], flight.get("origin"), flight.get("destination"), flight.get("scheduled_departure"), flight.get("estimated_departure"), flight.get("delay_minutes"), int((flight.get("delay_minutes") or 0) >= threshold), utcnow(), flight.get("flightaware_id"), flight.get("origin_timezone"), flight.get("destination_timezone")))
        await self.db.commit()

    @serialized_write
    async def record_observations(self, check_id: int, flights: list[dict], threshold: int) -> None:
        if not flights:
            return
        observed_at = utcnow()
        await self.db.executemany("""INSERT OR IGNORE INTO flight_observations
            (check_id,flight_id,origin,destination,scheduled_departure,estimated_departure,delay_minutes,above_threshold,observed_at,flightaware_id,origin_timezone,destination_timezone)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", [
                (check_id, flight["flight_id"], flight.get("origin"), flight.get("destination"), flight.get("scheduled_departure"), flight.get("estimated_departure"), flight.get("delay_minutes"), int((flight.get("delay_minutes") or 0) >= threshold), observed_at, flight.get("flightaware_id"), flight.get("origin_timezone"), flight.get("destination_timezone"))
                for flight in flights
            ])
        await self.db.commit()

    @serialized_write
    async def finish_check(self, check_id: int, *, status: str, request_count: int, flight_count: int, delayed_count: int, error: str | None = None) -> None:
        await self.db.execute("UPDATE checks SET status=?,request_count=?,flight_count=?,delayed_count=?,error=?,finished_at=? WHERE id=?", (status, request_count, flight_count, delayed_count, error, utcnow(), check_id))
        await self.db.commit()

    async def recent_checks(self, airport: str | None = None, limit: int = 20, offset: int = 0):
        if airport:
            return await (await self.db.execute(
                "SELECT * FROM checks WHERE airport=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (airport, limit, offset),
            )).fetchall()
        return await (await self.db.execute(
            "SELECT * FROM checks ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        )).fetchall()

    async def successful_checks(self, limit: int = 100):
        return await (await self.db.execute(
            "SELECT * FROM checks WHERE status=? ORDER BY id DESC LIMIT ?",
            (CheckStatus.COMPLETED, limit),
        )).fetchall()

    async def delayed_observations(self, limit: int = 100):
        return await (await self.db.execute(
            """SELECT o.*, c.airport FROM flight_observations o
               JOIN (
                   SELECT MAX(id) AS id FROM flight_observations
                   WHERE above_threshold=1
                   GROUP BY flight_id, scheduled_departure
               ) latest ON latest.id=o.id
               JOIN checks c ON c.id=o.check_id
               ORDER BY o.id DESC LIMIT ?""",
            (limit,),
        )).fetchall()

    async def check_observations(self, check_id: int):
        return await (await self.db.execute("SELECT * FROM flight_observations WHERE check_id=? ORDER BY id", (check_id,))).fetchall()

    async def usage(self, since: str, actor_user_id: int | None = None):
        query = """SELECT COALESCE(SUM(1 + a.retry_number), 0) AS total, SUM(a.status_code BETWEEN 200 AND 299) AS success,
            SUM(a.status_code IS NULL OR a.status_code >= 400) AS errors, SUM(a.retry_number) AS retries
            FROM api_requests a LEFT JOIN checks c ON c.id=a.check_id WHERE a.created_at>=?"""
        args: list = [since]
        if actor_user_id is not None:
            query += " AND COALESCE(a.actor_user_id, c.actor_user_id)=?"
            args.append(actor_user_id)
        return await (await self.db.execute(query, args)).fetchone()

    async def usage_by_user(self, since: str):
        return await (await self.db.execute("""SELECT COALESCE(a.actor_user_id, c.actor_user_id) AS user_id,
            CASE WHEN NULLIF(u.username, '') IS NOT NULL THEN '@' || u.username
                 ELSE COALESCE(NULLIF(u.display_name, ''), CAST(COALESCE(a.actor_user_id, c.actor_user_id) AS TEXT)) END AS user_name,
            COALESCE(SUM(1 + a.retry_number), 0) AS total, SUM(a.status_code BETWEEN 200 AND 299) AS success,
            SUM(a.status_code IS NULL OR a.status_code >= 400) AS errors, SUM(a.retry_number) AS retries
            FROM api_requests a LEFT JOIN checks c ON c.id=a.check_id LEFT JOIN users u ON u.telegram_user_id=COALESCE(a.actor_user_id, c.actor_user_id)
            WHERE a.created_at>=? GROUP BY COALESCE(a.actor_user_id, c.actor_user_id) ORDER BY total DESC""", (since,))).fetchall()

    async def api_request_count(self, since: str, actor_user_id: int | None = None) -> int:
        query = "SELECT COALESCE(SUM(1 + a.retry_number), 0) FROM api_requests a LEFT JOIN checks c ON c.id=a.check_id WHERE a.created_at>=?"
        args: list = [since]
        if actor_user_id is not None:
            query += " AND COALESCE(a.actor_user_id, c.actor_user_id)=?"
            args.append(actor_user_id)
        row = await (await self.db.execute(query, args)).fetchone()
        return row[0]
