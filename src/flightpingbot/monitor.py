from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .aeroapi import AeroAPI
from .repositories import Repository

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    check_id: int
    airport: str
    flights: list[dict]
    delayed: list[dict]
    error: str | None = None
    min_delay_minutes: int = 60


class FlightService:
    def __init__(self, repo: Repository, aeroapi: AeroAPI, min_delay_minutes: int, window_hours: int, daily_limit: int = 0, monthly_limit: int = 0, request_cooldown_seconds: int = 5):
        self.repo, self.aeroapi = repo, aeroapi
        self.min_delay_minutes, self.window_hours = min_delay_minutes, window_hours
        self.daily_limit, self.monthly_limit = daily_limit, monthly_limit
        self.request_cooldown_seconds = request_cooldown_seconds
        self._last_request_at: dict[int, float] = {}
        self._request_lock = asyncio.Lock()

    async def _enforce_request_cooldown(self, actor_user_id: int) -> None:
        now = asyncio.get_running_loop().time()
        async with self._request_lock:
            previous = self._last_request_at.get(actor_user_id)
            if previous is not None and now - previous < self.request_cooldown_seconds:
                remaining = max(1, int(self.request_cooldown_seconds - (now - previous)))
                raise RuntimeError(f"Please wait {remaining} seconds before starting another check.")
            self._last_request_at[actor_user_id] = now

    async def check(self, actor_user_id: int, airport: str) -> CheckResult:
        airport = airport.strip()
        if len(airport) != 3 or not airport.isascii() or not airport.isalpha():
            raise ValueError("The airport must be a three-letter IATA code.")
        airport = airport.upper()
        now = datetime.now(timezone.utc)
        if self.daily_limit and await self.repo.api_request_count(now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(), actor_user_id) >= self.daily_limit:
            raise RuntimeError("The daily AeroAPI request limit has been reached.")
        if self.monthly_limit and await self.repo.api_request_count(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(), actor_user_id) >= self.monthly_limit:
            raise RuntimeError("The monthly AeroAPI request limit has been reached.")
        api_key = await self.repo.aeroapi_key(actor_user_id)
        if not api_key:
            raise RuntimeError("Configure your AeroAPI key first with /aeroapi.")
        await self._enforce_request_cooldown(actor_user_id)
        check_id = await self.repo.create_check(actor_user_id, airport, self.window_hours)
        flights, status, latency, error, retries = await self.aeroapi.scheduled_departures(airport, self.window_hours, api_key)
        if status in {401, 403}:
            await self.repo.mark_aeroapi_key_invalid(actor_user_id)
        await self.repo.record_api_request(check_id, f"airports/{airport}/flights/scheduled_departures", status, latency, retry_number=retries, error=error)
        for flight in flights:
            await self.repo.record_observation(check_id, flight, self.min_delay_minutes)
        delayed = [flight for flight in flights if (flight.get("delay_minutes") or 0) >= self.min_delay_minutes]
        await self.repo.finish_check(check_id, status="error" if error else "completed", request_count=1, flight_count=len(flights), delayed_count=len(delayed), error=error)
        return CheckResult(check_id, airport, flights, delayed, error, self.min_delay_minutes)

    async def test_aeroapi(self, actor_user_id: int) -> int:
        api_key = await self.repo.aeroapi_key(actor_user_id)
        if not api_key:
            raise RuntimeError("Configure your AeroAPI key first with /aeroapi.")
        status, error = await self.aeroapi.validate_key(api_key)
        if status in {401, 403}:
            await self.repo.mark_aeroapi_key_invalid(actor_user_id)
        if error:
            raise RuntimeError(error)
        return status or 200


@dataclass
class _JobState:
    job_id: int
    airport: str
    actor_user_id: int
    task: asyncio.Task
    stop_event: asyncio.Event
    callbacks: dict[tuple[int, int], object]


class MonitorManager:
    def __init__(self, service: FlightService, repo: Repository, interval_minutes: int, duration_hours: int, max_active_airports: int = 3):
        self.service, self.repo = service, repo
        self.interval, self.duration = interval_minutes * 60, duration_hours * 3600
        self.max_active_airports = max_active_airports
        self.jobs: dict[tuple[int, str], _JobState] = {}

    @property
    def active(self) -> bool:
        return bool(self.jobs)

    @property
    def airport(self) -> str:
        return ", ".join(sorted(state.airport for state in self.jobs.values()))

    async def start(self, actor_user_id: int, chat_id: int, airport: str, notify) -> str:
        airport = airport.strip()
        if len(airport) != 3 or not airport.isascii() or not airport.isalpha():
            raise ValueError("The airport must be a three-letter IATA code.")
        airport = airport.upper()
        job_id, new_subscription = await self.repo.create_monitor_job(actor_user_id, chat_id, airport, self.service.window_hours, self.interval // 60, self.max_active_airports)
        job_key = (actor_user_id, airport)
        if job_key in self.jobs:
            self.jobs[job_key].callbacks[(actor_user_id, chat_id)] = notify
            return "subscribed" if new_subscription else "already_subscribed"
        stop_event = asyncio.Event()
        task = asyncio.create_task(self._run(job_id, airport, actor_user_id, stop_event), name=f"flightping-monitor-{airport}")
        self.jobs[job_key] = _JobState(job_id, airport, actor_user_id, task, stop_event, {(actor_user_id, chat_id): notify})
        return "started"

    async def stop_user(self, user_id: int, chat_id: int) -> bool:
        changed = False
        for job_key, state in list(self.jobs.items()):
            if (user_id, chat_id) not in state.callbacks:
                continue
            state.callbacks.pop((user_id, chat_id), None)
            await self.repo.remove_subscription(state.job_id, user_id, chat_id)
            changed = True
            if not state.callbacks:
                await self._stop_job(job_key, state)
        return changed

    async def stop_user_all(self, user_id: int) -> bool:
        changed = False
        for job_key, state in list(self.jobs.items()):
            keys = [key for key in state.callbacks if key[0] == user_id]
            for key in keys:
                state.callbacks.pop(key, None)
                await self.repo.remove_subscription(state.job_id, key[0], key[1])
                changed = True
            if not state.callbacks:
                await self._stop_job(job_key, state)
        return changed

    async def stop_all(self) -> None:
        for job_key, state in list(self.jobs.items()):
            await self._stop_job(job_key, state)

    async def restore_active(self, callback_factory) -> None:
        rows = await self.repo.active_monitor_jobs()
        grouped: dict[tuple[int, str], list] = {}
        for row in rows:
            grouped.setdefault((row["actor_user_id"], row["airport"]), []).append(row)
        for (actor_user_id, airport), subscriptions in grouped.items():
            job_id = subscriptions[0]["id"]
            stop_event = asyncio.Event()
            callbacks = {
                (row["telegram_user_id"], row["subscription_chat_id"]): callback_factory(row["telegram_user_id"], row["subscription_chat_id"])
                for row in subscriptions
            }
            task = asyncio.create_task(self._run(job_id, airport, actor_user_id, stop_event), name=f"flightping-monitor-{actor_user_id}-{airport}")
            self.jobs[(actor_user_id, airport)] = _JobState(job_id, airport, actor_user_id, task, stop_event, callbacks)

    async def _stop_job(self, job_key: tuple[int, str], state: _JobState) -> None:
        state.stop_event.set()
        await state.task
        await self.repo.stop_monitor_job(state.job_id)
        self.jobs.pop(job_key, None)

    async def _run(self, job_id: int, airport: str, actor_user_id: int, stop_event: asyncio.Event) -> None:
        deadline = asyncio.get_running_loop().time() + self.duration
        try:
            while not stop_event.is_set() and asyncio.get_running_loop().time() < deadline:
                try:
                    result = await self.service.check(actor_user_id, airport)
                    if stop_event.is_set():
                        break
                    state = self.jobs.get((actor_user_id, airport))
                    if state and result.delayed:
                        for (user_id, chat_id), notify in list(state.callbacks.items()):
                            if stop_event.is_set():
                                break
                            new_delayed = await self.repo.claim_new_alerts(result.check_id, chat_id, result.delayed)
                            if not new_delayed:
                                continue
                            recipient_result = CheckResult(result.check_id, result.airport, result.flights, new_delayed, result.error, result.min_delay_minutes)
                            try:
                                await notify(recipient_result)
                            except Exception as exc:
                                await self.repo.finish_alerts(result.check_id, chat_id, new_delayed, sent=False, error=str(exc)[:500])
                                log.exception("alert delivery failed for chat %s", chat_id)
                                continue
                            await self.repo.finish_alerts(result.check_id, chat_id, new_delayed, sent=True)
                except Exception:
                    log.exception("monitor cycle failed")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.repo.stop_monitor_job(job_id)
            self.jobs.pop((actor_user_id, airport), None)
