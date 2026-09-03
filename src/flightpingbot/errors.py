from __future__ import annotations

import httpx

from .aeroapi import AeroAPIError


class RateLimited(RuntimeError):
    """The per-user request cooldown is still active for `retry_after` seconds."""

    def __init__(self, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"Please wait {self.retry_after} seconds before starting another check.")


class MissingApiKeyError(RuntimeError):
    def __init__(self):
        super().__init__("Configure your AeroAPI key first with /aeroapi.")


class InvalidApiKeyFormat(ValueError):
    def __init__(self):
        super().__init__("The AeroAPI key must contain only ASCII characters and no spaces.")


AEROAPI_KEY_REJECTED_MESSAGE = "AeroAPI authorization failed. Please replace your key with /aeroapi."


def user_facing_error(exc: Exception | str, language: str = "en") -> str:
    """Turn upstream/application errors into short Telegram-safe text."""
    pl = language == "pl"
    if isinstance(exc, RateLimited):
        return f"Poczekaj {exc.retry_after} sekund przed kolejnym sprawdzeniem." if pl else str(exc)
    if isinstance(exc, MissingApiKeyError):
        return "Najpierw skonfiguruj klucz AeroAPI przez /aeroapi." if pl else str(exc)
    if isinstance(exc, InvalidApiKeyFormat):
        return "Klucz AeroAPI zawiera nieprawidłowe znaki. Ustaw go ponownie przez /aeroapi." if pl else str(exc)
    raw_message = str(exc)
    message = raw_message.lower()
    if message.startswith("aeroapi authorization failed"):
        return "Autoryzacja AeroAPI nie powiodła się. Podmień klucz przez /aeroapi." if pl else raw_message
    if isinstance(exc, AeroAPIError) or message.startswith("aeroapi returned http"):
        status_code = exc.status_code if isinstance(exc, AeroAPIError) else None
        if status_code is None:
            for code in (400, 401, 403, 429, 500, 502, 503, 504):
                if f"http {code}" in message:
                    status_code = code
                    break
        if status_code == 400:
            return "AeroAPI odrzuciło żądanie. Sprawdź kod lotniska i spróbuj ponownie." if pl else "AeroAPI rejected the request. Check the airport code and try again."
        if status_code == 401 or status_code == 403:
            return "Autoryzacja AeroAPI nie powiodła się. Skontaktuj się z administratorem." if pl else "AeroAPI authorization failed. Please contact the administrator."
        if status_code == 429:
            return "Osiągnięto limit żądań AeroAPI. Spróbuj ponownie później." if pl else "AeroAPI rate limit reached. Please try again later."
        if status_code and status_code >= 500:
            return "AeroAPI jest chwilowo niedostępne. Spróbuj ponownie później." if pl else "AeroAPI is temporarily unavailable. Please try again later."
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "Usługa lotów przekroczyła limit czasu. Spróbuj ponownie później." if pl else "The flight service timed out. Please try again later."
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return "Usługa lotów jest chwilowo nieosiągalna. Spróbuj ponownie później." if pl else "The flight service is temporarily unreachable. Please try again later."
    # AeroAPI request accounting stores errors as text, so the original httpx
    # exception type is not available when the result is rendered later.
    if "timed out" in message or "timeout" in message:
        return "Usługa lotów przekroczyła limit czasu. Spróbuj ponownie później." if pl else "The flight service timed out. Please try again later."
    if any(marker in message for marker in ("network", "connection refused", "connect error", "name or service not known", "remote protocol")):
        return "Usługa lotów jest chwilowo nieosiągalna. Spróbuj ponownie później." if pl else "The flight service is temporarily unreachable. Please try again later."
    if pl:
        known = {
            "The airport must be a three-letter IATA code.": "Lotnisko musi mieć trzyliterowy kod IATA.",
        }
        return known.get(raw_message, "Nie udało się wykonać żądania. Spróbuj ponownie później.")
    return raw_message or "The request could not be completed. Please try again later."
