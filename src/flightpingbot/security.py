from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message


class InboundSecurityMiddleware(BaseMiddleware):
    """Apply a per-user inbound message limit and expire stale FSM states."""

    MAX_TRACKED_USERS = 10_000

    def __init__(self, admins: frozenset[int], messages_per_minute: int, state_ttl_seconds: int):
        self.admins = admins
        self.messages_per_minute = messages_per_minute
        self.state_ttl_seconds = state_ttl_seconds
        self._message_times: dict[int, deque[float]] = defaultdict(deque)
        self._last_rate_notice: dict[int, float] = {}

    def _forget_oldest_user(self) -> None:
        if len(self._message_times) < self.MAX_TRACKED_USERS:
            return
        oldest_user_id = next(iter(self._message_times))
        self._message_times.pop(oldest_user_id, None)
        self._last_rate_notice.pop(oldest_user_id, None)

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user
        user_id = user.id if user else event.chat.id
        text = event.text or ""
        now = time.monotonic()
        state_now = time.time()
        is_cancel = text.strip().split(maxsplit=1)[0].split("@", 1)[0] == "/cancel" if text.strip() else False

        # Keep the emergency cancellation path available even during throttling.
        if user_id not in self.admins and not is_cancel:
            if user_id not in self._message_times:
                self._forget_oldest_user()
            times = self._message_times[user_id]
            cutoff = now - 60.0
            while times and times[0] <= cutoff:
                times.popleft()
            if len(times) >= self.messages_per_minute:
                if now - self._last_rate_notice.get(user_id, 0.0) >= 60.0:
                    self._last_rate_notice[user_id] = now
                    try:
                        await event.answer("⏳ Too many messages. Please wait a moment and try again.")
                    except Exception:
                        pass
                return None
            times.append(now)

        state = data.get("state")
        current_state = await state.get_state() if state else None
        if state and current_state:
            state_data = await state.get_data()
            started = state_data.get("_flightping_state_started_at")
            if (
                started is not None
                and state_now - float(started) >= self.state_ttl_seconds
                and not is_cancel
            ):
                await state.clear()
                try:
                    await event.answer("⌛ This input session expired. Please start again.")
                except Exception:
                    pass
                return None
            # The timestamp is refreshed once, after the handler runs (see
            # below) — a pre-handler update would always be overwritten.

        result = await handler(event, data)

        # Record the start time for states created by a handler (e.g. /check).
        if state:
            new_state = await state.get_state()
            if new_state:
                await state.update_data(_flightping_state_started_at=time.time())
        return result
