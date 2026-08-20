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
        self._airport_timezones: dict[str, str | None] = {}

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
        daily_used = await self.repo.api_request_count(now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(), actor_user_id)
        monthly_used = await self.repo.api_request_count(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(), actor_user_id)
        if self.daily_limit and daily_used >= self.daily_limit:
            raise RuntimeError("The daily AeroAPI request limit has been reached.")
        if self.monthly_limit and monthly_used >= self.monthly_limit:
            raise RuntimeError("The monthly AeroAPI request limit has been reached.")
        api_key = await self.repo.aeroapi_key(actor_user_id)
        if not api_key:
            raise RuntimeError("Configure your AeroAPI key first with /aeroapi.")
        await self._enforce_request_cooldown(actor_user_id)
        check_id = await self.repo.create_check(actor_user_id, airport, self.window_hours)
        limits_remaining = [
            self.daily_limit - daily_used if self.daily_limit else None,
            self.monthly_limit - monthly_used if self.monthly_limit else None,
        ]
        remaining_attempts = min(limit for limit in limits_remaining if limit is not None) if any(limit is not None for limit in limits_remaining) else None
        request_count = 0
        if airport not in self._airport_timezones:
            timezone_lookup = getattr(self.aeroapi, "airport_timezone", None)
            # The timezone lookup improves presentation only. Reserve the last
            # quota slot for the actual flight check instead of exceeding a cap.
            if timezone_lookup and (remaining_attempts is None or remaining_attempts >= 2):
                timezone_result = await timezone_lookup(airport, api_key)
                if isinstance(timezone_result, tuple):
                    airport_timezone, status, latency, timezone_error = timezone_result
                    await self.repo.record_api_request(check_id, f"airports/{airport}", status, latency, error=timezone_error)
                    request_count += 1
                    if remaining_attempts is not None:
                        remaining_attempts -= 1
                else:
                    # Compatibility with injected test/dummy clients.
                    airport_timezone = timezone_result
                self._airport_timezones[airport] = airport_timezone
            else:
                self._airport_timezones[airport] = None
        airport_timezone = self._airport_timezones[airport]
        flights, status, latency, error, retries = await self.aeroapi.scheduled_departures(
            airport,
            self.window_hours,
            api_key,
            max_attempts=remaining_attempts if remaining_attempts is not None else 4,
        )
        for flight in flights:
            flight.setdefault("origin_timezone", airport_timezone)
        if status in {401, 403}:
            await self.repo.mark_aeroapi_key_invalid(actor_user_id)
        await self.repo.record_api_request(check_id, f"airports/{airport}/flights/scheduled_departures", status, latency, retry_number=retries, error=error)
        await self.repo.record_observations(check_id, flights, self.min_delay_minutes)
        delayed = [flight for flight in flights if (flight.get("delay_minutes") or 0) >= self.min_delay_minutes]
        await self.repo.finish_check(check_id, status="error" if error else "completed", request_count=request_count + retries + 1, flight_count=len(flights), delayed_count=len(delayed), error=error)
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
    preserve_on_exit: bool = False


class MonitorManager:
    def __init__(self, service: FlightService, repo: Repository, interval_minutes: int, duration_hours: int, max_active_airports: int = 3):
        self.service, self.repo = service, repo
        self.interval, self.duration = interval_minutes * 60, duration_hours * 3600
        self.max_active_airports = max_active_airports
        self.jobs: dict[tuple[int, str], _JobState] = {}
        # Cooldowns are tracked per user, so serialize that user's monitor
        # checks instead of letting simultaneous jobs reject each other.
        self._check_queues: dict[int, asyncio.Lock] = {}

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

    async def stop_user_airport(self, user_id: int, airport: str, chat_id: int | None = None) -> bool:
        """Stop one airport for a user; optionally limit removal to one chat."""
        airport = airport.strip().upper()
        state = self.jobs.get((user_id, airport))
        if not state:
            return False
        keys = [key for key in state.callbacks if key[0] == user_id and (chat_id is None or key[1] == chat_id)]
        for key in keys:
            state.callbacks.pop(key, None)
            await self.repo.remove_subscription(state.job_id, key[0], key[1])
        if keys and not state.callbacks:
            await self._stop_job((user_id, airport), state)
        return bool(keys)

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

    async def stop_all(self, *, persist: bool = True) -> None:
        for job_key, state in list(self.jobs.items()):
            await self._stop_job(job_key, state, persist=persist)

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
            task = asyncio.create_task(
                self._run(job_id, airport, actor_user_id, stop_event, subscriptions[0]["started_at"]),
                name=f"flightping-monitor-{actor_user_id}-{airport}",
            )
            self.jobs[(actor_user_id, airport)] = _JobState(job_id, airport, actor_user_id, task, stop_event, callbacks)
        log.info("restored %d active monitor(s) after restart", len(grouped))

    async def _stop_job(self, job_key: tuple[int, str], state: _JobState, *, persist: bool = True) -> None:
        state.stop_event.set()
        state.preserve_on_exit = not persist
        await state.task
        if persist:
            await self.repo.stop_monitor_job(state.job_id)
        self.jobs.pop(job_key, None)

    async def _notify_invalid_key(self, state: _JobState) -> None:
        result = CheckResult(
            check_id=state.job_id,
            airport=state.airport,
            flights=[],
            delayed=[],
            error="AeroAPI authorization failed. Please replace your key with /aeroapi.",
        )
        for (user_id, chat_id), notify in list(state.callbacks.items()):
            try:
                await notify(result)
            except Exception:
                log.exception("could not notify chat %s about invalid AeroAPI key", chat_id)

    async def _queued_check(self, actor_user_id: int, airport: str) -> CheckResult:
        queue = self._check_queues.setdefault(actor_user_id, asyncio.Lock())
        async with queue:
            while True:
                try:
                    return await self.service.check(actor_user_id, airport)
                except RuntimeError as exc:
                    message = str(exc)
                    if not message.startswith("Please wait "):
                        raise
                    try:
                        wait_seconds = max(1, int(message.split()[2]))
                    except (IndexError, ValueError):
                        wait_seconds = 1
                    await asyncio.sleep(wait_seconds)

    async def _run(self, job_id: int, airport: str, actor_user_id: int, stop_event: asyncio.Event, started_at: str | None = None) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.duration
        first_delay = 0.0
        if started_at:
            try:
                started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
                deadline = loop.time() + max(0.0, self.duration - elapsed)
                intervals_elapsed = int(elapsed // self.interval)
                first_delay = max(0.0, (intervals_elapsed + 1) * self.interval - elapsed)
            except (TypeError, ValueError):
                log.warning("invalid monitor start time for %s: %s", airport, started_at)
        if started_at and first_delay:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=min(first_delay, max(0.0, deadline - loop.time())))
            except asyncio.TimeoutError:
                pass
        try:
            while not stop_event.is_set() and loop.time() < deadline:
                try:
                    result = await self._queued_check(actor_user_id, airport)
                    if stop_event.is_set():
                        break
                    state = self.jobs.get((actor_user_id, airport))
                    if state and result.error and any(code in result.error for code in ("HTTP 401", "HTTP 403")):
                        await self._notify_invalid_key(state)
                        log.warning("stopping monitor %s: AeroAPI key was rejected", job_id)
                        break
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
                except Exception as exc:
                    if isinstance(exc, RuntimeError) and str(exc) == "Configure your AeroAPI key first with /aeroapi.":
                        # The key was marked invalid by the preceding cycle. Do not
                        # keep a dead job alive for the remainder of its duration.
                        log.warning("stopping monitor %s: AeroAPI key is no longer valid", job_id)
                        state = self.jobs.get((actor_user_id, airport))
                        if state:
                            await self._notify_invalid_key(state)
                        break
                    log.exception("monitor cycle failed")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            state = self.jobs.get((actor_user_id, airport))
            if not state or not state.preserve_on_exit:
                await self.repo.stop_monitor_job(job_id)
            self.jobs.pop((actor_user_id, airport), None)
