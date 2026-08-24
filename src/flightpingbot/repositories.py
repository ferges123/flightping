from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
import json

from .database import Database
from .credentials import CredentialCipher
from .statuses import UserStatus


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialized_write(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self.db.write_lock:
            return await method(self, *args, **kwargs)
    return wrapped


class Repository:
    def __init__(self, db: Database, credentials_key: str | None = None):
        self.db = db
        self.credentials = CredentialCipher(credentials_key) if credentials_key else None

    @_serialized_write
    async def set_aeroapi_key(self, user_id: int, api_key: str) -> None:
        if not self.credentials:
            raise RuntimeError("AeroAPI credential encryption is not configured")
        if not api_key or not api_key.isascii() or any(character.isspace() for character in api_key):
            raise ValueError("The AeroAPI key must contain only ASCII characters and no spaces.")
        now = utcnow()
        encrypted = self.credentials.encrypt(api_key)
        await self.db.execute("""INSERT INTO user_aeroapi_credentials
            (telegram_user_id, encrypted_api_key, key_suffix, created_at, updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(telegram_user_id) DO UPDATE SET
            encrypted_api_key=?, key_suffix=?, updated_at=?, needs_reauth=0, invalid_at=NULL""",
            (user_id, encrypted, api_key[-4:], now, now, encrypted, api_key[-4:], now))
        await self.db.commit()

    async def aeroapi_key(self, user_id: int) -> str | None:
        if not self.credentials:
            return None
        row = await (await self.db.execute("SELECT encrypted_api_key, needs_reauth FROM user_aeroapi_credentials WHERE telegram_user_id=?", (user_id,))).fetchone()
        if row and row[1]:
            return None
        return self.credentials.decrypt(row[0]) if row else None

    async def aeroapi_key_suffix(self, user_id: int) -> str | None:
        row = await (await self.db.execute("SELECT key_suffix, needs_reauth FROM user_aeroapi_credentials WHERE telegram_user_id=?", (user_id,))).fetchone()
        return f"invalid:{row[0]}" if row and row[1] else (row[0] if row else None)

    @_serialized_write
    async def mark_aeroapi_key_invalid(self, user_id: int) -> None:
        await self.db.execute("UPDATE user_aeroapi_credentials SET needs_reauth=1, invalid_at=?, updated_at=? WHERE telegram_user_id=?", (utcnow(), utcnow(), user_id))
        await self.db.commit()

    @_serialized_write
    async def remove_aeroapi_key(self, user_id: int) -> bool:
        cursor = await self.db.execute("DELETE FROM user_aeroapi_credentials WHERE telegram_user_id=?", (user_id,))
        await self.db.commit()
        return cursor.rowcount == 1

    @_serialized_write
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

    @_serialized_write
    async def update_user_settings(self, user_id: int, *, language: str | None = None, window_hours: int | None = None,
                                   interval_minutes: int | None = None, min_delay_minutes: int | None = None,
                                   duration_hours: int | None = None, reset: bool = False) -> None:
        if language is not None and language not in {"en", "pl"}:
            raise ValueError("Unsupported language")
        if window_hours is not None and window_hours not in {3, 6, 9, 12}:
            raise ValueError("Unsupported check window")
        if interval_minutes is not None and interval_minutes not in {15, 30, 45, 60}:
            raise ValueError("Unsupported monitor interval")
        if min_delay_minutes is not None and min_delay_minutes not in {30, 45, 60, 90, 120}:
            raise ValueError("Unsupported delay threshold")
        if duration_hours is not None and duration_hours not in {3, 6, 12, 24}:
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

    @_serialized_write
    async def upsert_access_request(self, user_id: int, chat_id: int, username: str | None, display_name: str) -> tuple[str, int | None]:
        now = utcnow()
        current = await self.user(user_id)
        if current and current["status"] == UserStatus.BLOCKED:
            return UserStatus.BLOCKED, None
        if current and current["status"] == UserStatus.APPROVED:
            return UserStatus.APPROVED, None
        if current and current["status"] == UserStatus.DENIED:
            recent = await (await self.db.execute("SELECT decided_at FROM access_requests WHERE telegram_user_id=? AND status='denied' ORDER BY decided_at DESC LIMIT 1", (user_id,))).fetchone()
            if recent and recent[0]:
                try:
                    if datetime.now(timezone.utc) - datetime.fromisoformat(recent[0]) < timedelta(hours=24):
                        return "cooldown", None
                except ValueError:
                    pass
        await self.db.execute("""INSERT INTO users(telegram_user_id,chat_id,username,display_name,status,is_admin,created_at,updated_at)
            VALUES(?,?,?,?,?,0,?,?) ON CONFLICT(telegram_user_id) DO UPDATE SET chat_id=?, username=?, display_name=?, updated_at=?""",
            (user_id, chat_id, username, display_name, UserStatus.PENDING, now, now, chat_id, username, display_name, now))
        pending = await (await self.db.execute("SELECT id FROM access_requests WHERE telegram_user_id=? AND status='pending'", (user_id,))).fetchone()
        if pending:
            await self.db.commit()
            return UserStatus.PENDING, pending[0]
        await self.db.execute("INSERT INTO access_requests(telegram_user_id,status,created_at) VALUES (?, 'pending', ?)", (user_id, now))
        request_id = (await (await self.db.execute("SELECT last_insert_rowid()")).fetchone())[0]
        await self.db.commit()
        return "requested", request_id

    @_serialized_write
    async def decide_request(self, request_id: int, admin_id: int | None, approve: bool) -> tuple[bool, int | None]:
        now = utcnow()
        status = UserStatus.APPROVED if approve else UserStatus.DENIED
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.execute("SELECT telegram_user_id FROM access_requests WHERE id=? AND status='pending'", (request_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if not row:
                await self.db.rollback()
                return False, None
            user_id = row[0]
            cursor = await self.db.execute("UPDATE access_requests SET status=?, decided_by=?, decided_at=? WHERE id=? AND status='pending'", (status, admin_id, now, request_id))
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
        return await (await self.db.execute("SELECT id FROM access_requests WHERE telegram_user_id=? AND status='pending'", (user_id,))).fetchone()

    async def pending_request_ids(self) -> dict[int, int]:
        """Pending access request id per user, in one query for the users page."""
        rows = await (await self.db.execute(
            "SELECT telegram_user_id, MIN(id) FROM access_requests WHERE status='pending' GROUP BY telegram_user_id"
        )).fetchall()
        return {row[0]: row[1] for row in rows}

    @_serialized_write
    async def create_check(self, actor: int, airport: str, window_hours: int) -> int:
        now = utcnow()
        cursor = await self.db.execute("INSERT INTO checks(actor_user_id,airport,window_hours,status,started_at) VALUES(?,?,?,'running',?)", (actor, airport, window_hours, now))
        await self.db.commit()
        return cursor.lastrowid

    @_serialized_write
    async def record_api_request(self, check_id: int, endpoint: str, status_code: int | None, latency_ms: int, retry_number: int = 0, error: str | None = None) -> None:
        await self.db.execute("INSERT INTO api_requests(check_id,endpoint,status_code,latency_ms,retry_number,error,created_at) VALUES(?,?,?,?,?,?,?)", (check_id, endpoint, status_code, latency_ms, retry_number, error, utcnow()))
        await self.db.commit()

    @_serialized_write
    async def record_observation(self, check_id: int, flight: dict, threshold: int) -> None:
        await self.db.execute("""INSERT OR IGNORE INTO flight_observations
            (check_id,flight_id,origin,destination,scheduled_departure,estimated_departure,delay_minutes,above_threshold,observed_at,flightaware_id,origin_timezone,destination_timezone)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (check_id, flight["flight_id"], flight.get("origin"), flight.get("destination"), flight.get("scheduled_departure"), flight.get("estimated_departure"), flight.get("delay_minutes"), int((flight.get("delay_minutes") or 0) >= threshold), utcnow(), flight.get("flightaware_id"), flight.get("origin_timezone"), flight.get("destination_timezone")))
        await self.db.commit()

    @_serialized_write
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

    @_serialized_write
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
            "SELECT * FROM checks WHERE status='completed' ORDER BY id DESC LIMIT ?",
            (limit,),
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

    async def usage(self, since: str, actor_user_id: int | None = None):
        query = """SELECT COALESCE(SUM(1 + a.retry_number), 0) AS total, SUM(a.status_code BETWEEN 200 AND 299) AS success,
            SUM(a.status_code IS NULL OR a.status_code >= 400) AS errors, SUM(a.retry_number) AS retries
            FROM api_requests a LEFT JOIN checks c ON c.id=a.check_id WHERE a.created_at>=?"""
        args: list = [since]
        if actor_user_id is not None:
            query += " AND c.actor_user_id=?"
            args.append(actor_user_id)
        return await (await self.db.execute(query, args)).fetchone()

    async def usage_by_user(self, since: str):
        return await (await self.db.execute("""SELECT c.actor_user_id AS user_id,
            CASE WHEN NULLIF(u.username, '') IS NOT NULL THEN '@' || u.username
                 ELSE COALESCE(NULLIF(u.display_name, ''), CAST(c.actor_user_id AS TEXT)) END AS user_name,
            COALESCE(SUM(1 + a.retry_number), 0) AS total, SUM(a.status_code BETWEEN 200 AND 299) AS success,
            SUM(a.status_code IS NULL OR a.status_code >= 400) AS errors, SUM(a.retry_number) AS retries
            FROM api_requests a JOIN checks c ON c.id=a.check_id LEFT JOIN users u ON u.telegram_user_id=c.actor_user_id
            WHERE a.created_at>=? GROUP BY c.actor_user_id ORDER BY total DESC""", (since,))).fetchall()

    async def api_request_count(self, since: str, actor_user_id: int | None = None) -> int:
        query = "SELECT COALESCE(SUM(1 + a.retry_number), 0) FROM api_requests a LEFT JOIN checks c ON c.id=a.check_id WHERE a.created_at>=?"
        args: list = [since]
        if actor_user_id is not None:
            query += " AND c.actor_user_id=?"
            args.append(actor_user_id)
        row = await (await self.db.execute(query, args)).fetchone()
        return row[0]

    @_serialized_write
    async def audit(self, actor_user_id: int | None, action: str, object_type: str | None = None, object_id: str | None = None, metadata: dict | None = None) -> None:
        await self.db.execute("""INSERT INTO audit_events(actor_user_id,action,object_type,object_id,metadata_json,created_at)
            VALUES(?,?,?,?,?,?)""", (actor_user_id, action, object_type, object_id, json.dumps(metadata or {}, sort_keys=True), utcnow()))
        await self.db.commit()

    async def list_alerts(self, airport: str | None = None, limit: int = 50):
        query = """SELECT a.*, c.airport FROM alerts a JOIN checks c ON c.id=a.check_id"""
        args: tuple = ()
        if airport:
            query += " WHERE c.airport=?"
            args = (airport,)
        query += " ORDER BY a.id DESC LIMIT ?"
        return await (await self.db.execute(query, (*args, limit))).fetchall()

    async def list_audit(self, days: int = 7, limit: int = 100):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return await (await self.db.execute("SELECT * FROM audit_events WHERE created_at >= ? ORDER BY id DESC LIMIT ?", (cutoff, limit))).fetchall()

    async def user_counts(self):
        rows = await (await self.db.execute("SELECT status, COUNT(*) AS count FROM users GROUP BY status")).fetchall()
        return {row["status"]: row["count"] for row in rows}

    async def check_observations(self, check_id: int):
        return await (await self.db.execute("SELECT * FROM flight_observations WHERE check_id=? ORDER BY id", (check_id,))).fetchall()

    @_serialized_write
    async def set_user_status(self, user_id: int, status: str) -> bool:
        if status not in {UserStatus.APPROVED, UserStatus.REVOKED, UserStatus.BLOCKED}:
            raise ValueError("invalid user status")
        cursor = await self.db.execute("UPDATE users SET status=?, updated_at=? WHERE telegram_user_id=? AND is_admin=0", (status, utcnow(), user_id))
        await self.db.commit()
        return cursor.rowcount == 1

    async def active_monitor_jobs(self):
        return await (await self.db.execute("""SELECT j.*, s.telegram_user_id, s.chat_id AS subscription_chat_id
            FROM monitor_jobs j JOIN monitor_subscriptions s ON s.job_id=j.id
            WHERE j.status='active' ORDER BY j.id""")).fetchall()

    async def monitor_jobs(self, limit: int | None = None, offset: int = 0):
        """Return active and finished monitoring jobs for the admin panel."""
        query = """SELECT * FROM monitor_jobs
            ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                     COALESCE(stopped_at, started_at) DESC, id DESC"""
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            return await (await self.db.execute(query, (limit, offset))).fetchall()
        return await (await self.db.execute(query)).fetchall()

    async def monitor_job(self, job_id: int):
        return await (await self.db.execute(
            "SELECT * FROM monitor_jobs WHERE id=?", (job_id,)
        )).fetchone()

    @_serialized_write
    async def recover_monitors_after_restart(self) -> None:
        """Stop persisted monitor rows whose owners no longer have access."""
        await self.db.execute("""UPDATE monitor_jobs
            SET status='stopped', stopped_at=?
            WHERE status='active' AND NOT EXISTS (
                SELECT 1 FROM users
                WHERE users.telegram_user_id=monitor_jobs.actor_user_id
                  AND users.status='approved'
            )""", (utcnow(),))
        await self.db.commit()

    @_serialized_write
    async def create_monitor_job(self, actor_user_id: int, chat_id: int, airport: str, window_hours: int, interval_minutes: int,
                                 max_active_airports: int = 3, min_delay_minutes: int | None = None,
                                 duration_hours: int | None = None) -> tuple[int, bool]:
        existing = await (await self.db.execute("SELECT id FROM monitor_jobs WHERE status='active' AND actor_user_id=? AND airport=? LIMIT 1", (actor_user_id, airport))).fetchone()
        if existing:
            cursor = await self.db.execute("INSERT OR IGNORE INTO monitor_subscriptions(job_id,telegram_user_id,chat_id,created_at) VALUES(?,?,?,?)", (existing[0], actor_user_id, chat_id, utcnow()))
            await self.db.commit()
            return existing[0], cursor.rowcount == 1
        active = await (await self.db.execute("SELECT COUNT(*) FROM monitor_jobs WHERE status='active' AND actor_user_id=?", (actor_user_id,))).fetchone()
        if active[0] >= max_active_airports:
            raise RuntimeError(f"The maximum of {max_active_airports} active airports has been reached.")
        cursor = await self.db.execute("""INSERT INTO monitor_jobs
            (actor_user_id,chat_id,airport,window_hours,interval_minutes,min_delay_minutes,duration_hours,status,started_at)
            VALUES(?,?,?,?,?,?,?,'active',?)""", (actor_user_id, chat_id, airport, window_hours, interval_minutes, min_delay_minutes, duration_hours, utcnow()))
        job_id = cursor.lastrowid
        await self.db.execute("INSERT INTO monitor_subscriptions(job_id,telegram_user_id,chat_id,created_at) VALUES(?,?,?,?)", (job_id, actor_user_id, chat_id, utcnow()))
        await self.db.commit()
        return job_id, True

    @_serialized_write
    async def remove_subscription(self, job_id: int, user_id: int, chat_id: int) -> bool:
        cursor = await self.db.execute("DELETE FROM monitor_subscriptions WHERE job_id=? AND telegram_user_id=? AND chat_id=?", (job_id, user_id, chat_id))
        await self.db.commit()
        return cursor.rowcount == 1

    async def job_subscriptions(self, job_id: int):
        return await (await self.db.execute("SELECT * FROM monitor_subscriptions WHERE job_id=?", (job_id,))).fetchall()

    async def user_monitor_jobs(self, user_id: int, chat_id: int):
        return await (await self.db.execute("SELECT j.* FROM monitor_jobs j JOIN monitor_subscriptions s ON s.job_id=j.id WHERE s.telegram_user_id=? AND s.chat_id=? AND j.status='active'", (user_id, chat_id))).fetchall()

    @_serialized_write
    async def stop_monitor_job(self, job_id: int | None) -> None:
        if job_id is None:
            return
        await self.db.execute("UPDATE monitor_jobs SET status='stopped', stopped_at=? WHERE id=? AND status='active'", (utcnow(), job_id))
        await self.db.commit()

    @_serialized_write
    async def claim_new_alerts(self, check_id: int, recipient_chat_id: int, flights: list[dict]) -> list[dict]:
        new_flights: list[dict] = []
        for flight in flights:
            cursor = await self.db.execute("""INSERT INTO alerts
                (check_id,recipient_chat_id,flight_id,scheduled_departure,delay_minutes,status,created_at)
                VALUES(?,?,?,?,?,'pending',?)
                ON CONFLICT(flight_id, scheduled_departure, recipient_chat_id) DO UPDATE SET
                    check_id=excluded.check_id,
                    delay_minutes=excluded.delay_minutes,
                    status='pending',
                    error=NULL,
                    sent_at=NULL
                WHERE alerts.status='error'
                   OR (alerts.status='pending' AND alerts.check_id != excluded.check_id)""",
                (check_id, recipient_chat_id, flight["flight_id"], flight.get("scheduled_departure") or "unknown", flight.get("delay_minutes") or 0, utcnow()))
            if cursor.rowcount == 1:
                new_flights.append(flight)
        await self.db.commit()
        return new_flights

    @_serialized_write
    async def finish_alerts(self, check_id: int, recipient_chat_id: int, flights: list[dict], *, sent: bool, error: str | None = None) -> None:
        status = "sent" if sent else "error"
        for flight in flights:
            await self.db.execute("""UPDATE alerts SET status=?, error=?, sent_at=?
                WHERE check_id=? AND recipient_chat_id=? AND flight_id=? AND scheduled_departure=? AND status='pending'""", (status, error, utcnow() if sent else None, check_id, recipient_chat_id, flight["flight_id"], flight.get("scheduled_departure") or "unknown"))
        await self.db.commit()
