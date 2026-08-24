import asyncio

import pytest

from flightpingbot.monitor import CheckResult, FlightService, MonitorManager, _JobState


class FakeRepo:
    def __init__(self):
        self.stopped = []
        self.next_id = 6

    async def create_monitor_job(self, *args):
        self.next_id += 1
        return self.next_id, True

    async def stop_monitor_job(self, job_id):
        self.stopped.append(job_id)

    async def remove_subscription(self, *args):
        return True


class FakeService:
    window_hours = 9


@pytest.mark.asyncio
async def test_monitor_expires_and_closes_job():
    repo = FakeRepo()
    monitor = MonitorManager(FakeService(), repo, interval_minutes=30, duration_hours=0)
    await monitor.start(100, 100, "TFS", lambda result: None)
    await monitor.jobs[(100, "TFS")].task
    assert not monitor.active
    assert repo.stopped == [7]


@pytest.mark.asyncio
async def test_same_airport_isolated_per_user():
    repo = FakeRepo()
    monitor = MonitorManager(FakeService(), repo, interval_minutes=30, duration_hours=0, max_active_airports=2)
    assert await monitor.start(100, 100, "TFS", lambda result: None) == "started"
    assert await monitor.start(200, 200, "TFS", lambda result: None) == "started"
    assert set(monitor.jobs) == {(100, "TFS"), (200, "TFS")}
    await monitor.stop_all()


@pytest.mark.asyncio
async def test_stop_only_removes_calling_users_subscription():
    repo = FakeRepo()
    monitor = MonitorManager(FakeService(), repo, interval_minutes=30, duration_hours=1, max_active_airports=2)
    await monitor.start(100, 100, "TFS", lambda result: None)
    await monitor.start(200, 200, "TFS", lambda result: None)
    assert await monitor.stop_user(100, 100) is True
    assert (200, "TFS") in monitor.jobs
    assert (200, 200) in monitor.jobs[(200, "TFS")].callbacks
    assert await monitor.stop_user(200, 200) is True
    assert not monitor.jobs


@pytest.mark.asyncio
async def test_monitor_rejects_non_ascii_airport_code():
    monitor = MonitorManager(FakeService(), FakeRepo(), interval_minutes=30, duration_hours=1)
    with pytest.raises(ValueError):
        await monitor.start(100, 100, "ÄBC", lambda result: None)


@pytest.mark.asyncio
async def test_finished_run_keeps_a_newer_state_under_the_same_key():
    """Regression: a run that expires while a newer job for the same
    (user, airport) key was already registered must not remove it."""
    repo = FakeRepo()
    monitor = MonitorManager(FakeService(), repo, interval_minutes=30, duration_hours=0)
    stop_event = asyncio.Event()
    stop_event.set()
    new_state = _JobState(2, "TFS", 100, asyncio.Event(), {(100, 100): lambda result: None})
    monitor.jobs[(100, "TFS")] = new_state

    await monitor._run(1, "TFS", 100, stop_event, _JobState(1, "TFS", 100, stop_event, {}))

    assert monitor.jobs[(100, "TFS")] is new_state
    assert repo.stopped == [1]


@pytest.mark.asyncio
async def test_check_flags_auth_failure_from_status_code_not_error_text():
    """Regression: auth failure is detected via the auth_failed flag, not by
    sniffing 'HTTP 401' out of the error message."""

    class RepoStub:
        async def api_request_count(self, since, user_id):
            return 0

        async def aeroapi_key(self, user_id):
            return "secret"

        async def create_check(self, actor, airport, window_hours):
            return 1

        async def record_api_request(self, *args, **kwargs):
            pass

        async def record_observations(self, *args, **kwargs):
            pass

        async def mark_aeroapi_key_invalid(self, user_id):
            self.invalidated = user_id

        async def finish_check(self, *args, **kwargs):
            pass

    class AeroStub:
        async def scheduled_departures(self, airport, window_hours, api_key, *, max_attempts=4):
            return [], 401, 0, "AeroAPI returned HTTP 401: nope", 0

    service = FlightService(RepoStub(), AeroStub(), min_delay_minutes=60, window_hours=9)
    result = await service.check(200, "TFS")

    assert result.auth_failed is True
    assert result.error == "AeroAPI returned HTTP 401: nope"
