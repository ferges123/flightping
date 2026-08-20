from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .monitor import CheckResult
from .errors import user_facing_error


def _time(value: str | None, timezone_name: str) -> str:
    if not value:
        return "not available"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(ZoneInfo(timezone_name))
        return f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({parsed:%H:%M} UTC)"
    except (OverflowError, TypeError, ValueError, ZoneInfoNotFoundError):
        return escape(str(value).replace("T", " ")[:64])


def _airport(code: str | None, name: str | None) -> str:
    if not code:
        return "?"
    return f"{escape(code)} ({escape(name)})" if name else escape(code)


def flightaware_url(flight_id: str) -> str:
    return f"https://www.flightaware.com/live/flight/{quote(str(flight_id), safe='')}"


def format_check(result: CheckResult, timezone_name: str = "Atlantic/Canary") -> str:
    if result.error:
        return f"❌ <b>Could not complete the flight check</b>\n\n{escape(user_facing_error(RuntimeError(result.error)))}"
    if not result.delayed:
        return (
            f"✅ <b>{escape(result.airport)} — no major delays found</b>\n\n"
            f"Checked <b>{len(result.flights)}</b> scheduled departure(s).\n"
            f"Delay threshold: {result.min_delay_minutes} min"
        )
    blocks = [
        f"⚠️ <b>Delays found at {escape(result.airport)}</b>",
        f"{len(result.delayed)} flight(s) exceed the {result.min_delay_minutes}-minute threshold.",
    ]
    for flight in result.delayed[:10]:
        delay = flight.get("delay_minutes") or 0
        blocks.append(
            "✈️ <a href=\"{url}\"><b>{flight}</b></a>\n"
            "🛫 {origin} → 🛬 {destination}\n"
            "🕒 Scheduled: {scheduled}\n"
            "⏱️ Estimated: {estimated}\n"
            "🚨 Delay: <b>+{delay} min</b>".format(
                flight=escape(flight["flight_id"]),
                url=escape(flightaware_url(flight.get("flightaware_id") or flight["flight_id"]), quote=True),
                origin=_airport(flight.get("origin"), flight.get("origin_name")),
                destination=_airport(flight.get("destination"), flight.get("destination_name")),
                scheduled=_time(flight.get("scheduled_departure"), flight.get("origin_timezone") or timezone_name),
                estimated=_time(flight.get("estimated_departure"), flight.get("origin_timezone") or timezone_name),
                delay=delay,
            )
        )
    if len(result.delayed) > 10:
        blocks.append(f"…and {len(result.delayed) - 10} more delayed flight(s).")
    return "\n\n".join(blocks)
