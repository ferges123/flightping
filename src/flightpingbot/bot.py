from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from .aeroapi import AeroAPI
from .auth import Auth
from .config import Settings
from .database import Database
from .handlers.admin import make_router as make_admin_router
from .handlers.monitoring import make_router as make_monitoring_router
from .monitor import FlightService, MonitorManager
from .maintenance import Maintenance
from .repositories import Repository
from .keyboards import main_keyboard
from .formatting import format_check
from .security import InboundSecurityMiddleware

log = logging.getLogger(__name__)


async def run(settings: Settings) -> None:
    # Keep the Telegram bot importable in minimal environments; web dependencies
    # are only needed when the application is actually started.
    from .web import WebPanel, serve as serve_web

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    db = await Database(settings.database_path).connect()
    repo = Repository(db, settings.credentials_key)
    await repo.sync_admins(settings.admin_user_ids)
    await repo.recover_monitors_after_restart()
    auth = Auth(settings, repo)
    aeroapi = AeroAPI()
    service = FlightService(repo, aeroapi, settings.min_delay_minutes, settings.monitor_window_hours, settings.daily_api_request_limit, settings.monthly_api_request_limit, settings.user_request_cooldown_seconds)
    monitor = MonitorManager(service, repo, settings.monitor_interval_minutes, settings.monitor_duration_hours, settings.max_active_airports)
    maintenance = Maintenance(
        db,
        settings.observation_retention_days,
        settings.audit_retention_days,
        settings.api_request_retention_days,
        settings.state_dir / "backups",
        backup_count=3,
        check_days=settings.check_retention_days,
        alert_days=settings.alert_retention_days,
        monitor_job_days=settings.monitor_job_retention_days,
    )
    await maintenance.start()
    bot = Bot(settings.bot_token)

    def restored_notification(user_id: int, chat_id: int):
        async def notify(result):
            saved = await repo.user_settings(user_id)
            language = saved["language"] if saved else "en"
            await bot.send_message(chat_id, format_check(result, settings.timezone_name, language), parse_mode=ParseMode.HTML)
        return notify

    await monitor.restore_active(restored_notification)
    web_task = asyncio.create_task(
        serve_web(WebPanel(repo, monitor, bot, settings.admin_user_ids, settings.timezone_name), settings.web_host, settings.web_port),
        name="flightping-web",
    )
    user_commands = [
        BotCommand(command="start", description="Request access or show access status"),
        BotCommand(command="check", description="Check scheduled departures and delays"),
        BotCommand(command="monitor", description="Start airport monitoring"),
        BotCommand(command="stop", description="Stop your monitoring subscriptions"),
        BotCommand(command="status", description="Show monitoring status"),
        BotCommand(command="settings", description="Change language and monitoring defaults"),
        BotCommand(command="help", description="Show help and current settings"),
        BotCommand(command="aeroapi", description="Configure your AeroAPI key"),
        BotCommand(command="hide", description="Hide the keyboard"),
    ]
    admin_commands = user_commands + [
        BotCommand(command="requests", description="List pending access requests"),
        BotCommand(command="users", description="List users"),
        BotCommand(command="usage", description="Show API usage"),
        BotCommand(command="admin_status", description="Show system status"),
        BotCommand(command="alerts", description="Show alerts"),
        BotCommand(command="audit", description="Show audit events"),
        BotCommand(command="db_status", description="Show database status"),
        BotCommand(command="stopall", description="Stop all monitors"),
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    for admin_id in settings.admin_user_ids:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as exc:
            # Telegram returns chat not found until the admin sends /start
            # to a newly created bot. Default user commands remain available.
            log.warning("could not set admin command scope for %s: %s", admin_id, exc)
    dispatcher = Dispatcher()
    dispatcher.message.middleware(
        InboundSecurityMiddleware(
            settings.admin_user_ids,
            settings.telegram_messages_per_minute,
            settings.fsm_state_ttl_seconds,
        )
    )
    dispatcher.include_router(make_admin_router(auth, repo, bot, monitor))
    dispatcher.include_router(make_monitoring_router(auth, service, monitor))
    polling_task = asyncio.create_task(dispatcher.start_polling(bot), name="flightping-polling")
    try:
        done, _ = await asyncio.wait({web_task, polling_task}, return_when=asyncio.FIRST_COMPLETED)
        # A stopped or failed web server must bring down the process so
        # systemd can restart the complete application instead of leaving a
        # functioning bot with a silently unavailable admin panel.
        for task in done:
            task.result()
    finally:
        for task in (web_task, polling_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(web_task, polling_task, return_exceptions=True)
        # Stop in-memory tasks without marking persisted monitors as stopped;
        # the next process start will restore them from SQLite.
        await monitor.stop_all(persist=False)
        await maintenance.stop()
        await aeroapi.close()
        await bot.session.close()
        await db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)
    try:
        env_file = Path("config/flightpingbot.env")
        settings = Settings.from_env(env_file if env_file.exists() else None)
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
