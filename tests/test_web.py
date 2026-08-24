import httpx
import pytest

from flightpingbot.database import Database
from flightpingbot.repositories import Repository
from flightpingbot.web import WebPanel


class StubMonitor:
    duration = 6 * 3600

    def __init__(self, start_result="started"):
        self.start_result = start_result
        self.started = []

    async def start(self, actor_user_id, chat_id, airport, notify):
        if isinstance(self.start_result, Exception):
            raise self.start_result
        self.started.append((actor_user_id, airport))
        return self.start_result


class StubBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


def _client(panel: WebPanel) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=panel.app()),
        base_url="http://panel",
        follow_redirects=True,
    )


async def _make_repo(tmp_path) -> Repository:
    db = await Database(tmp_path / "test.sqlite3").connect()
    repo = Repository(db)
    await repo.sync_admins(frozenset({100}))
    await repo.upsert_access_request(200, 200, "pilot", "Pilot")
    await repo.decide_request(1, 100, True)
    return repo


@pytest.mark.asyncio
async def test_start_monitor_reports_per_user_limit_instead_of_erroring(tmp_path):
    """Regression: the per-user airport limit raises RuntimeError and must
    surface as a redirect notice, not an unhandled server error."""
    repo = await _make_repo(tmp_path)
    monitor = StubMonitor(RuntimeError("The maximum of 3 active airports has been reached."))
    panel = WebPanel(repo, monitor, StubBot(), frozenset({100}))

    async with _client(panel) as client:
        response = await client.post("/actions/monitor/start", data={"user_id": "200", "airport": "TFS"})
        body = (await response.aread()).decode()

    assert "The maximum of 3 active airports has been reached." in body


@pytest.mark.asyncio
async def test_start_monitor_rejects_unapproved_user_with_notice(tmp_path):
    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        repo = Repository(db)
        await repo.sync_admins(frozenset({100}))
        monitor = StubMonitor()
        panel = WebPanel(repo, monitor, StubBot(), frozenset({100}))

        async with _client(panel) as client:
            response = await client.post("/actions/monitor/start", data={"user_id": "999", "airport": "TFS"})
            body = (await response.aread()).decode()

        assert "Choose an approved user." in body
        assert monitor.started == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_start_monitor_adds_monitoring_for_approved_user(tmp_path):
    repo = await _make_repo(tmp_path)
    monitor = StubMonitor("started")
    panel = WebPanel(repo, monitor, StubBot(), frozenset({100}))

    async with _client(panel) as client:
        response = await client.post("/actions/monitor/start", data={"user_id": "200", "airport": "TFS"})
        body = (await response.aread()).decode()

    assert "Monitoring for TFS was added." in body
    assert monitor.started == [(200, "TFS")]


@pytest.mark.asyncio
async def test_monitoring_page_labels_planned_expiry_only_for_active_jobs(tmp_path):
    repo = await _make_repo(tmp_path)
    job_id, _ = await repo.create_monitor_job(200, 200, "TFS", 9, 30)
    await repo.stop_monitor_job(job_id)
    await repo.create_monitor_job(200, 200, "WAW", 9, 30)
    panel = WebPanel(repo, StubMonitor(), StubBot(), frozenset({100}))

    async with _client(panel) as client:
        response = await client.get("/monitoring")
        body = (await response.aread()).decode()

    assert "Planned expiry" in body
    assert "Valid until" not in body
