import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import flightpingbot.bot as bot_module
import flightpingbot.web as web_module
from flightpingbot.config import Settings


class _FakeRepo:
    async def sync_admins(self, admin_ids):
        return None

    async def recover_monitors_after_restart(self):
        return None


class _FakeSession:
    def __init__(self, events):
        self.events = events

    async def close(self):
        self.events.append("bot.close")


class _FakeBot:
    def __init__(self, token, events):
        self.events = events
        self.session = _FakeSession(events)

    async def set_my_commands(self, *args, **kwargs):
        self.events.append("bot.commands")


class _FakeDispatcher:
    def __init__(self, events, polling):
        self.events = events
        self.polling = polling
        self.message = SimpleNamespace(middleware=self._middleware)

    def _middleware(self, value):
        self.events.append("dispatcher.middleware")

    def include_router(self, router):
        self.events.append("dispatcher.router")

    async def start_polling(self, bot):
        self.events.append("polling.start")
        return await self.polling()


def _install_run_fakes(monkeypatch, events, *, serve, polling):
    repo = _FakeRepo()

    class FakeDatabase:
        def __init__(self, path):
            self.path = path

        async def connect(self):
            events.append("db.connect")
            return self

        async def close(self):
            events.append("db.close")

    class FakeAeroAPI:
        async def close(self):
            events.append("aeroapi.close")

    class FakeService:
        def __init__(self, *args):
            pass

    class FakeMonitor:
        def __init__(self, *args):
            pass

        async def restore_active(self, callback_factory):
            events.append("monitor.restore")

        async def cancel_all(self):
            events.append("monitor.cancel")

    class FakeMaintenance:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            events.append("maintenance.start")

        async def stop(self):
            events.append("maintenance.stop")

    monkeypatch.setattr(bot_module, "Database", FakeDatabase)
    monkeypatch.setattr(bot_module, "Repository", lambda db, key: repo)
    monkeypatch.setattr(bot_module, "AeroAPI", FakeAeroAPI)
    monkeypatch.setattr(bot_module, "FlightService", FakeService)
    monkeypatch.setattr(bot_module, "MonitorManager", FakeMonitor)
    monkeypatch.setattr(bot_module, "Maintenance", FakeMaintenance)
    monkeypatch.setattr(bot_module, "Bot", lambda token: _FakeBot(token, events))
    monkeypatch.setattr(bot_module, "Dispatcher", lambda: _FakeDispatcher(events, polling))
    monkeypatch.setattr(web_module, "WebPanel", lambda *args, **kwargs: object())
    monkeypatch.setattr(web_module, "serve", serve)


@pytest.mark.asyncio
async def test_run_closes_every_resource_when_polling_stops(monkeypatch, tmp_path):
    events = []

    async def serve(panel, host, port):
        events.append("web.start")
        await asyncio.Event().wait()

    async def polling():
        return None

    _install_run_fakes(monkeypatch, events, serve=serve, polling=polling)

    await bot_module.run(Settings("token", frozenset({100}), Path(tmp_path)))

    assert {"monitor.cancel", "maintenance.stop", "aeroapi.close", "bot.close", "db.close"} <= set(events)


@pytest.mark.asyncio
async def test_run_cleans_up_when_web_server_fails(monkeypatch, tmp_path):
    events = []

    async def serve(panel, host, port):
        events.append("web.start")
        raise RuntimeError("web server failed")

    async def polling():
        await asyncio.Event().wait()

    _install_run_fakes(monkeypatch, events, serve=serve, polling=polling)

    with pytest.raises(RuntimeError, match="web server failed"):
        await bot_module.run(Settings("token", frozenset({100}), Path(tmp_path)))

    assert {"monitor.cancel", "maintenance.stop", "aeroapi.close", "bot.close", "db.close"} <= set(events)
