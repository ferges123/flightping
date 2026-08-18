import httpx
import pytest

from flightpingbot.aeroapi import AeroAPI


@pytest.mark.asyncio
async def test_scheduled_departures_uses_api_datetime_format():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"scheduled_departures": [{
            "fa_flight_id": "FA-1",
            "ident": "AB123",
            "departure_delay": 3660,
            "scheduled_out": "2026-08-18T10:00:00Z",
            "estimated_out": "2026-08-18T11:01:00Z",
        }]})

    client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))
    api = AeroAPI(client=client)
    try:
        flights, status, _, error, retries = await api.scheduled_departures("TFS", 9, "personal-key")
    finally:
        await client.aclose()
    assert status == 200
    assert error is None
    assert retries == 0
    assert flights[0]["delay_minutes"] == 61
    assert "." not in seen["start"]
    assert seen["start"].endswith("Z")
    assert seen["max_pages"] == "5"


@pytest.mark.asyncio
async def test_retries_rate_limit_then_succeeds():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"detail": "rate limited"})
        return httpx.Response(200, json={"scheduled_departures": []})

    client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))
    api = AeroAPI(client=client)
    try:
        flights, status, _, error, retries = await api.scheduled_departures("TFS", 9, "personal-key")
    finally:
        await client.aclose()
    assert calls == 2
    assert flights == []
    assert status == 200
    assert error is None
    assert retries == 1


@pytest.mark.asyncio
async def test_retries_timeout_then_succeeds():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("upstream timeout", request=request)
        return httpx.Response(200, json={"scheduled_departures": []})

    client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))
    api = AeroAPI(client=client)
    try:
        flights, status, _, error, retries = await api.scheduled_departures("TFS", 9, "personal-key")
    finally:
        await client.aclose()
    assert calls == 2
    assert flights == []
    assert status == 200
    assert error is None
    assert retries == 1


@pytest.mark.asyncio
async def test_bad_request_is_not_retried_and_is_recorded():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"detail": "bad airport"})

    client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))
    api = AeroAPI(client=client)
    try:
        flights, status, _, error, retries = await api.scheduled_departures("BAD", 9, "personal-key")
    finally:
        await client.aclose()
    assert calls == 1
    assert flights == []
    assert status == 400
    assert "HTTP 400" in error
    assert retries == 0


@pytest.mark.asyncio
async def test_validate_key_uses_account_usage_endpoint():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, json={"usage": []})

    client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))
    api = AeroAPI(client=client)
    try:
        status, error = await api.validate_key("personal-key")
    finally:
        await client.aclose()
    assert status == 200
    assert error is None
    assert seen["x-apikey"] == "personal-key"
