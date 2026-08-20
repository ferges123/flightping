from cryptography.fernet import Fernet
import httpx
import pytest

from flightpingbot.aeroapi import AeroAPI
from flightpingbot.database import Database
from flightpingbot.monitor import FlightService
from flightpingbot.repositories import Repository


@pytest.mark.asyncio
async def test_personal_key_is_encrypted_and_used_by_aeroapi(tmp_path):
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers["x-apikey"]
        return httpx.Response(200, json={"usage": []})

    db = await Database(tmp_path / "credentials.sqlite3").connect()
    client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))
    try:
        repo = Repository(db, Fernet.generate_key().decode())
        await repo.sync_admins(frozenset({100}))
        await repo.set_aeroapi_key(100, "personal-key")
        stored = await (await db.execute("SELECT encrypted_api_key FROM user_aeroapi_credentials WHERE telegram_user_id=100")).fetchone()
        assert stored[0] != "personal-key"
        service = FlightService(repo, AeroAPI(client=client), 60, 9)
        assert await service.test_aeroapi(100) == 200
        assert seen["key"] == "personal-key"
    finally:
        await client.aclose()
        await db.close()


@pytest.mark.asyncio
async def test_check_has_per_user_cooldown(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scheduled_departures": []})

    db = await Database(tmp_path / "cooldown.sqlite3").connect()
    client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))
    try:
        repo = Repository(db, Fernet.generate_key().decode())
        await repo.sync_admins(frozenset({100}))
        await repo.set_aeroapi_key(100, "personal-key")
        service = FlightService(repo, AeroAPI(client=client), 60, 9, request_cooldown_seconds=60)
        await service.check(100, "TFS")
        with pytest.raises(RuntimeError, match="wait"):
            await service.check(100, "WAW")
    finally:
        await client.aclose()
        await db.close()


@pytest.mark.asyncio
async def test_api_usage_counts_timezone_lookups_and_preserves_a_strict_limit(tmp_path):
    paths = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/flights/scheduled_departures"):
            return httpx.Response(200, json={"scheduled_departures": []})
        return httpx.Response(200, json={"timezone": "Europe/Warsaw"})

    db = await Database(tmp_path / "usage.sqlite3").connect()
    client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))
    try:
        repo = Repository(db, Fernet.generate_key().decode())
        await repo.sync_admins(frozenset({100}))
        await repo.set_aeroapi_key(100, "personal-key")
        service = FlightService(repo, AeroAPI(client=client), 60, 9, daily_limit=1)
        result = await service.check(100, "TFS")
        assert result.error is None
        assert paths == ["/airports/TFS/flights/scheduled_departures"]
        assert await repo.api_request_count("1970-01-01T00:00:00+00:00", 100) == 1
        check = await (await db.execute("SELECT request_count FROM checks WHERE id=?", (result.check_id,))).fetchone()
        assert check["request_count"] == 1
    finally:
        await client.aclose()
        await db.close()


@pytest.mark.asyncio
async def test_api_usage_records_the_optional_timezone_lookup(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/flights/scheduled_departures"):
            return httpx.Response(200, json={"scheduled_departures": []})
        return httpx.Response(200, json={"timezone": "Europe/Warsaw"})

    db = await Database(tmp_path / "usage.sqlite3").connect()
    client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))
    try:
        repo = Repository(db, Fernet.generate_key().decode())
        await repo.sync_admins(frozenset({100}))
        await repo.set_aeroapi_key(100, "personal-key")
        service = FlightService(repo, AeroAPI(client=client), 60, 9, daily_limit=3)
        result = await service.check(100, "TFS")
        assert await repo.api_request_count("1970-01-01T00:00:00+00:00", 100) == 2
        check = await (await db.execute("SELECT request_count FROM checks WHERE id=?", (result.check_id,))).fetchone()
        assert check["request_count"] == 2
    finally:
        await client.aclose()
        await db.close()
