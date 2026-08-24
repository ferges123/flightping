from pathlib import Path
from types import SimpleNamespace

import pytest

from flightpingbot.auth import Auth
from flightpingbot.config import Settings
from flightpingbot.handlers.admin import make_router as make_admin_router
from flightpingbot.handlers.admin import _audit_actor_label
from flightpingbot.handlers.monitoring import make_router as make_monitoring_router
from flightpingbot.errors import user_facing_error
from flightpingbot.formatting import format_check
from flightpingbot.monitor import CheckResult


class FakeRepo:
    def __init__(self, status=None):
        self.status = status

    async def user(self, user_id):
        return {"status": self.status} if self.status else None

    async def audit(self, *args, **kwargs):
        return None

    async def usage(self, since, actor_user_id=None):
        return {"total": 2, "success": 1, "errors": 1, "retries": 0}

    async def set_user_status(self, user_id, status):
        self.status = status
        return True

    async def remove_aeroapi_key(self, user_id):
        return False


class FakeMessage:
    def __init__(self, user_id, text="/help"):
        self.from_user = SimpleNamespace(id=user_id, username=None, full_name="Test User")
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.text = text
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)

    async def delete(self):
        return None


class FakeService:
    repo = FakeRepo()
    window_hours = 9
    min_delay_minutes = 60

    async def check(self, user_id, airport, **kwargs):
        raise RuntimeError("AeroAPI returned HTTP 400: invalid airport")

    async def test_aeroapi(self, user_id):
        return 200


class FakeMonitor:
    active = False
    airport = ""
    interval = 30 * 60

    def __init__(self):
        self.stopped_users = []

    async def stop_user_all(self, user_id):
        self.stopped_users.append(user_id)


def auth(tmp_path):
    return Auth(Settings("bot", frozenset({100}), Path(tmp_path)), FakeRepo())


@pytest.mark.asyncio
async def test_help_hides_admin_commands_for_regular_users(tmp_path):
    router = make_monitoring_router(auth(tmp_path), FakeService(), FakeMonitor())
    handler = next(item.callback for item in router.message.handlers if item.callback.__name__ == "help_command")
    regular = FakeMessage(200)
    await handler(regular)
    assert "/admin_status" not in regular.answers[0]
    admin = FakeMessage(100)
    await handler(admin)
    assert "/admin_status" in admin.answers[0]


@pytest.mark.asyncio
async def test_all_admin_message_commands_reject_regular_user(tmp_path):
    router = make_admin_router(auth(tmp_path), FakeRepo(), object(), FakeMonitor())
    protected = {"users", "requests", "approve", "deny", "revoke", "block", "unblock", "checks", "checklog", "admin_status", "alerts", "audit", "db_status", "stopall"}
    handlers = {item.callback.__name__: item.callback for item in router.message.handlers}
    assert protected <= handlers.keys()
    for name in protected:
        message = FakeMessage(200, f"/{name}")
        await handlers[name](message)
        assert message.answers == [], name


@pytest.mark.asyncio
async def test_regular_user_can_view_personal_usage(tmp_path):
    router = make_admin_router(auth(tmp_path), FakeRepo(), object(), FakeMonitor())
    handler = next(item.callback for item in router.message.handlers if item.callback.__name__ == "usage")
    message = FakeMessage(200, "/usage")
    await handler(message)
    assert message.answers[0].startswith("Your AeroAPI usage:\nToday: requests=2, successes=1, errors=1, retries=0\nThis month: requests=2, successes=1, errors=1, retries=0")


@pytest.mark.asyncio
async def test_admin_callback_rejects_regular_user(tmp_path):
    router = make_admin_router(auth(tmp_path), FakeRepo(), object(), FakeMonitor())
    handler = next(item.callback for item in router.callback_query.handlers if item.callback.__name__ == "decide")

    class Callback(FakeMessage):
        data = "access:approve:1"

        async def answer(self, text, **kwargs):
            self.answers.append(text)

    callback = Callback(200)
    await handler(callback)
    assert callback.answers == ["You are not authorized."]


@pytest.mark.asyncio
async def test_check_handler_returns_readable_upstream_error(tmp_path):
    router = make_monitoring_router(auth(tmp_path), FakeService(), FakeMonitor())
    handler = next(item.callback for item in router.message.handlers if item.callback.__name__ == "check")
    message = FakeMessage(200, "/check TFS")
    await handler(message)
    assert message.answers == ["🔒 You don't have access yet. Send /start to request access."]

    approved_auth = Auth(Settings("bot", frozenset({100, 200}), Path(tmp_path)), FakeRepo())
    router = make_monitoring_router(approved_auth, FakeService(), FakeMonitor())
    handler = next(item.callback for item in router.message.handlers if item.callback.__name__ == "check")
    message = FakeMessage(200, "/check TFS")
    await handler(message)
    assert message.answers == ["AeroAPI rejected the request. Check the airport code and try again."]


@pytest.mark.asyncio
async def test_aeroapi_test_command_uses_personal_service(tmp_path):
    approved_auth = Auth(Settings("bot", frozenset({100}), Path(tmp_path)), FakeRepo())
    router = make_monitoring_router(approved_auth, FakeService(), FakeMonitor())
    handler = next(item.callback for item in router.message.handlers if item.callback.__name__ == "aeroapi_command")
    message = FakeMessage(100, "/aeroapi test")
    await handler(message, None)
    assert message.answers == ["✅ Your AeroAPI key works. FlightAware accepted the test request."]


@pytest.mark.asyncio
async def test_fsm_input_is_rejected_after_access_is_revoked(tmp_path):
    repo = FakeRepo(status="revoked")
    settings = Settings("bot", frozenset(), Path(tmp_path))
    current_auth = Auth(settings, repo)
    router = make_monitoring_router(current_auth, FakeService(), FakeMonitor())
    handler = next(item.callback for item in router.message.handlers if item.callback.__name__ == "check_airport_input")

    class State:
        cleared = False

        async def clear(self):
            self.cleared = True

    message = FakeMessage(200, "TFS")
    state = State()
    await handler(message, state)
    assert state.cleared is True
    assert message.answers == ["🔒 You don't have access yet. Send /start to request access."]


@pytest.mark.asyncio
async def test_admin_revocation_stops_user_monitor(tmp_path):
    repo = FakeRepo(status="approved")
    current_auth = Auth(Settings("bot", frozenset({100}), Path(tmp_path)), repo)
    monitor = FakeMonitor()

    class Bot:
        async def send_message(self, *args, **kwargs):
            return None

    router = make_admin_router(current_auth, repo, Bot(), monitor)
    handler = next(item.callback for item in router.message.handlers if item.callback.__name__ == "revoke")
    message = FakeMessage(100, "/revoke 200")
    await handler(message)
    assert monitor.stopped_users == [200]


@pytest.mark.asyncio
async def test_key_handler_warns_when_telegram_delete_fails(tmp_path):
    approved_auth = Auth(Settings("bot", frozenset({100}), Path(tmp_path)), FakeRepo(status="approved"))
    router = make_monitoring_router(approved_auth, FakeService(), FakeMonitor())
    handler = next(item.callback for item in router.message.handlers if item.callback.__name__ == "aeroapi_key_input")

    class UndeletableMessage(FakeMessage):
        async def delete(self):
            raise RuntimeError("delete failed")

    class State:
        async def clear(self):
            return None

    message = UndeletableMessage(100, "")
    await handler(message, State())
    assert "Delete it manually" in message.answers[0]


def test_upstream_errors_are_user_friendly():
    assert user_facing_error(RuntimeError("AeroAPI returned HTTP 500: outage")) == "AeroAPI is temporarily unavailable. Please try again later."


def test_audit_labels_anonymous_web_actions():
    assert _audit_actor_label({"actor_user_id": None, "metadata_json": '{"source":"web"}'}) == "web"
    assert _audit_actor_label({"actor_user_id": 100, "metadata_json": None}) == "100"


@pytest.mark.asyncio
async def test_aeroapi_remove_explains_when_no_key_exists(tmp_path):
    router = make_monitoring_router(auth(tmp_path), FakeService(), FakeMonitor())
    handler = next(item.callback for item in router.message.handlers if item.callback.__name__ == "aeroapi_command")
    message = FakeMessage(100, "/aeroapi remove")
    await handler(message, None)
    assert message.answers == ["ℹ️ No AeroAPI key was configured."]


def test_format_check_tolerates_invalid_upstream_time_data():
    result = CheckResult(1, "WAW", [], [{
        "flight_id": "W61234",
        "origin": "WAW",
        "destination": "TFS",
        "scheduled_departure": "not-a-time",
        "estimated_departure": "<invalid>",
        "origin_timezone": "not/a-timezone",
        "delay_minutes": 90,
    }])
    rendered = format_check(result)
    assert "not-a-time" in rendered
    assert "&lt;invalid&gt;" in rendered
