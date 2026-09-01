from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..statuses import AccessRequestStatus, UserStatus
from ..preference_options import DURATION_HOURS, INTERVAL_MINUTES, LANGUAGES, MIN_DELAY_MINUTES, WINDOW_HOURS
from .base import BaseRepository, serialized_write, utcnow


class UserRepository(BaseRepository):
    """Users, access requests and per-user preferences."""

    @serialized_write
    async def sync_admins(self, admin_ids: frozenset[int]) -> None:
        now = utcnow()
        rows = await (await self.db.execute("SELECT telegram_user_id, is_admin FROM users WHERE is_admin=1")).fetchall()
        existing = {row[0] for row in rows}
        for user_id in existing - admin_ids:
            await self.db.execute("UPDATE users SET is_admin=0, updated_at=? WHERE telegram_user_id=?", (now, user_id))
        for user_id in admin_ids:
            await self.db.execute("""INSERT INTO users(telegram_user_id,chat_id,username,display_name,status,is_admin,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(telegram_user_id) DO UPDATE SET is_admin=1, updated_at=?""",
                (user_id, user_id, None, "Administrator", UserStatus.APPROVED, 1, now, now, now))
        await self.db.commit()

    async def user(self, user_id: int):
        return await (await self.db.execute("SELECT * FROM users WHERE telegram_user_id=?", (user_id,))).fetchone()

    async def user_settings(self, user_id: int):
        return await (await self.db.execute("SELECT * FROM user_settings WHERE telegram_user_id=?", (user_id,))).fetchone()

    @serialized_write
    async def update_user_settings(self, user_id: int, *, language: str | None = None, window_hours: int | None = None,
                                   interval_minutes: int | None = None, min_delay_minutes: int | None = None,
                                   duration_hours: int | None = None, reset: bool = False) -> None:
        if language is not None and language not in LANGUAGES:
            raise ValueError("Unsupported language")
        if window_hours is not None and window_hours not in WINDOW_HOURS:
            raise ValueError("Unsupported check window")
        if interval_minutes is not None and interval_minutes not in INTERVAL_MINUTES:
            raise ValueError("Unsupported monitor interval")
        if min_delay_minutes is not None and min_delay_minutes not in MIN_DELAY_MINUTES:
            raise ValueError("Unsupported delay threshold")
        if duration_hours is not None and duration_hours not in DURATION_HOURS:
            raise ValueError("Unsupported monitoring duration")
        if reset:
            await self.db.execute("DELETE FROM user_settings WHERE telegram_user_id=?", (user_id,))
        else:
            for column, value in (
                ("language", language), ("window_hours", window_hours), ("interval_minutes", interval_minutes),
                ("min_delay_minutes", min_delay_minutes), ("duration_hours", duration_hours),
            ):
                if value is not None:
                    await self.db.execute(f"""INSERT INTO user_settings(telegram_user_id,{column}) VALUES(?,?)
                        ON CONFLICT(telegram_user_id) DO UPDATE SET {column}=excluded.{column}""", (user_id, value))
        await self.db.commit()

    @serialized_write
    async def upsert_access_request(self, user_id: int, chat_id: int, username: str | None, display_name: str) -> tuple[str, int | None]:
        now = utcnow()
        current = await self.user(user_id)
        if current and current["status"] == UserStatus.BLOCKED:
            return UserStatus.BLOCKED, None
        if current and current["status"] == UserStatus.APPROVED:
            return UserStatus.APPROVED, None
        if current and current["status"] == UserStatus.DENIED:
            recent = await (await self.db.execute("SELECT decided_at FROM access_requests WHERE telegram_user_id=? AND status=? ORDER BY decided_at DESC LIMIT 1", (user_id, AccessRequestStatus.DENIED))).fetchone()
            if recent and recent[0]:
                try:
                    if datetime.now(timezone.utc) - datetime.fromisoformat(recent[0]) < timedelta(hours=24):
                        return "cooldown", None
                except ValueError:
                    pass
        await self.db.execute("""INSERT INTO users(telegram_user_id,chat_id,username,display_name,status,is_admin,created_at,updated_at)
            VALUES(?,?,?,?,?,0,?,?) ON CONFLICT(telegram_user_id) DO UPDATE SET chat_id=?, username=?, display_name=?, updated_at=?""",
            (user_id, chat_id, username, display_name, UserStatus.PENDING, now, now, chat_id, username, display_name, now))
        pending = await (await self.db.execute("SELECT id FROM access_requests WHERE telegram_user_id=? AND status=?", (user_id, AccessRequestStatus.PENDING))).fetchone()
        if pending:
            await self.db.commit()
            return UserStatus.PENDING, pending[0]
        await self.db.execute("INSERT INTO access_requests(telegram_user_id,status,created_at) VALUES (?,?,?)", (user_id, AccessRequestStatus.PENDING, now))
        request_id = (await (await self.db.execute("SELECT last_insert_rowid()")).fetchone())[0]
        await self.db.commit()
        return "requested", request_id

    @serialized_write
    async def decide_request(self, request_id: int, admin_id: int | None, approve: bool) -> tuple[bool, int | None]:
        now = utcnow()
        status = UserStatus.APPROVED if approve else UserStatus.DENIED
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.execute("SELECT telegram_user_id FROM access_requests WHERE id=? AND status=?", (request_id, AccessRequestStatus.PENDING))
            row = await cursor.fetchone()
            await cursor.close()
            if not row:
                await self.db.rollback()
                return False, None
            user_id = row[0]
            cursor = await self.db.execute("UPDATE access_requests SET status=?, decided_by=?, decided_at=? WHERE id=? AND status=?", (status, admin_id, now, request_id, AccessRequestStatus.PENDING))
            changed = cursor.rowcount == 1
            await cursor.close()
            if not changed:
                await self.db.rollback()
                return False, None
            await self.db.execute("UPDATE users SET status=?, updated_at=? WHERE telegram_user_id=?", (status, now, user_id))
            await self.db.commit()
            return True, user_id
        except Exception:
            await self.db.rollback()
            raise

    async def list_users(self, status: str | None = None):
        query, args = "SELECT * FROM users", ()
        if status:
            query += " WHERE status=?"
            args = (status,)
        query += " ORDER BY created_at"
        return await (await self.db.execute(query, args)).fetchall()

    async def pending_request_for_user(self, user_id: int):
        return await (await self.db.execute("SELECT id FROM access_requests WHERE telegram_user_id=? AND status=?", (user_id, AccessRequestStatus.PENDING))).fetchone()

    async def pending_request_ids(self) -> dict[int, int]:
        """Pending access request id per user, in one query for the users page."""
        rows = await (await self.db.execute(
            "SELECT telegram_user_id, MIN(id) FROM access_requests WHERE status=? GROUP BY telegram_user_id", (AccessRequestStatus.PENDING,)
        )).fetchall()
        return {row[0]: row[1] for row in rows}

    async def user_counts(self):
        rows = await (await self.db.execute("SELECT status, COUNT(*) AS count FROM users GROUP BY status")).fetchall()
        return {row["status"]: row["count"] for row in rows}

    @serialized_write
    async def set_user_status(self, user_id: int, status: str) -> bool:
        if status not in {UserStatus.APPROVED, UserStatus.REVOKED, UserStatus.BLOCKED}:
            raise ValueError("invalid user status")
        cursor = await self.db.execute("UPDATE users SET status=?, updated_at=? WHERE telegram_user_id=? AND is_admin=0", (status, utcnow(), user_id))
        await self.db.commit()
        return cursor.rowcount == 1
