from __future__ import annotations

import httpx

from .aeroapi import AeroAPIError


def user_facing_error(exc: Exception) -> str:
    """Turn upstream/application exceptions into short Telegram-safe text."""
    message = str(exc).lower()
    if "ascii" in message and "encode" in message:
        return "The AeroAPI key contains invalid characters. Set it again with /aeroapi."
    if isinstance(exc, AeroAPIError) or message.startswith("aeroapi returned http"):
        status_code = exc.status_code if isinstance(exc, AeroAPIError) else None
        if status_code is None:
            for code in (400, 401, 403, 429, 500, 502, 503, 504):
                if f"http {code}" in message:
                    status_code = code
                    break
        if status_code == 400:
            return "AeroAPI rejected the request. Check the airport code and try again."
        if status_code == 401 or status_code == 403:
            return "AeroAPI authorization failed. Please contact the administrator."
        if status_code == 429:
            return "AeroAPI rate limit reached. Please try again later."
        if status_code and status_code >= 500:
            return "AeroAPI is temporarily unavailable. Please try again later."
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "The flight service timed out. Please try again later."
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return "The flight service is temporarily unreachable. Please try again later."
    return str(exc) or "The request could not be completed. Please try again later."
