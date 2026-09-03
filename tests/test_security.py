from pathlib import Path
from types import SimpleNamespace

import pytest

from flightpingbot.security import InboundSecurityMiddleware


class FakeState:
    def __init__(self, current_state=None, data=None):
        self.current_state = current_state
        self.data = data or {}
        self.cleared = False

    async def get_state(self):
        return self.current_state

    async def get_data(self):
        return self.data

    async def clear(self):
        self.cleared = True
        self.current_state = None

    async def update_data(self, **values):
        self.data.update(values)


class FakeMessage:
    def __init__(self, user_id=10, text="hello"):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id)
        self.text = text
        self.answers = []

    async def answer(self, text):
        self.answers.append(text)


def test_production_env_is_explicitly_ignored():
    gitignore = Path(__file__).parents[1] / ".gitignore"
    assert "config/flightpingbot.env" in gitignore.read_text()


@pytest.mark.asyncio
async def test_rate_limit_rejects_the_next_user_message():
    middleware = InboundSecurityMiddleware(frozenset(), messages_per_minute=1, state_ttl_seconds=900)
    calls = []

    async def handler(event, data):
        calls.append(event.text)

    await middleware(handler, FakeMessage(text="first"), {})
    blocked = FakeMessage(text="second")
    assert await middleware(handler, blocked, {}) is None

    assert calls == ["first"]
    assert blocked.answers == ["⏳ Too many messages. Please wait a moment and try again."]


@pytest.mark.asyncio
async def test_cancel_bypasses_rate_limit():
    middleware = InboundSecurityMiddleware(frozenset(), messages_per_minute=1, state_ttl_seconds=900)
    calls = []

    async def handler(event, data):
        calls.append(event.text)

    await middleware(handler, FakeMessage(text="first"), {})
    await middleware(handler, FakeMessage(text="/cancel@FlightPingBot"), {})

    assert calls == ["first", "/cancel@FlightPingBot"]


@pytest.mark.asyncio
async def test_rate_limit_tracking_has_a_bounded_number_of_users():
    middleware = InboundSecurityMiddleware(frozenset(), messages_per_minute=30, state_ttl_seconds=900)
    middleware.MAX_TRACKED_USERS = 2

    async def handler(event, data):
        return None

    for user_id in (1, 2, 3):
        await middleware(handler, FakeMessage(user_id=user_id), {})
    assert set(middleware._message_times) == {2, 3}


@pytest.mark.asyncio
async def test_expired_state_is_cleared_but_cancel_remains_available(monkeypatch):
    monkeypatch.setattr("flightpingbot.security.time.time", lambda: 1000)
    middleware = InboundSecurityMiddleware(frozenset(), messages_per_minute=30, state_ttl_seconds=60)
    calls = []

    async def handler(event, data):
        calls.append(event.text)

    expired_state = FakeState("InputState:airport", {"_flightping_state_started_at": 900})
    expired_message = FakeMessage(text="WAW")
    assert await middleware(handler, expired_message, {"state": expired_state}) is None
    assert expired_state.cleared is True
    assert calls == []
    assert expired_message.answers == ["⌛ This input session expired. Please start again."]

    cancel_state = FakeState("InputState:airport", {"_flightping_state_started_at": 900})
    await middleware(handler, FakeMessage(text="/cancel"), {"state": cancel_state})
    assert cancel_state.cleared is False
    assert calls == ["/cancel"]
