from __future__ import annotations

import time
import asyncio
from datetime import datetime, timedelta, timezone

import httpx


class AeroAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AeroAPI:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._owned_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url="https://aeroapi.flightaware.com/aeroapi",
            headers={"accept": "application/json"},
            timeout=httpx.Timeout(20.0, connect=10.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def close(self) -> None:
        if self._owned_client:
            await self.client.aclose()

    async def validate_key(self, api_key: str) -> tuple[int | None, str | None]:
        try:
            response = await self.client.get("/account/usage", headers={"x-apikey": api_key})
            if response.status_code < 400:
                return response.status_code, None
            return response.status_code, str(AeroAPIError(f"AeroAPI returned HTTP {response.status_code}", response.status_code))
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            return None, str(exc)

    async def airport_timezone(self, airport: str, api_key: str) -> tuple[str | None, int | None, int, str | None]:
        """Return airport timezone together with accounting data for this request."""
        started = time.monotonic()
        try:
            response = await self.client.get(f"/airports/{airport}", headers={"x-apikey": api_key})
            latency = int((time.monotonic() - started) * 1000)
            if response.status_code >= 400:
                return None, response.status_code, latency, f"AeroAPI returned HTTP {response.status_code}"
            payload = response.json()
            return payload.get("timezone") or payload.get("time_zone"), response.status_code, latency, None
        except (httpx.HTTPError, ValueError) as exc:
            return None, getattr(exc, "status_code", None), int((time.monotonic() - started) * 1000), str(exc)

    async def scheduled_departures(self, airport: str, window_hours: int, api_key: str, *, max_attempts: int = 4) -> tuple[list[dict], int | None, int, str | None, int]:
        start = datetime.now(timezone.utc).replace(microsecond=0)
        end = start + timedelta(hours=window_hours)
        endpoint = f"/airports/{airport}/flights/scheduled_departures"
        params = {
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            # AeroAPI returns a cursor after its first page; include enough
            # pages for a busy airport within the configured time window.
            "max_pages": 5,
        }
        retries = 0
        started = time.monotonic()
        last_error: Exception | None = None
        max_attempts = max(1, min(4, max_attempts))
        for attempt in range(max_attempts):
            try:
                response = await self.client.get(endpoint, params=params, headers={"x-apikey": api_key})
                if response.status_code < 400:
                    latency = int((time.monotonic() - started) * 1000)
                    payload = response.json()
                    flights = [self._normalize(item) for item in payload.get("scheduled_departures", [])]
                    return flights, response.status_code, latency, None, retries
                detail = response.text[:500].replace("\n", " ")
                last_error = AeroAPIError(f"AeroAPI returned HTTP {response.status_code}: {detail}", response.status_code)
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable or attempt == max_attempts - 1:
                    break
                retries += 1
                retry_after = response.headers.get("retry-after")
                try:
                    delay = min(8.0, max(0.25, float(retry_after))) if retry_after else min(8.0, 0.5 * (2 ** (attempt)))
                except ValueError:
                    delay = min(8.0, 0.5 * (2 ** attempt))
                await asyncio.sleep(delay)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = exc
                if attempt == max_attempts - 1:
                    break
                retries += 1
                await asyncio.sleep(min(8.0, 0.5 * (2 ** attempt)))
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                break
        latency = int((time.monotonic() - started) * 1000)
        return [], getattr(last_error, "status_code", None), latency, str(last_error), retries

    @staticmethod
    def _normalize(item: dict) -> dict:
        scheduled = item.get("scheduled_out") or item.get("scheduled_departure")
        estimated = item.get("estimated_out") or item.get("estimated_departure") or scheduled
        delay = None
        if item.get("departure_delay") is not None:
            delay = max(0, int(item["departure_delay"] // 60))
        if scheduled and estimated:
            try:
                if delay is None:
                    estimated_at = datetime.fromisoformat(estimated.replace("Z", "+00:00"))
                    scheduled_at = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
                    if estimated_at.tzinfo is None:
                        estimated_at = estimated_at.replace(tzinfo=timezone.utc)
                    if scheduled_at.tzinfo is None:
                        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
                    delay = max(0, int((estimated_at - scheduled_at).total_seconds() // 60))
            except (OverflowError, TypeError, ValueError):
                pass
        origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
        destination = item.get("destination") if isinstance(item.get("destination"), dict) else {}
        return {
            "flight_id": str(item.get("ident_iata") or item.get("ident") or item.get("flight_number") or "unknown"),
            # FlightAware's web tracker uses the ICAO callsign (e.g. WZZ1411)
            # even when AeroAPI also returns the IATA number (e.g. W61411).
            "flightaware_id": str(item.get("ident") or item.get("ident_iata") or item.get("flight_number") or "unknown"),
            "origin": origin.get("code_iata") or origin.get("code"),
            "origin_name": origin.get("name"),
            "origin_timezone": origin.get("timezone"),
            "destination": destination.get("code_iata") or destination.get("code"),
            "destination_name": destination.get("name"),
            "destination_timezone": destination.get("timezone"),
            "scheduled_departure": scheduled,
            "estimated_departure": estimated,
            "delay_minutes": delay,
        }
