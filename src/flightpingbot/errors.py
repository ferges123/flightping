from __future__ import annotations

import httpx

from .aeroapi import AeroAPIError


def user_facing_error(exc: Exception, language: str = "en") -> str:
    """Turn upstream/application exceptions into short Telegram-safe text."""
    message = str(exc).lower()
    if "ascii" in message and "encode" in message:
        return "Klucz AeroAPI zawiera nieprawidłowe znaki. Ustaw go ponownie przez /aeroapi." if language == "pl" else "The AeroAPI key contains invalid characters. Set it again with /aeroapi."
    if isinstance(exc, AeroAPIError) or message.startswith("aeroapi returned http"):
        status_code = exc.status_code if isinstance(exc, AeroAPIError) else None
        if status_code is None:
            for code in (400, 401, 403, 429, 500, 502, 503, 504):
                if f"http {code}" in message:
                    status_code = code
                    break
        if status_code == 400:
            return "AeroAPI odrzuciło żądanie. Sprawdź kod lotniska i spróbuj ponownie." if language == "pl" else "AeroAPI rejected the request. Check the airport code and try again."
        if status_code == 401 or status_code == 403:
            return "Autoryzacja AeroAPI nie powiodła się. Skontaktuj się z administratorem." if language == "pl" else "AeroAPI authorization failed. Please contact the administrator."
        if status_code == 429:
            return "Osiągnięto limit żądań AeroAPI. Spróbuj ponownie później." if language == "pl" else "AeroAPI rate limit reached. Please try again later."
        if status_code and status_code >= 500:
            return "AeroAPI jest chwilowo niedostępne. Spróbuj ponownie później." if language == "pl" else "AeroAPI is temporarily unavailable. Please try again later."
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "Usługa lotów przekroczyła limit czasu. Spróbuj ponownie później." if language == "pl" else "The flight service timed out. Please try again later."
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return "Usługa lotów jest chwilowo nieosiągalna. Spróbuj ponownie później." if language == "pl" else "The flight service is temporarily unreachable. Please try again later."
    if language == "pl":
        known = {
            "The airport must be a three-letter IATA code.": "Lotnisko musi mieć trzyliterowy kod IATA.",
            "Configure your AeroAPI key first with /aeroapi.": "Najpierw skonfiguruj klucz AeroAPI przez /aeroapi.",
        }
        return known.get(str(exc), "Nie udało się wykonać żądania. Spróbuj ponownie później.")
    return str(exc) or "The request could not be completed. Please try again later."
