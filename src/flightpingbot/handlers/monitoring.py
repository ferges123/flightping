from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeChat, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from datetime import datetime, timezone

from ..auth import Auth
from ..errors import user_facing_error
from ..formatting import format_check
from ..monitor import FlightService, MonitorManager
from ..keyboards import main_keyboard
from ..i18n import button_texts, t, telegram_commands
import logging


log = logging.getLogger(__name__)


class InputState(StatesGroup):
    check_airport = State()
    monitor_airport = State()
    aeroapi_key = State()


def make_router(auth: Auth, service: FlightService, monitor: MonitorManager, bot=None) -> Router:
    router = Router(name="monitoring")

    async def preferences(user_id: int):
        get_settings = getattr(service.repo, "user_settings", None)
        saved = await get_settings(user_id) if get_settings else None
        language = saved["language"] if saved else "en"
        return language, {
            "window_hours": saved["window_hours"] if saved and saved["window_hours"] else service.window_hours,
            "interval_minutes": saved["interval_minutes"] if saved and saved["interval_minutes"] else monitor.interval // 60,
            "min_delay_minutes": saved["min_delay_minutes"] if saved and saved["min_delay_minutes"] else service.min_delay_minutes,
            "duration_hours": saved["duration_hours"] if saved and saved["duration_hours"] else getattr(monitor, "duration", 6 * 3600) // 3600,
        }

    def settings_keyboard(language: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "set_language_en"), callback_data="setting:language:en"), InlineKeyboardButton(text=t(language, "set_language_pl"), callback_data="setting:language:pl")],
            [InlineKeyboardButton(text=t(language, "set_menu_window"), callback_data="setting:menu:window"), InlineKeyboardButton(text=t(language, "set_menu_interval"), callback_data="setting:menu:interval")],
            [InlineKeyboardButton(text=t(language, "set_menu_delay"), callback_data="setting:menu:delay"), InlineKeyboardButton(text=t(language, "set_menu_duration"), callback_data="setting:menu:duration")],
            [InlineKeyboardButton(text=t(language, "set_reset"), callback_data="setting:reset")],
        ])

    def value_keyboard(kind: str, language: str) -> InlineKeyboardMarkup:
        values = {"window": (3, 6, 9, 12), "interval": (15, 30, 45, 60), "delay": (30, 45, 60, 90, 120), "duration": (3, 6, 12, 24)}[kind]
        suffix = " h" if kind in {"window", "duration"} else " min"
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{value}{suffix}", callback_data=f"setting:{kind}:{value}") for value in values],
            [InlineKeyboardButton(text=t(language, "set_back"), callback_data="setting:back")],
        ])

    async def settings_text(user_id: int) -> tuple[str, str]:
        language, values = await preferences(user_id)
        now = datetime.now(timezone.utc)
        usage = await service.repo.usage(
            now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(),
            user_id,
        )
        text = "\n".join((
            t(language, "settings_title"), "",
            t(language, "settings_language", language="Polski" if language == "pl" else "English"),
            t(language, "settings_window", value=values["window_hours"]),
            t(language, "settings_interval", value=values["interval_minutes"]),
            t(language, "settings_delay", value=values["min_delay_minutes"]),
            t(language, "settings_duration", value=values["duration_hours"]), "",
            t(language, "settings_monthly_usage", value=usage["total"] or 0), "",
            t(language, "settings_note"),
        ))
        return language, text

    async def refresh_user_menu(user_id: int, language: str, notice_key: str) -> None:
        """Apply a changed/reset language to the per-chat command menu and keyboard."""
        if bot is None:
            return
        try:
            is_admin = auth.is_admin(user_id)
            await bot.set_my_commands(
                [BotCommand(command=command, description=description) for command, description in telegram_commands(language, is_admin)],
                scope=BotCommandScopeChat(chat_id=user_id),
            )
            # Telegram swaps the device keyboard only when a message includes
            # a new reply markup.
            await bot.send_message(user_id, t(language, notice_key), reply_markup=main_keyboard(is_admin, language))
        except Exception:
            log.warning("could not update the command menu for user %s", user_id)

    @router.message(Command("settings"), F.chat.type == "private")
    @router.message(F.text.in_(button_texts("btn_settings")), F.chat.type == "private")
    async def setting(message: Message):
        if not message.from_user or not await auth.is_approved(message.from_user.id):
            await message.answer(t("en", "no_access"))
            return
        language, text = await settings_text(message.from_user.id)
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=settings_keyboard(language))

    @router.callback_query(F.data.startswith("setting:"))
    async def change_setting(callback: CallbackQuery):
        user = callback.from_user
        if not user or not await auth.is_approved(user.id):
            await callback.answer(t("en", "no_access"), show_alert=True)
            return
        _, kind, *raw_value = callback.data.split(":")
        language, _ = await preferences(user.id)
        if kind == "menu":
            await callback.message.edit_reply_markup(reply_markup=value_keyboard(raw_value[0], language))
            await callback.answer()
            return
        if kind == "back":
            _, text = await settings_text(user.id)
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=settings_keyboard(language))
            await callback.answer()
            return
        if kind == "reset":
            await service.repo.update_user_settings(user.id, reset=True)
            language, text = await settings_text(user.id)
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=settings_keyboard(language))
            await callback.answer(t(language, "settings_reset"))
            await refresh_user_menu(user.id, language, "settings_reset")
            return
        value = raw_value[0]
        if kind == "language":
            await service.repo.update_user_settings(user.id, language=value)
            language, text = await settings_text(user.id)
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=settings_keyboard(language))
            await callback.answer(t(language, "settings_language_saved"))
            await refresh_user_menu(user.id, language, "settings_language_saved")
            return
        field = {"window": "window_hours", "interval": "interval_minutes", "delay": "min_delay_minutes", "duration": "duration_hours"}[kind]
        await service.repo.update_user_settings(user.id, **{field: int(value)})
        language, text = await settings_text(user.id)
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=settings_keyboard(language))
        label = t(language, f"settings_{kind}", value="{value}").split(":", 1)[0]
        await callback.answer(t(language, "settings_saved", label=label, value=f"{value}{' h' if kind in {'window', 'duration'} else ' min'}"))

    @router.message(F.text.in_(button_texts("btn_aeroapi")), F.chat.type == "private")
    @router.message(Command("aeroapi"), F.chat.type == "private")
    async def aeroapi_command(message: Message, state: FSMContext):
        user = message.from_user
        if not user or not await auth.is_approved(user.id):
            await message.answer(t("en", "no_access"))
            return
        language, _ = await preferences(user.id)
        parts = (message.text or "").split(maxsplit=1)
        action = parts[1].strip().lower() if len(parts) == 2 else "set"
        if action == "status":
            suffix = await service.repo.aeroapi_key_suffix(user.id)
            if suffix and suffix.startswith("invalid:"):
                await message.answer(t(language, "aeroapi_invalid", suffix=suffix.removeprefix("invalid:")))
            else:
                await message.answer(t(language, "aeroapi_configured", suffix=suffix) if suffix else t(language, "aeroapi_missing"))
        elif action == "remove":
            removed = await service.repo.remove_aeroapi_key(user.id)
            if removed:
                await service.repo.audit(user.id, "aeroapi_key_removed")
                await monitor.stop_user_all(user.id)
                await message.answer(t(language, "aeroapi_removed"))
            else:
                await message.answer(t(language, "aeroapi_none"))
        elif action == "test":
            try:
                status = await service.test_aeroapi(user.id)
            except (RuntimeError, ValueError) as exc:
                await message.answer(user_facing_error(exc, language))
                return
            await service.repo.audit(user.id, "aeroapi_key_test", metadata={"status_code": status})
            await message.answer(t(language, "aeroapi_works"))
        elif action in {"set", "replace"}:
            await state.set_state(InputState.aeroapi_key)
            await message.answer(t(language, "setup") + t(language, "setup_cancel"), parse_mode=ParseMode.HTML)
        else:
            await message.answer(t(language, "aeroapi_usage"))

    @router.message(InputState.aeroapi_key, ~F.text.startswith("/"), F.chat.type == "private")
    async def aeroapi_key_input(message: Message, state: FSMContext):
        user = message.from_user
        if not user:
            return
        if not await auth.is_approved(user.id):
            await state.clear()
            await message.answer(t("en", "no_access"))
            return
        language, _ = await preferences(user.id)
        api_key = (message.text or "").strip()
        delete_failed = False
        try:
            await message.delete()
        except Exception:
            delete_failed = True
            log.warning("could not delete AeroAPI key message")
        delete_warning = t(language, "key_delete_warning") if delete_failed else ""
        if not api_key or len(api_key) > 512:
            await message.answer(t(language, "aeroapi_invalid_input") + delete_warning)
            return
        try:
            await service.repo.set_aeroapi_key(user.id, api_key)
        except ValueError as exc:
            await state.clear()
            await message.answer(user_facing_error(exc, language) + " /aeroapi" + delete_warning)
            return
        await service.repo.audit(user.id, "aeroapi_key_set")
        await state.clear()
        await message.answer(t(language, "aeroapi_saved") + delete_warning)

    @router.message(F.text.in_(button_texts("btn_check")), F.chat.type == "private")
    async def check_button(message: Message, state: FSMContext):
        if not message.from_user or not await auth.is_approved(message.from_user.id):
            await message.answer(t("en", "no_access"))
            return
        language, _ = await preferences(message.from_user.id)
        await state.set_state(InputState.check_airport)
        await message.answer(t(language, "airport_prompt"))

    @router.message(InputState.check_airport, ~F.text.startswith("/"), F.chat.type == "private")
    async def check_airport_input(message: Message, state: FSMContext):
        user = message.from_user
        if not user:
            return
        if not await auth.is_approved(user.id):
            await state.clear()
            await message.answer(t("en", "no_access"))
            return
        language, values = await preferences(user.id)
        try:
            result = await service.check(user.id, (message.text or "").strip(), window_hours=values["window_hours"], min_delay_minutes=values["min_delay_minutes"])
        except (RuntimeError, ValueError) as exc:
            await message.answer(user_facing_error(exc, language) + t(language, "try_again"))
            return
        await state.clear()
        await service.repo.audit(user.id, "check", "check", str(result.check_id), {"airport": result.airport})
        await message.answer(format_check(result, auth.settings.timezone_name, language), parse_mode=ParseMode.HTML)

    @router.message(F.text.in_(button_texts("btn_monitor")), F.chat.type == "private")
    async def monitor_button(message: Message, state: FSMContext):
        if not message.from_user or not await auth.is_approved(message.from_user.id):
            await message.answer(t("en", "no_access"))
            return
        language, _ = await preferences(message.from_user.id)
        await state.set_state(InputState.monitor_airport)
        await message.answer(t(language, "airport_prompt"))

    @router.message(InputState.monitor_airport, ~F.text.startswith("/"), F.chat.type == "private")
    async def monitor_airport_input(message: Message, state: FSMContext):
        user = message.from_user
        if not user:
            return
        if not await auth.is_approved(user.id):
            await state.clear()
            await message.answer(t("en", "no_access"))
            return
        language, values = await preferences(user.id)
        airport = (message.text or "").strip().upper()
        try:
            async def notify(result):
                current_language, _ = await preferences(user.id)
                await message.answer(format_check(result, auth.settings.timezone_name, current_language), parse_mode=ParseMode.HTML)
            monitor_state = await monitor.start(user.id, message.chat.id, airport, notify, **values)
        except (RuntimeError, ValueError) as exc:
            await message.answer(user_facing_error(exc, language) + t(language, "try_again"))
            return
        await state.clear()
        await service.repo.audit(user.id, "monitor_start", "monitor", airport, {"state": monitor_state})
        await message.answer(t(language, "monitor_started" if monitor_state == "started" else "monitor_subscribed", airport=airport), parse_mode=ParseMode.HTML)

    @router.message(Command("cancel"), F.chat.type == "private")
    async def cancel_input(message: Message, state: FSMContext):
        await state.clear()
        language, _ = await preferences(message.from_user.id) if message.from_user else ("en", {})
        await message.answer(t(language, "cancelled"))

    @router.message(F.text.in_(button_texts("btn_hide") | {"✖ Hide keyboard"}), F.chat.type == "private")
    @router.message(Command("hide"), F.chat.type == "private")
    async def hide_keyboard(message: Message, state: FSMContext):
        await state.clear()
        language, _ = await preferences(message.from_user.id) if message.from_user else ("en", {})
        await message.answer(t(language, "hidden"), reply_markup=ReplyKeyboardRemove())

    @router.message(F.text.in_(button_texts("btn_help")), F.chat.type == "private")
    @router.message(Command("help"), F.chat.type == "private")
    async def help_command(message: Message):
        language, _ = await preferences(message.from_user.id) if message.from_user else ("en", {})
        text = t(language, "help") + "\n\n" + t(language, "setup") + "\n/aeroapi test — verify your personal API key\n"
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
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard(bool(message.from_user and auth.is_admin(message.from_user.id)), language))

    @router.message(Command("check"), F.chat.type == "private")
    async def check(message: Message):
        user = message.from_user
        if not user or not await auth.is_approved(user.id):
            await message.answer(t("en", "no_access"))
            return
        language, values = await preferences(user.id)
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer(t(language, "check_usage"))
            return
        try:
            result = await service.check(user.id, parts[1], window_hours=values["window_hours"], min_delay_minutes=values["min_delay_minutes"])
        except (RuntimeError, ValueError) as exc:
            await message.answer(user_facing_error(exc, language))
            return
        await service.repo.audit(user.id, "check", "check", str(result.check_id), {"airport": result.airport})
        await message.answer(format_check(result, auth.settings.timezone_name, language), parse_mode=ParseMode.HTML)

    @router.message(Command("monitor"), F.chat.type == "private")
    async def start_monitor(message: Message):
        user = message.from_user
        if not user or not await auth.is_approved(user.id):
            await message.answer(t("en", "no_access"))
            return
        language, values = await preferences(user.id)
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer(t(language, "monitor_usage"))
            return
        try:
            async def notify(result):
                current_language, _ = await preferences(user.id)
                await message.answer(format_check(result, auth.settings.timezone_name, current_language), parse_mode=ParseMode.HTML)
            monitor_state = await monitor.start(user.id, message.chat.id, parts[1], notify, **values)
        except (RuntimeError, ValueError) as exc:
            await message.answer(user_facing_error(exc, language))
            return
        await service.repo.audit(user.id, "monitor_start", "monitor", parts[1].upper(), {"state": monitor_state})
        await message.answer(t(language, "monitor_started" if monitor_state == "started" else "monitor_subscribed", airport=parts[1].upper()), parse_mode=ParseMode.HTML)

    async def stop_all_monitors(message: Message):
        if not message.from_user or not await auth.is_approved(message.from_user.id):
            await message.answer(t("en", "no_access"))
            return
        language, _ = await preferences(message.from_user.id)
        # Capture the airports before stopping: after stop_user() the registry
        # no longer holds this user's jobs (and its property is global anyway).
        jobs = await service.repo.user_monitor_jobs(message.from_user.id, message.chat.id)
        airports = ", ".join(row["airport"] for row in jobs)
        changed = await monitor.stop_user(message.from_user.id, message.chat.id)
        await service.repo.audit(message.from_user.id, "monitor_stop", "monitor", airports)
        await message.answer(t(language, "monitors_stopped" if changed else "monitors_missing"))

    @router.message(F.text.in_(button_texts("btn_stop")), F.chat.type == "private")
    async def stop_monitor_button(message: Message):
        await stop_all_monitors(message)

    @router.message(Command("stop"), F.chat.type == "private")
    async def stop_monitor(message: Message):
        if not message.from_user or not await auth.is_approved(message.from_user.id):
            await message.answer(t("en", "no_access"))
            return
        language, _ = await preferences(message.from_user.id)
        parts = (message.text or "").split()
        if len(parts) > 2:
            await message.answer(t(language, "stop_usage"))
            return
        if len(parts) == 1:
            await stop_all_monitors(message)
            return
        airport = parts[1].upper()
        changed = await monitor.stop_user_airport(message.from_user.id, airport, message.chat.id)
        await service.repo.audit(message.from_user.id, "monitor_stop", "monitor", airport)
        await message.answer(t(language, "monitor_stopped" if changed else "monitor_missing", airport=airport), parse_mode=ParseMode.HTML)

    @router.message(F.text.in_(button_texts("btn_status")), F.chat.type == "private")
    @router.message(Command("status"), F.chat.type == "private")
    async def status(message: Message):
        if not message.from_user or not await auth.is_approved(message.from_user.id):
            await message.answer(t("en", "no_access"))
            return
        language, _ = await preferences(message.from_user.id)
        jobs = await service.repo.user_monitor_jobs(message.from_user.id, message.chat.id)
        if not jobs:
            await message.answer(t(language, "monitor_inactive"))
            return
        airports = ", ".join(row["airport"] for row in jobs)
        await message.answer(t(language, "monitor_active", airports=airports), parse_mode=ParseMode.HTML)

    return router
