from __future__ import annotations

from .base import BaseRepository, serialized_write, utcnow


MAX_FAVORITE_AIRPORTS = 8


class FavoriteAirportRepository(BaseRepository):
    """Per-user shortcuts for frequently checked airports."""

    async def favorite_airports(self, user_id: int):
        return await (await self.db.execute(
            "SELECT airport FROM user_favorite_airports WHERE telegram_user_id=? ORDER BY created_at, airport",
            (user_id,),
        )).fetchall()

    @serialized_write
    async def add_favorite_airport(self, user_id: int, airport: str) -> str:
        airport = airport.strip().upper()
        if len(airport) != 3 or not airport.isascii() or not airport.isalpha():
            raise ValueError("The airport must be a three-letter IATA code.")
        exists = await (await self.db.execute(
            "SELECT 1 FROM user_favorite_airports WHERE telegram_user_id=? AND airport=?",
            (user_id, airport),
        )).fetchone()
        if exists:
            return "exists"
        count = await (await self.db.execute(
            "SELECT COUNT(*) FROM user_favorite_airports WHERE telegram_user_id=?", (user_id,)
        )).fetchone()
        if count[0] >= MAX_FAVORITE_AIRPORTS:
            raise ValueError(f"You can save up to {MAX_FAVORITE_AIRPORTS} favorite airports.")
        await self.db.execute(
            "INSERT INTO user_favorite_airports(telegram_user_id,airport,created_at) VALUES(?,?,?)",
            (user_id, airport, utcnow()),
        )
        await self.db.commit()
        return "added"

    @serialized_write
    async def remove_favorite_airport(self, user_id: int, airport: str) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM user_favorite_airports WHERE telegram_user_id=? AND airport=?",
            (user_id, airport.strip().upper()),
        )
        await self.db.commit()
        return cursor.rowcount == 1
