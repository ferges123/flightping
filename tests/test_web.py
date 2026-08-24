import httpx
import pytest

from flightpingbot.database import Database
from flightpingbot.repositories import Repository
from flightpingbot.web import WebPanel


class StubMonitor:
    duration = 6 * 3600

    def __init__(self, start_result="started", repo=None):
        self.start_result = start_result
        self.repo = repo
        self.started = []
        self.stopped_airports = []
        self.stopped_users = []

    async def start(self, actor_user_id, chat_id, airport, notify):
        if isinstance(self.start_result, Exception):
            raise self.start_result
        self.started.append((actor_user_id, airport))
        return self.start_result

    async def stop_user_airport(self, user_id, airport, chat_id=None):
        """Mirror MonitorManager: stopping also persists the job as stopped."""
        self.stopped_airports.append((user_id, airport))
        if self.repo:
            for row in await self.repo.active_monitor_jobs():
                if row["actor_user_id"] == user_id and row["airport"] == airport:
                    await self.repo.stop_monitor_job(row["id"])
        return True

    async def stop_user_all(self, user_id):
        self.stopped_users.append(user_id)
        return True


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

    try:
        async with _client(panel) as client:
            response = await client.post("/actions/monitor/start", data={"user_id": "200", "airport": "TFS"})
            body = (await response.aread()).decode()
    finally:
        await repo.db.close()

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

    try:
        async with _client(panel) as client:
            response = await client.post("/actions/monitor/start", data={"user_id": "200", "airport": "TFS"})
            body = (await response.aread()).decode()
    finally:
        await repo.db.close()

    assert "Monitoring for TFS was added." in body
    assert monitor.started == [(200, "TFS")]


@pytest.mark.asyncio
async def test_monitoring_page_labels_planned_expiry_only_for_active_jobs(tmp_path):
    repo = await _make_repo(tmp_path)
    job_id, _ = await repo.create_monitor_job(200, 200, "TFS", 9, 30)
    await repo.stop_monitor_job(job_id)
    await repo.create_monitor_job(200, 200, "WAW", 9, 30)
    panel = WebPanel(repo, StubMonitor(), StubBot(), frozenset({100}))

    try:
        async with _client(panel) as client:
            response = await client.get("/monitoring")
            body = (await response.aread()).decode()
    finally:
        await repo.db.close()

    assert "Planned expiry" in body
    assert "Valid until" not in body
    assert f"<td>—</td><td><form method='post' action='/actions/monitor/{job_id}/remonitor'>" in body


@pytest.mark.asyncio
async def test_monitoring_page_caps_an_unreasonably_large_page_number(tmp_path):
    repo = await _make_repo(tmp_path)
    panel = WebPanel(repo, StubMonitor(), StubBot(), frozenset({100}))

    try:
        async with _client(panel) as client:
            response = await client.get("/monitoring?page=999999999999999999")
            body = (await response.aread()).decode()
    finally:
        await repo.db.close()

    assert response.status_code == 200
    assert "?page=100000" in body


@pytest.mark.asyncio
async def test_stop_monitor_action_stops_job_and_writes_audit(tmp_path):
    repo = await _make_repo(tmp_path)
    job_id, _ = await repo.create_monitor_job(200, 200, "TFS", 9, 30)
    monitor = StubMonitor(repo=repo)
    panel = WebPanel(repo, monitor, StubBot(), frozenset({100}))

    try:
        async with _client(panel) as client:
            response = await client.post(f"/actions/monitor/200/TFS/stop")
            await response.aread()
            job = await repo.monitor_job(job_id)
            audits = await repo.list_audit(days=1)
    finally:
        await repo.db.close()

    assert response.status_code == 200
    assert job["status"] == "stopped"
    assert monitor.stopped_airports == [(200, "TFS")]
    assert any(row["action"] == "monitor_stop" for row in audits)


@pytest.mark.asyncio
async def test_remonitor_action_restarts_a_stopped_job(tmp_path):
    repo = await _make_repo(tmp_path)
    stopped_id, _ = await repo.create_monitor_job(200, 200, "TFS", 9, 30)
    await repo.stop_monitor_job(stopped_id)
    active_id, _ = await repo.create_monitor_job(200, 200, "WAW", 9, 30)
    monitor = StubMonitor("started")
    panel = WebPanel(repo, monitor, StubBot(), frozenset({100}))

    try:
        async with _client(panel) as client:
            good = await client.post(f"/actions/monitor/{stopped_id}/remonitor")
            good_body = (await good.aread()).decode()
            bad = await client.post(f"/actions/monitor/{active_id}/remonitor")
            bad_body = (await bad.aread()).decode()
    finally:
        await repo.db.close()

    assert "Monitoring for TFS was restarted." in good_body
    assert monitor.started == [(200, "TFS")]
    assert "no longer available to restart" in bad_body


@pytest.mark.asyncio
async def test_panel_monitor_notifications_follow_the_user_language(tmp_path):
    from flightpingbot.monitor import CheckResult

    repo = await _make_repo(tmp_path)
    await repo.update_user_settings(200, language="pl")

    class NotifyingStub(StubMonitor):
        async def start(self, actor_user_id, chat_id, airport, notify):
            self.started.append((actor_user_id, airport))
            await notify(CheckResult(check_id=1, airport=airport, flights=[], delayed=[]))
            return "started"

    bot = StubBot()
    panel = WebPanel(repo, NotifyingStub("started"), bot, frozenset({100}))

    try:
        async with _client(panel) as client:
            response = await client.post("/actions/monitor/start", data={"user_id": "200", "airport": "TFS"})
            await response.aread()
    finally:
        await repo.db.close()

    assert bot.sent and "nie znaleziono dużych opóźnień" in bot.sent[0][1]


@pytest.mark.asyncio
async def test_user_status_action_blocks_user_and_stops_monitors(tmp_path):
    repo = await _make_repo(tmp_path)
    monitor = StubMonitor()
    bot = StubBot()
    panel = WebPanel(repo, monitor, bot, frozenset({100}))

    try:
        async with _client(panel) as client:
            blocked = await client.post("/actions/user/200/blocked")
            await blocked.aread()
            user = await repo.user(200)
            invalid = await client.post("/actions/user/200/nonsense")
            await invalid.aread()
            after = await repo.user(200)
    finally:
        await repo.db.close()

    assert user["status"] == "blocked"
    assert monitor.stopped_users == [200]
    assert after["status"] == "blocked"


@pytest.mark.asyncio
async def test_access_decision_action_approves_and_notifies(tmp_path):
    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        repo = Repository(db)
        await repo.sync_admins(frozenset({100}))
        _, request_id = await repo.upsert_access_request(300, 300, "copilot", "Copilot")
        monitor = StubMonitor()
        bot = StubBot()
        panel = WebPanel(repo, monitor, bot, frozenset({100}))

        async with _client(panel) as client:
            approve = await client.post(f"/actions/access/{request_id}/approve")
            await approve.aread()
            approved = await repo.user(300)
            deny_again = await client.post(f"/actions/access/{request_id}/deny")
            await deny_again.aread()
            still_approved = await repo.user(300)

        assert approved["status"] == "approved"
        assert any(chat == 300 and "Access granted" in text for chat, text in bot.sent)
        assert still_approved["status"] == "approved"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_users_page_lists_pending_actions_without_per_user_queries(tmp_path):
    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        repo = Repository(db)
        await repo.sync_admins(frozenset({100}))
        for user_id in (301, 302):
            await repo.upsert_access_request(user_id, user_id, f"user{user_id}", f"User {user_id}")
        pending = await repo.pending_request_ids()
        assert len(pending) == 2

        monitor = StubMonitor()
        panel = WebPanel(repo, monitor, StubBot(), frozenset({100}))
        async with _client(panel) as client:
            response = await client.get("/users")
            body = (await response.aread()).decode()

        assert "/actions/access/" in body
        assert body.count(">Approve<") == 2
    finally:
        await db.close()
