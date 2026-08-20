from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from ..auth import Auth
from ..errors import user_facing_error
from ..formatting import format_check
from ..monitor import FlightService, MonitorManager
from ..keyboards import main_keyboard
from ..onboarding import AEROAPI_SETUP_INSTRUCTIONS
import logging


log = logging.getLogger(__name__)


class InputState(StatesGroup):
    check_airport = State()
    monitor_airport = State()
    aeroapi_key = State()


def make_router(auth: Auth, service: FlightService, monitor: MonitorManager) -> Router:
    router = Router(name="monitoring")

    @router.message(F.text == "🔐 AeroAPI", F.chat.type == "private")
    @router.message(Command("aeroapi"), F.chat.type == "private")
    async def aeroapi_command(message: Message, state: FSMContext):
        user = message.from_user
        if not user or not await auth.is_approved(user.id):
            await message.answer("🔒 You don't have access yet. Send /start to request access.")
            return
        parts = (message.text or "").split(maxsplit=1)
        action = parts[1].strip().lower() if len(parts) == 2 else "set"
        if action == "status":
            suffix = await service.repo.aeroapi_key_suffix(user.id)
            if suffix and suffix.startswith("invalid:"):
                await message.answer(f"⚠️ AeroAPI key ending in …{suffix.removeprefix('invalid:')} was rejected. Use /aeroapi set to replace it.")
            else:
                await message.answer(f"✅ AeroAPI is configured (key ending in …{suffix})." if suffix else "⚠️ AeroAPI is not configured yet. Use /aeroapi to add your personal key.")
        elif action == "remove":
            removed = await service.repo.remove_aeroapi_key(user.id)
            if removed:
                await service.repo.audit(user.id, "aeroapi_key_removed")
                await monitor.stop_user_all(user.id)
                await message.answer("✅ Your AeroAPI key was removed and monitoring was stopped.")
            else:
                await message.answer("ℹ️ No AeroAPI key was configured.")
        elif action == "test":
            try:
                status = await service.test_aeroapi(user.id)
            except (RuntimeError, ValueError) as exc:
                await message.answer(user_facing_error(exc))
                return
            await service.repo.audit(user.id, "aeroapi_key_test", metadata={"status_code": status})
            await message.answer("✅ Your AeroAPI key works. FlightAware accepted the test request.")
        elif action in {"set", "replace"}:
            await state.set_state(InputState.aeroapi_key)
            await message.answer(AEROAPI_SETUP_INSTRUCTIONS + "\n\nSend /cancel if you changed your mind.", parse_mode=ParseMode.HTML)
        else:
            await message.answer("Usage: /aeroapi · /aeroapi status · /aeroapi test · /aeroapi remove")

    @router.message(InputState.aeroapi_key, ~F.text.startswith("/"), F.chat.type == "private")
    async def aeroapi_key_input(message: Message, state: FSMContext):
        user = message.from_user
        if not user:
            return
        if not await auth.is_approved(user.id):
            await state.clear()
            await message.answer("🔒 You don't have access yet. Send /start to request access.")
            return
        api_key = (message.text or "").strip()
        delete_failed = False
        try:
            await message.delete()
        except Exception:
            delete_failed = True
            log.warning("could not delete AeroAPI key message")
        delete_warning = "\n\n⚠️ I could not delete your key message. Delete it manually from this chat." if delete_failed else ""
        if not api_key or len(api_key) > 512:
            await message.answer("⚠️ That doesn't look like a valid AeroAPI key. Try again or send /cancel." + delete_warning)
            return
        try:
            await service.repo.set_aeroapi_key(user.id, api_key)
        except ValueError as exc:
            await state.clear()
            await message.answer(str(exc) + " Use /aeroapi to try again." + delete_warning)
            return
        await service.repo.audit(user.id, "aeroapi_key_set")
        await state.clear()
        await message.answer("✅ Your AeroAPI key was saved securely." + delete_warning)

    @router.message(F.text == "🔎 Check", F.chat.type == "private")
    async def check_button(message: Message, state: FSMContext):
        if not message.from_user or not await auth.is_approved(message.from_user.id):
            await message.answer("🔒 You don't have access yet. Send /start to request access.")
            return
        await state.set_state(InputState.check_airport)
        await message.answer("📍 Send a three-letter airport code, for example WAW or TFS.\nSend /cancel to stop.")

    @router.message(InputState.check_airport, ~F.text.startswith("/"), F.chat.type == "private")
    async def check_airport_input(message: Message, state: FSMContext):
        user = message.from_user
        if not user:
            return
        if not await auth.is_approved(user.id):
            await state.clear()
            await message.answer("🔒 You don't have access yet. Send /start to request access.")
            return
        try:
            result = await service.check(user.id, (message.text or "").strip())
        except (RuntimeError, ValueError) as exc:
            await message.answer(user_facing_error(exc) + " Try again or send /cancel.")
            return
        await state.clear()
        await service.repo.audit(user.id, "check", "check", str(result.check_id), {"airport": result.airport})
        await message.answer(format_check(result, auth.settings.timezone_name), parse_mode=ParseMode.HTML)

    @router.message(F.text == "▶️ Monitor", F.chat.type == "private")
    async def monitor_button(message: Message, state: FSMContext):
        if not message.from_user or not await auth.is_approved(message.from_user.id):
            await message.answer("🔒 You don't have access yet. Send /start to request access.")
            return
        await state.set_state(InputState.monitor_airport)
        await message.answer("📍 Send a three-letter airport code, for example WAW or TFS.\nSend /cancel to stop.")

    @router.message(InputState.monitor_airport, ~F.text.startswith("/"), F.chat.type == "private")
    async def monitor_airport_input(message: Message, state: FSMContext):
        user = message.from_user
        if not user:
            return
        if not await auth.is_approved(user.id):
            await state.clear()
            await message.answer("🔒 You don't have access yet. Send /start to request access.")
            return
        airport = (message.text or "").strip().upper()
        try:
            monitor_state = await monitor.start(user.id, message.chat.id, airport, lambda result: message.answer(format_check(result, auth.settings.timezone_name), parse_mode=ParseMode.HTML))
        except (RuntimeError, ValueError) as exc:
            await message.answer(user_facing_error(exc) + " Try again or send /cancel.")
            return
        await state.clear()
        await service.repo.audit(user.id, "monitor_start", "monitor", airport, {"state": monitor_state})
        await message.answer(f"✅ Monitoring for <b>{airport}</b> {'started.' if monitor_state == 'started' else 'is already active for you.'}", parse_mode=ParseMode.HTML)

    @router.message(Command("cancel"), F.chat.type == "private")
    async def cancel_input(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("↩️ Cancelled. Nothing was changed.")

    @router.message(F.text == "✖ Hide", F.chat.type == "private")
    @router.message(F.text == "✖ Hide keyboard", F.chat.type == "private")
    @router.message(Command("hide"), F.chat.type == "private")
    async def hide_keyboard(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Keyboard hidden. Send /help to show it again.", reply_markup=ReplyKeyboardRemove())

    @router.message(F.text == "❓ Help", F.chat.type == "private")
    @router.message(Command("help"), F.chat.type == "private")
    async def help_command(message: Message):
        text = (
            "<b>FlightPingBot help</b>\n\n"
            "<b>User commands</b>\n"
            "/start — request access or show your access status\n"
            "/check &lt;IATA&gt; — check scheduled departures and delays\n"
            "/monitor &lt;IATA&gt; — start monitoring every 30 minutes\n"
            "/stop — stop your monitoring subscriptions\n"
            "/status — show the monitor status\n"
            "/aeroapi — configure your personal AeroAPI key\n"
            "/usage — show your personal AeroAPI usage\n"
            "/help — show this help\n"
        )
        text += "\n" + AEROAPI_SETUP_INSTRUCTIONS + "\n"
        text += "/aeroapi test — verify your personal API key\n"
        if message.from_user and auth.is_admin(message.from_user.id):
            text += (
                "\n<b>Admin commands</b>\n"
                "/requests — list pending access requests\n"
                "/users — list users and their status\n"
                "/approve &lt;user_id&gt; / /deny &lt;user_id&gt; — decide an access request\n"
                "/revoke &lt;user_id&gt; / /block &lt;user_id&gt; / /unblock &lt;user_id&gt;\n"
                "/checks [IATA] — show recent checks\n"
                "/checklog &lt;check_id&gt; — show observed flights\n"
                "/usage [user_id] — show per-user API usage\n"
                "/admin_status — show users, monitor and database status\n"
                "/alerts [IATA] — show sent and suppressed alerts\n"
                "/audit [days] — show recent audit events\n"
                "/db_status — show database, WAL and backup status\n"
                "/stopall — emergency stop for all monitors\n"
            )
        text += (
            "\n<b>Current settings</b>\n"
            f"Check window: {auth.settings.monitor_window_hours} hours\n"
            f"Monitor interval: {auth.settings.monitor_interval_minutes} minutes\n"
            f"Monitor duration: {auth.settings.monitor_duration_hours} hours\n"
            f"Delay threshold: {auth.settings.min_delay_minutes} minutes\n"
            f"Maximum active airports: {auth.settings.max_active_airports}\n"
        )
        await message.answer(text + "\nAll commands work in private chats only.", parse_mode=ParseMode.HTML, reply_markup=main_keyboard(bool(message.from_user and auth.is_admin(message.from_user.id))))

    @router.message(Command("check"), F.chat.type == "private")
    async def check(message: Message):
        user = message.from_user
        if not user or not await auth.is_approved(user.id):
            await message.answer("🔒 You don't have access yet. Send /start to request access.")
            return
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("Usage: /check <IATA>\nExample: /check TFS")
            return
        try:
            result = await service.check(user.id, parts[1])
        except (RuntimeError, ValueError) as exc:
            await message.answer(user_facing_error(exc))
            return
        await service.repo.audit(user.id, "check", "check", str(result.check_id), {"airport": result.airport})
        await message.answer(format_check(result, auth.settings.timezone_name), parse_mode=ParseMode.HTML)

    @router.message(Command("monitor"), F.chat.type == "private")
    async def start_monitor(message: Message):
        user = message.from_user
        if not user or not await auth.is_approved(user.id):
            await message.answer("🔒 You don't have access yet. Send /start to request access.")
            return
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("Usage: /monitor <IATA>\nExample: /monitor TFS")
            return
        try:
            monitor_state = await monitor.start(user.id, message.chat.id, parts[1], lambda result: message.answer(format_check(result, auth.settings.timezone_name), parse_mode=ParseMode.HTML))
        except (RuntimeError, ValueError) as exc:
            await message.answer(str(exc))
            return
        await service.repo.audit(user.id, "monitor_start", "monitor", parts[1].upper(), {"state": monitor_state})
        await message.answer(f"Monitoring for {parts[1].upper()} {'started.' if monitor_state == 'started' else 'subscription added.'}")

    @router.message(F.text == "⏹ Stop", F.chat.type == "private")
    @router.message(Command("stop"), F.chat.type == "private")
    async def stop_monitor(message: Message):
        if not message.from_user or not await auth.is_approved(message.from_user.id):
            await message.answer("🔒 You don't have access yet. Send /start to request access.")
            return
        parts = (message.text or "").split()
        if len(parts) > 2:
            await message.answer("Usage: /stop [IATA]\nExample: /stop WAW")
            return
        if len(parts) == 2:
            airport = parts[1].upper()
            changed = await monitor.stop_user_airport(message.from_user.id, airport, message.chat.id)
            await service.repo.audit(message.from_user.id, "monitor_stop", "monitor", airport)
            await message.answer(f"✅ Monitoring for <b>{airport}</b> was stopped." if changed else f"ℹ️ You have no active monitoring for <b>{airport}</b>.", parse_mode=ParseMode.HTML)
            return
        changed = await monitor.stop_user(message.from_user.id, message.chat.id)
        await service.repo.audit(message.from_user.id, "monitor_stop", "monitor", monitor.airport or "")
        await message.answer("✅ Your monitoring subscriptions were stopped." if changed else "ℹ️ You have no active monitoring subscriptions.")

    @router.message(F.text == "📊 Status", F.chat.type == "private")
    @router.message(Command("status"), F.chat.type == "private")
    async def status(message: Message):
        if not message.from_user or not await auth.is_approved(message.from_user.id):
            await message.answer("🔒 You don't have access yet. Send /start to request access.")
            return
        jobs = await service.repo.user_monitor_jobs(message.from_user.id, message.chat.id)
        if not jobs:
            await message.answer("📡 Monitoring is currently inactive.")
            return
        airports = ", ".join(row["airport"] for row in jobs)
        await message.answer(f"📡 Monitoring is active for: <b>{airports}</b>", parse_mode=ParseMode.HTML)

    return router
