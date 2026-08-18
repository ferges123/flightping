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

    async def scheduled_departures(self, airport: str, window_hours: int, api_key: str) -> tuple[list[dict], int | None, int, str | None, int]:
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
        for attempt in range(4):
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
                if not retryable or attempt == 3:
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
                if attempt == 3:
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
                    delay = max(0, int((datetime.fromisoformat(estimated.replace("Z", "+00:00")) - datetime.fromisoformat(scheduled.replace("Z", "+00:00"))).total_seconds() // 60))
            except ValueError:
                pass
        origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
        destination = item.get("destination") if isinstance(item.get("destination"), dict) else {}
        return {
            "flight_id": str(item.get("ident_iata") or item.get("ident") or item.get("flight_number") or "unknown"),
            "origin": origin.get("code_iata") or origin.get("code"),
            "origin_name": origin.get("name"),
            "destination": destination.get("code_iata") or destination.get("code"),
            "destination_name": destination.get("name"),
            "scheduled_departure": scheduled,
            "estimated_departure": estimated,
            "delay_minutes": delay,
        }
