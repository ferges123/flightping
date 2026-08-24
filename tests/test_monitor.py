import asyncio

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from flightpingbot.monitor import CheckResult, FlightService, MonitorManager, _JobState
from flightpingbot.statuses import CheckStatus


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


class SlowAero:
    async def scheduled_departures(self, airport, window_hours, api_key, *, max_attempts=4):
        await asyncio.sleep(30)
        return [], None, 0, None, 0


@pytest.mark.asyncio
async def test_check_finalizes_as_cancelled_when_task_is_cancelled():
    finished = []

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
            pass

        async def finish_check(self, check_id, *, status, **kwargs):
            finished.append(status)

    service = FlightService(RepoStub(), SlowAero(), min_delay_minutes=60, window_hours=9)
    task = asyncio.create_task(service.check(200, "TFS"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished == [CheckStatus.CANCELLED]


@pytest.mark.asyncio
async def test_stop_cancels_an_in_flight_cycle_immediately():
    repo = FakeRepo()

    class BlockingService:
        window_hours = 9
        min_delay_minutes = 60

        async def check(self, actor_user_id, airport, **kwargs):
            await asyncio.sleep(30)

    monitor = MonitorManager(BlockingService(), repo, interval_minutes=30, duration_hours=1)
    await monitor.start(100, 100, "TFS", lambda result: None)
    await asyncio.sleep(0.05)
    await asyncio.wait_for(monitor.stop_user(100, 100), timeout=2)

    assert not monitor.jobs
    assert repo.stopped


@pytest.mark.asyncio
async def test_cancel_all_is_fast_and_does_not_persist():
    repo = FakeRepo()

    class BlockingService:
        window_hours = 9
        min_delay_minutes = 60

        async def check(self, actor_user_id, airport, **kwargs):
            await asyncio.sleep(30)

    monitor = MonitorManager(BlockingService(), repo, interval_minutes=30, duration_hours=1)
    await monitor.start(100, 100, "TFS", lambda result: None)
    await asyncio.sleep(0.05)
    await asyncio.wait_for(monitor.cancel_all(), timeout=2)

    assert not monitor.jobs
    assert repo.stopped == []


class AlertRepo(FakeRepo):
    def __init__(self):
        super().__init__()
        self.removed_subscriptions = []
        self.alert_outcomes = []
        self.claims = 0

    async def claim_new_alerts(self, check_id, chat_id, flights):
        self.claims += 1
        return list(flights) if self.claims == 1 else []

    async def remove_subscription(self, job_id, user_id, chat_id):
        self.removed_subscriptions.append((job_id, user_id, chat_id))
        return True

    async def finish_alerts(self, check_id, chat_id, flights, *, sent, error=None):
        self.alert_outcomes.append((sent, error))


class AlertService:
    window_hours = 9
    min_delay_minutes = 60

    def __init__(self, delayed):
        self.delayed = delayed

    async def check(self, actor_user_id, airport, **kwargs):
        return CheckResult(check_id=1, airport=airport, flights=self.delayed, delayed=self.delayed)


@pytest.mark.asyncio
async def test_blocked_chat_drops_subscription_and_ends_job():
    repo = AlertRepo()
    delayed = [{"flight_id": "LO123", "scheduled_departure": "2026-08-24T10:00:00Z", "delay_minutes": 90}]

    async def notify(result):
        raise TelegramForbiddenError(None, "Forbidden: bot was blocked by the user")

    monitor = MonitorManager(AlertService(delayed), repo, interval_minutes=30, duration_hours=1)
    await monitor.start(100, 100, "TFS", notify)
    await asyncio.wait_for(monitor.jobs[(100, "TFS")].task, timeout=5)

    assert not monitor.jobs
    assert repo.removed_subscriptions == [(7, 100, 100)]
    assert repo.stopped == [7]
    assert repo.alert_outcomes == [(False, "Telegram server says - Forbidden: bot was blocked by the user")]


@pytest.mark.asyncio
async def test_flood_control_keeps_subscription_for_retry():
    repo = AlertRepo()
    delayed = [{"flight_id": "LO123", "scheduled_departure": "2026-08-24T10:00:00Z", "delay_minutes": 90}]

    async def notify(result):
        raise TelegramRetryAfter(None, "retry after 3", 3)

    monitor = MonitorManager(AlertService(delayed), repo, interval_minutes=1, duration_hours=1)
    await monitor.start(100, 100, "TFS", notify)
    await asyncio.sleep(0.2)
    assert (100, "TFS") in monitor.jobs
    assert (100, 100) in monitor.jobs[(100, "TFS")].callbacks
    assert repo.removed_subscriptions == []
    assert repo.alert_outcomes and "flood control" in (repo.alert_outcomes[0][1] or "")
    assert all(sent is False for sent, _ in repo.alert_outcomes)
    await monitor.stop_user_all(100)
