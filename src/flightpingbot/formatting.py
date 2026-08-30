from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .monitor import CheckResult
from .errors import user_facing_error


def local_time(value, timezone_name: str = "Atlantic/Canary", *, empty: str = "—") -> str:
    """Escaped local time with the UTC offset; `empty` marker when unknown."""
    if not value:
        return escape(empty)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(ZoneInfo(timezone_name))
        return escape(f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({parsed:%H:%M} UTC)")
    except (OverflowError, TypeError, ValueError, ZoneInfoNotFoundError):
        return escape(str(value).replace("T", " ")[:64])


def _time(value: str | None, timezone_name: str, language: str = "en") -> str:
    return local_time(value, timezone_name, empty="brak danych" if language == "pl" else "not available")


def _airport(code: str | None, name: str | None) -> str:
    if not code:
        return "?"
    return f"{escape(code)} ({escape(name)})" if name else escape(code)


def flightaware_url(flight_id: str) -> str:
    return f"https://www.flightaware.com/live/flight/{quote(str(flight_id), safe='')}"


def format_check(result: CheckResult, timezone_name: str = "Atlantic/Canary", language: str = "en") -> str:
    pl = language == "pl"
    if result.error:
        title = "Nie udało się sprawdzić lotów" if pl else "Could not complete the flight check"
        return f"❌ <b>{title}</b>\n\n{escape(user_facing_error(result.error, language))}"
    if not result.delayed:
        title = "nie znaleziono dużych opóźnień" if pl else "no major delays found"
        checked = "Sprawdzone planowane odloty" if pl else "Checked"
        threshold = "Próg opóźnienia" if pl else "Delay threshold"
        return (
            f"✅ <b>{escape(result.airport)} — {title}</b>\n\n"
            f"{checked}: <b>{len(result.flights)}</b>.\n"
            f"{threshold}: {result.min_delay_minutes} min"
        )
    blocks = [
        f"⚠️ <b>{'Znaleziono opóźnienia na lotnisku' if pl else 'Delays found at'} {escape(result.airport)}</b>",
        f"{'Liczba lotów powyżej progu' if pl else 'Flight(s) exceeding the'} {result.min_delay_minutes}-{'minutowego progu' if pl else 'minute threshold'}: {len(result.delayed)}." if pl else f"{len(result.delayed)} flight(s) exceed the {result.min_delay_minutes}-minute threshold.",
    ]
    for flight in result.delayed[:10]:
        delay = flight.get("delay_minutes") or 0
        blocks.append(
            (
                "✈️ <a href=\"{url}\"><b>{flight}</b></a>\n"
                "🛫 {origin} → 🛬 {destination}\n"
                + ("🕒 Planowo: {scheduled}\n⏱️ Szacowany: {estimated}\n🚨 Opóźnienie: <b>+{delay} min</b>" if pl else "🕒 Scheduled: {scheduled}\n⏱️ Estimated: {estimated}\n🚨 Delay: <b>+{delay} min</b>")
            ).format(
                flight=escape(flight["flight_id"]),
                url=escape(flightaware_url(flight.get("flightaware_id") or flight["flight_id"]), quote=True),
                origin=_airport(flight.get("origin"), flight.get("origin_name")),
                destination=_airport(flight.get("destination"), flight.get("destination_name")),
                scheduled=_time(flight.get("scheduled_departure"), flight.get("origin_timezone") or timezone_name, language),
                estimated=_time(flight.get("estimated_departure"), flight.get("origin_timezone") or timezone_name, language),
                delay=delay,
            )
        )
    if len(result.delayed) > 10:
        blocks.append(f"…oraz {len(result.delayed) - 10} kolejnych opóźnionych lotów." if pl else f"…and {len(result.delayed) - 10} more delayed flight(s).")
    return "\n\n".join(blocks)
