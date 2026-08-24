from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from datetime import datetime, timedelta, timezone
from html import escape
import json

from ..auth import Auth
from ..repositories import Repository
from ..keyboards import main_keyboard
from ..i18n import t
from ..i18n import button_texts
from ..statuses import UserStatus


def _audit_actor_label(row) -> str:
    """Render anonymous web-panel actions without impersonating an admin."""
    if row["actor_user_id"] is not None:
        return str(row["actor_user_id"])
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    return "web" if metadata.get("source") == "web" else "-"


def make_router(auth: Auth, repo: Repository, bot, monitor=None) -> Router:
    router = Router(name="admin")

    async def language(user_id: int) -> str:
        saved = await repo.user_settings(user_id)
        return saved["language"] if saved else "en"

    def admin(message_or_callback) -> bool:
        user = message_or_callback.from_user
        return bool(user and auth.is_admin(user.id))

    @router.message(Command("start"), F.chat.type == "private")
    async def start(message: Message):
        user = message.from_user
        if not user:
            return
        user_language = await language(user.id)
        keyboard = main_keyboard(auth.is_admin(user.id), user_language)
        status, request_id = await auth.request_access(user.id, message.chat.id, user.username, user.full_name)
        if status == UserStatus.APPROVED:
            if await repo.aeroapi_key_suffix(user.id):
                await message.answer("✅ Masz już dostęp do FlightPingBot." if user_language == "pl" else "✅ You already have access to FlightPingBot.", reply_markup=keyboard)
            else:
                prefix = "✅ Masz już dostęp do FlightPingBot.\n\n" if user_language == "pl" else "✅ You already have access to FlightPingBot.\n\n"
                await message.answer(prefix + t(user_language, "setup"), parse_mode="HTML", reply_markup=keyboard)
        elif status == UserStatus.BLOCKED:
            await message.answer("🚫 Twój dostęp jest zablokowany. Jeśli to pomyłka, skontaktuj się z administratorem." if user_language == "pl" else "🚫 Your access is blocked. Contact an administrator if you think this is a mistake.", reply_markup=keyboard)
        elif status == UserStatus.PENDING:
            await message.answer("⏳ Twoja prośba o dostęp nadal czeka na decyzję. Powiadomimy Cię po jej rozpatrzeniu." if user_language == "pl" else "⏳ Your access request is still pending. We'll notify you when it is reviewed.", reply_markup=keyboard)
        elif status == "cooldown":
            await message.answer("ℹ️ Poprzednia prośba została odrzucona. Kolejną możesz wysłać po 24 godzinach." if user_language == "pl" else "ℹ️ Your previous request was denied. You can submit another request after 24 hours.", reply_markup=keyboard)
        else:
            await message.answer("✅ Twoja prośba o dostęp została wysłana do administratorów." if user_language == "pl" else "✅ Your access request was sent to the administrators.", reply_markup=keyboard)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Approve", callback_data=f"access:approve:{request_id}"), InlineKeyboardButton(text="❌ Deny", callback_data=f"access:deny:{request_id}")]])
            text = f"New access request\nID: {user.id}\nUsername: @{user.username or 'not set'}\nName: {user.full_name}"
            for admin_id in auth.settings.admin_user_ids:
                try:
                    await bot.send_message(admin_id, text, reply_markup=keyboard)
                except Exception:
                    pass

    @router.callback_query(F.data.startswith("access:"))
    async def decide(callback: CallbackQuery):
        if not admin(callback):
            await callback.answer("You are not authorized.", show_alert=True)
            return
        _, decision, raw_id = callback.data.split(":")
        try:
            changed, user_id = await repo.decide_request(int(raw_id), callback.from_user.id, decision == "approve")
        except ValueError:
            changed, user_id = False, None
        await callback.answer("Saved." if changed else "This request has already been decided.")
        if changed and user_id:
            await repo.audit(callback.from_user.id, "access_decision", "access_request", raw_id, {"decision": decision, "user_id": user_id})
            await callback.message.edit_reply_markup(reply_markup=None)
            user_language = await language(user_id)
            text = (("✅ <b>Przyznano dostęp</b>\n\n" + t(user_language, "setup")) if user_language == "pl" else ("✅ <b>Access granted</b>\n\n" + t(user_language, "setup"))) if decision == "approve" else ("❌ Twoja prośba o dostęp została odrzucona." if user_language == "pl" else "❌ Your access request was denied.")
            await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=main_keyboard(False, user_language))

    @router.message(F.text.in_(button_texts("btn_users")), F.chat.type == "private")
    @router.message(Command("users"), F.chat.type == "private")
    async def users(message: Message):
        if not admin(message):
            return
        rows = await repo.list_users()
        if not rows:
            await message.answer("No users found.")
            return
        await message.answer("\n".join(f"{row['telegram_user_id']} — {row['status']} — {row['display_name']}" for row in rows[:50]))

    @router.message(Command("requests"), F.chat.type == "private")
    async def requests(message: Message):
        if not admin(message):
            return
        rows = await repo.list_users(UserStatus.PENDING)
        await message.answer("No pending requests." if not rows else "\n".join(f"{row['telegram_user_id']} — {row['display_name']}" for row in rows))

    async def decide_by_user(message: Message, approve: bool):
        if not admin(message):
            return
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Usage: /approve <user_id> or /deny <user_id>")
            return
        rows = await repo.list_users(UserStatus.PENDING)
        target = next((row for row in rows if row["telegram_user_id"] == int(parts[1])), None)
        if not target:
            await message.answer("No pending request found.")
            return
        request = await repo.pending_request_for_user(target["telegram_user_id"])
        changed, user_id = await repo.decide_request(request[0], message.from_user.id, approve)
        if changed:
            await repo.audit(message.from_user.id, "access_decision", "access_request", str(request[0]), {"decision": "approve" if approve else "deny", "user_id": user_id})
        await message.answer("Saved." if changed else "This request was already decided.")
        if changed and user_id:
            user_language = await language(user_id)
            text = (("✅ <b>Przyznano dostęp</b>\n\n" + t(user_language, "setup")) if user_language == "pl" else ("✅ <b>Access granted</b>\n\n" + t(user_language, "setup"))) if approve else ("❌ Twoja prośba o dostęp została odrzucona." if user_language == "pl" else "❌ Your access request was denied.")
            await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=main_keyboard(False, user_language))

    @router.message(Command("approve"), F.chat.type == "private")
    async def approve(message: Message):
        await decide_by_user(message, True)

    @router.message(Command("deny"), F.chat.type == "private")
    async def deny(message: Message):
        await decide_by_user(message, False)

    async def change_status(message: Message, status: str):
        if not admin(message):
            return
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer(f"Usage: /{status} <user_id>")
            return
        target_user_id = int(parts[1])
        if await repo.set_user_status(target_user_id, status):
            if status in {UserStatus.BLOCKED, UserStatus.REVOKED} and monitor:
                await monitor.stop_user_all(target_user_id)
            await repo.audit(message.from_user.id, f"user_{status}", "user", parts[1])
            await message.answer("Saved.")
            if status in {UserStatus.BLOCKED, UserStatus.REVOKED}:
                try:
                    user_language = await language(target_user_id)
                    text = ("🚫 Twój dostęp został zablokowany. Monitorowanie zostało zatrzymane." if status == UserStatus.BLOCKED else "⛔ Twój dostęp został cofnięty. Monitorowanie zostało zatrzymane.") if user_language == "pl" else ("🚫 Your access has been blocked. Monitoring has been stopped." if status == UserStatus.BLOCKED else "⛔ Your access has been revoked. Monitoring has been stopped.")
                    await bot.send_message(target_user_id, text)
                except Exception:
                    pass
        else:
            await message.answer("User not found, or the target is an administrator.")

    @router.message(Command("revoke"), F.chat.type == "private")
    async def revoke(message: Message):
        await change_status(message, UserStatus.REVOKED)

    @router.message(Command("block"), F.chat.type == "private")
    async def block(message: Message):
        await change_status(message, UserStatus.BLOCKED)

    @router.message(Command("unblock"), F.chat.type == "private")
    async def unblock(message: Message):
        await change_status(message, UserStatus.APPROVED)

    @router.message(Command("checks"), F.chat.type == "private")
    async def checks(message: Message):
        if not admin(message):
            return
        parts = (message.text or "").split()
        rows = await repo.recent_checks(parts[1].upper() if len(parts) == 2 else None)
        await message.answer("No checks found." if not rows else "\n".join(f"#{r['id']} {r['airport']} {r['status']} flights={r['flight_count']} delayed={r['delayed_count']}" for r in rows))

    @router.message(Command("checklog"), F.chat.type == "private")
    async def checklog(message: Message):
        if not admin(message):
            return
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Usage: /checklog <check_id>")
            return
        observations = await repo.check_observations(int(parts[1]))
        await message.answer("No observations found." if not observations else "\n".join(f"{row['flight_id']} {row['origin'] or '?'}→{row['destination'] or '?'} delay={row['delay_minutes'] or 0} min" for row in observations[:50]))

    @router.message(F.text.in_(button_texts("btn_usage")), F.chat.type == "private")
    @router.message(Command("usage"), F.chat.type == "private")
    async def usage(message: Message):
        if not admin(message):
            if not message.from_user:
                return
            now = datetime.now(timezone.utc)
            day = await repo.usage(now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(), message.from_user.id)
            month = await repo.usage(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(), message.from_user.id)
            await message.answer(
                "Your AeroAPI usage:\n"
                f"Today: requests={day['total'] or 0}, successes={day['success'] or 0}, errors={day['errors'] or 0}, retries={day['retries'] or 0}\n"
                f"This month: requests={month['total'] or 0}, successes={month['success'] or 0}, errors={month['errors'] or 0}, retries={month['retries'] or 0}"
            )
            return
        parts = (message.text or "").split()
        user_id = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else None
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        row = await repo.usage(since, user_id)
        if user_id is None:
            by_user = await repo.usage_by_user(since)
            lines = [f"Last 24 hours — total: requests={row['total'] or 0}, successes={row['success'] or 0}, errors={row['errors'] or 0}, retries={row['retries'] or 0}", "", "Per user:"]
            lines.extend(f"• {item['user_name']}: requests={item['total'] or 0}, successes={item['success'] or 0}, errors={item['errors'] or 0}, retries={item['retries'] or 0}" for item in by_user)
            await message.answer("\n".join(lines) if by_user else lines[0] + "\n\nPer user: no data")
            return
        total = row["total"] or 0
        limit = auth.settings.daily_api_request_limit
        usage_percent = (total / limit * 100) if limit else None
        warning = f"\nWarning threshold: {auth.settings.usage_warning_percent}%" if limit and usage_percent >= auth.settings.usage_warning_percent else ""
        limit_text = f", limit={limit}, usage={usage_percent:.1f}%" if limit else ""
        scope = f" for user {user_id}" if user_id is not None else " overall"
        await message.answer(f"Last 24 hours{scope}: requests={total}, successes={row['success'] or 0}, errors={row['errors'] or 0}, retries={row['retries'] or 0}{limit_text}{warning}")

    @router.message(F.text.in_(button_texts("btn_admin_status")), F.chat.type == "private")
    @router.message(Command("admin_status"), F.chat.type == "private")
    async def admin_status(message: Message):
        if not admin(message):
            return
        counts = await repo.user_counts()
        checks = await repo.recent_checks(limit=1)
        active = monitor.active if monitor else False
        active_airport = escape(monitor.airport) if active and monitor else ""
        latest_check = (
            f"#{checks[0]['id']} {escape(checks[0]['airport'])} {escape(checks[0]['status'])}"
            if checks else "none"
        )
        database_path = escape(str(repo.db.path))
        await message.answer(
            "<b>Admin status</b>\n\n"
            f"Users: approved={counts.get('approved', 0)}, pending={counts.get('pending', 0)}, revoked={counts.get('revoked', 0)}, blocked={counts.get('blocked', 0)}\n"
            f"Active monitor: {'yes — ' + active_airport if active else 'no'}\n"
            f"Latest check: {latest_check}\n"
            f"Database: {database_path}",
            parse_mode="HTML",
        )

    @router.message(Command("alerts"), F.chat.type == "private")
    async def alerts(message: Message):
        if not admin(message):
            return
        parts = (message.text or "").split()
        rows = await repo.list_alerts(parts[1].upper() if len(parts) == 2 else None)
        await message.answer("No alerts found." if not rows else "\n".join(f"#{row['id']} {row['airport']} {row['flight_id']} {row['status']} +{row['delay_minutes']} min" for row in rows))

    @router.message(Command("audit"), F.chat.type == "private")
    async def audit(message: Message):
        if not admin(message):
            return
        parts = (message.text or "").split()
        days = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 7
        rows = await repo.list_audit(max(1, min(days, 180)))
        if not rows:
            await message.answer("No audit events found.")
            return
        lines = [f"{row['created_at']} user={_audit_actor_label(row)} {row['action']} {row['object_type'] or ''} {row['object_id'] or ''}" for row in rows]
        chunks: list[str] = []
        current = ""
        for line in lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > 3800:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        for index, chunk in enumerate(chunks, 1):
            prefix = f"Audit events ({index}/{len(chunks)})\n" if len(chunks) > 1 else "Audit events\n"
            await message.answer(prefix + chunk)

    @router.message(Command("db_status"), F.chat.type == "private")
    async def db_status(message: Message):
        if not admin(message):
            return
        db_path = repo.db.path
        wal_path = db_path.with_name(db_path.name + "-wal")
        backups = sorted((auth.settings.state_dir / "backups").glob("flightpingbot-*.sqlite3"), reverse=True)
        await message.answer(f"<b>Database status</b>\n\nDB: {db_path.stat().st_size if db_path.exists() else 0} bytes\nWAL: {wal_path.stat().st_size if wal_path.exists() else 0} bytes\nBackups: {len(backups)}\nLatest backup: {backups[0].name if backups else 'none'}", parse_mode="HTML")

    @router.message(F.text.in_(button_texts("btn_stop_all")), F.chat.type == "private")
    @router.message(Command("stopall"), F.chat.type == "private")
    async def stopall(message: Message):
        if not admin(message):
            return
        if monitor:
            await monitor.stop_all()
        await repo.audit(message.from_user.id, "stop_all_monitors")
        await message.answer("✅ All monitors were stopped.")

    return router
