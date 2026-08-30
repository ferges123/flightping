from __future__ import annotations

from ..statuses import AlertStatus
from .base import BaseRepository, serialized_write, utcnow


class AlertRepository(BaseRepository):
    """Delay alerts claimed and delivered to subscriber chats."""

    async def list_alerts(self, airport: str | None = None, limit: int = 50):
        query = """SELECT a.*, c.airport FROM alerts a JOIN checks c ON c.id=a.check_id"""
        args: tuple = ()
        if airport:
            query += " WHERE c.airport=?"
            args = (airport,)
        query += " ORDER BY a.id DESC LIMIT ?"
        return await (await self.db.execute(query, (*args, limit))).fetchall()

    @serialized_write
    async def claim_new_alerts(self, check_id: int, recipient_chat_id: int, flights: list[dict]) -> list[dict]:
        new_flights: list[dict] = []
        for flight in flights:
            cursor = await self.db.execute("""INSERT INTO alerts
                (check_id,recipient_chat_id,flight_id,scheduled_departure,delay_minutes,status,created_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(flight_id, scheduled_departure, recipient_chat_id) DO UPDATE SET
                    check_id=excluded.check_id,
                    delay_minutes=excluded.delay_minutes,
                    status=?,
                    error=NULL,
                    sent_at=NULL
                WHERE alerts.status=?
                   OR (alerts.status=? AND alerts.check_id != excluded.check_id)""",
                (check_id, recipient_chat_id, flight["flight_id"], flight.get("scheduled_departure") or "unknown", flight.get("delay_minutes") or 0, AlertStatus.PENDING, utcnow(), AlertStatus.PENDING, AlertStatus.ERROR, AlertStatus.PENDING))
            if cursor.rowcount == 1:
                new_flights.append(flight)
        await self.db.commit()
        return new_flights

    @serialized_write
    async def finish_alerts(self, check_id: int, recipient_chat_id: int, flights: list[dict], *, sent: bool, error: str | None = None) -> None:
        status = AlertStatus.SENT if sent else AlertStatus.ERROR
        for flight in flights:
            await self.db.execute("""UPDATE alerts SET status=?, error=?, sent_at=?
                WHERE check_id=? AND recipient_chat_id=? AND flight_id=? AND scheduled_departure=? AND status=?""",
                (status, error, utcnow() if sent else None, check_id, recipient_chat_id, flight["flight_id"], flight.get("scheduled_departure") or "unknown", AlertStatus.PENDING))
        await self.db.commit()
