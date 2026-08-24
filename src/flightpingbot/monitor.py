from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter

from .aeroapi import AeroAPI
from .errors import MissingApiKeyError, RateLimited
from .repositories import Repository
from .statuses import CheckStatus

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    check_id: int
    airport: str
    flights: list[dict]
    delayed: list[dict]
    error: str | None = None
    min_delay_minutes: int = 60
    auth_failed: bool = False


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
                raise RateLimited(remaining)
            self._last_request_at[actor_user_id] = now

    async def check(self, actor_user_id: int, airport: str, *, window_hours: int | None = None,
                    min_delay_minutes: int | None = None) -> CheckResult:
        window_hours = window_hours or self.window_hours
        min_delay_minutes = min_delay_minutes or self.min_delay_minutes
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
            raise MissingApiKeyError()
        await self._enforce_request_cooldown(actor_user_id)
        check_id = await self.repo.create_check(actor_user_id, airport, window_hours)
        try:
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
                window_hours,
                api_key,
                max_attempts=remaining_attempts if remaining_attempts is not None else 4,
            )
            for flight in flights:
                flight.setdefault("origin_timezone", airport_timezone)
            if status in {401, 403}:
                await self.repo.mark_aeroapi_key_invalid(actor_user_id)
            await self.repo.record_api_request(check_id, f"airports/{airport}/flights/scheduled_departures", status, latency, retry_number=retries, error=error)
            await self.repo.record_observations(check_id, flights, min_delay_minutes)
            delayed = [flight for flight in flights if (flight.get("delay_minutes") or 0) >= min_delay_minutes]
            await self.repo.finish_check(check_id, status=CheckStatus.ERROR if error else CheckStatus.COMPLETED, request_count=request_count + retries + 1, flight_count=len(flights), delayed_count=len(delayed), error=error)
            return CheckResult(check_id, airport, flights, delayed, error, min_delay_minutes, auth_failed=status in {401, 403})
        except asyncio.CancelledError:
            # A cancelled stop must not leave the check stuck in "running".
            await self.repo.finish_check(check_id, status=CheckStatus.CANCELLED, request_count=0, flight_count=0, delayed_count=0, error="cancelled")
            raise

    async def test_aeroapi(self, actor_user_id: int) -> int:
        api_key = await self.repo.aeroapi_key(actor_user_id)
        if not api_key:
            raise MissingApiKeyError()
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
    stop_event: asyncio.Event
    callbacks: dict[tuple[int, int], object]
    task: asyncio.Task | None = None
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

    async def start(self, actor_user_id: int, chat_id: int, airport: str, notify, *, window_hours: int | None = None,
                    interval_minutes: int | None = None, min_delay_minutes: int | None = None,
                    duration_hours: int | None = None) -> str:
        airport = airport.strip()
        if len(airport) != 3 or not airport.isascii() or not airport.isalpha():
            raise ValueError("The airport must be a three-letter IATA code.")
        airport = airport.upper()
        window_hours = window_hours or self.service.window_hours
        interval_minutes = interval_minutes or self.interval // 60
        min_delay_minutes = min_delay_minutes or getattr(self.service, "min_delay_minutes", 60)
        duration_seconds = duration_hours * 3600 if duration_hours else self.duration
        job_id, new_subscription = await self.repo.create_monitor_job(actor_user_id, chat_id, airport, window_hours, interval_minutes, self.max_active_airports, min_delay_minutes, duration_hours)
        job_key = (actor_user_id, airport)
        if job_key in self.jobs:
            self.jobs[job_key].callbacks[(actor_user_id, chat_id)] = notify
            return "subscribed" if new_subscription else "already_subscribed"
        stop_event = asyncio.Event()
        state = _JobState(job_id, airport, actor_user_id, stop_event, {(actor_user_id, chat_id): notify})
        # Register before creating the task so the run loop always sees its
        # own state in the registry.
        self.jobs[job_key] = state
        state.task = asyncio.create_task(
            self._run(job_id, airport, actor_user_id, stop_event, state, duration_seconds=duration_seconds),
            name=f"flightping-monitor-{airport}",
        )
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

    async def cancel_all(self) -> None:
        """Cancel every monitor task immediately without persisting stops.

        Used at application shutdown: active jobs are restored from SQLite on
        the next start, and cancelling avoids waiting out an in-flight AeroAPI
        cycle (up to ~100 s with retries) beyond systemd's stop timeout.
        """
        states = list(self.jobs.values())
        self.jobs.clear()
        tasks = []
        for state in states:
            state.stop_event.set()
            state.preserve_on_exit = True
            if state.task and not state.task.done():
                state.task.cancel()
                tasks.append(state.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def restore_active(self, callback_factory) -> None:
        rows = await self.repo.active_monitor_jobs()
        grouped: dict[tuple[int, str], list] = {}
        for row in rows:
            grouped.setdefault((row["actor_user_id"], row["airport"]), []).append(row)
        for (actor_user_id, airport), subscriptions in grouped.items():
            job = subscriptions[0]
            job_id = job["id"]
            stop_event = asyncio.Event()
            callbacks = {
                (row["telegram_user_id"], row["subscription_chat_id"]): callback_factory(row["telegram_user_id"], row["subscription_chat_id"])
                for row in subscriptions
            }
            state = _JobState(job_id, airport, actor_user_id, stop_event, callbacks)
            self.jobs[(actor_user_id, airport)] = state
            state.task = asyncio.create_task(
                self._run(
                    job_id, airport, actor_user_id, stop_event, state, job["started_at"],
                    window_hours=job["window_hours"],
                    interval_minutes=job["interval_minutes"],
                    min_delay_minutes=job["min_delay_minutes"],
                    duration_seconds=job["duration_hours"] * 3600 if job["duration_hours"] else None,
                ),
                name=f"flightping-monitor-{actor_user_id}-{airport}",
            )
        log.info("restored %d active monitor(s) after restart", len(grouped))

    async def _stop_job(self, job_key: tuple[int, str], state: _JobState, *, persist: bool = True) -> None:
        state.stop_event.set()
        state.preserve_on_exit = not persist
        if state.task and not state.task.done():
            # Cancel rather than wait out an in-flight cycle: a user asking to
            # stop expects it now, not after up to ~100 s of HTTP retries.
            state.task.cancel()
        try:
            await state.task
        except asyncio.CancelledError:
            pass
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

    async def _queued_check(self, actor_user_id: int, airport: str, *, window_hours: int, min_delay_minutes: int) -> CheckResult:
        queue = self._check_queues.setdefault(actor_user_id, asyncio.Lock())
        async with queue:
            while True:
                try:
                    return await self.service.check(actor_user_id, airport, window_hours=window_hours, min_delay_minutes=min_delay_minutes)
                except RateLimited as exc:
                    await asyncio.sleep(exc.retry_after)

    async def _run(self, job_id: int, airport: str, actor_user_id: int, stop_event: asyncio.Event, state: _JobState,
                   started_at: str | None = None,
                   *, window_hours: int | None = None, interval_minutes: int | None = None,
                   min_delay_minutes: int | None = None, duration_seconds: int | None = None) -> None:
        window_hours = window_hours or self.service.window_hours
        interval = (interval_minutes or self.interval // 60) * 60
        min_delay_minutes = min_delay_minutes or getattr(self.service, "min_delay_minutes", 60)
        duration = duration_seconds or self.duration
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration
        first_delay = 0.0
        if started_at:
            try:
                started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
                deadline = loop.time() + max(0.0, duration - elapsed)
                intervals_elapsed = int(elapsed // interval)
                first_delay = max(0.0, (intervals_elapsed + 1) * interval - elapsed)
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
                    result = await self._queued_check(actor_user_id, airport, window_hours=window_hours, min_delay_minutes=min_delay_minutes)
                    if stop_event.is_set():
                        break
                    current = self.jobs.get((actor_user_id, airport))
                    if current and result.auth_failed:
                        await self._notify_invalid_key(current)
                        log.warning("stopping monitor %s: AeroAPI key was rejected", job_id)
                        break
                    if current and result.delayed:
                        for (user_id, chat_id), notify in list(current.callbacks.items()):
                            if stop_event.is_set():
                                break
                            new_delayed = await self.repo.claim_new_alerts(result.check_id, chat_id, result.delayed)
                            if not new_delayed:
                                continue
                            recipient_result = CheckResult(result.check_id, result.airport, result.flights, new_delayed, result.error, result.min_delay_minutes, result.auth_failed)
                            try:
                                await notify(recipient_result)
                            except (TelegramForbiddenError, TelegramNotFound) as exc:
                                # The chat is gone or has blocked the bot.
                                # Drop it instead of polling AeroAPI for
                                # nobody until the job expires; when the last
                                # chat leaves, end the whole job below.
                                await self.repo.finish_alerts(result.check_id, chat_id, new_delayed, sent=False, error=str(exc)[:500])
                                current.callbacks.pop((user_id, chat_id), None)
                                await self.repo.remove_subscription(state.job_id, user_id, chat_id)
                                log.warning("chat %s unreachable (%s); subscription removed", chat_id, type(exc).__name__)
                            except TelegramRetryAfter as exc:
                                # Transient flood limit: keep the subscription;
                                # claim_new_alerts re-claims these ERROR alerts
                                # on the next cycle.
                                await self.repo.finish_alerts(result.check_id, chat_id, new_delayed, sent=False, error=f"telegram flood control: retry after {exc.retry_after}s")
                                log.warning("telegram flood control for chat %s; %d alert(s) will retry next cycle", chat_id, len(new_delayed))
                            except Exception as exc:
                                await self.repo.finish_alerts(result.check_id, chat_id, new_delayed, sent=False, error=str(exc)[:500])
                                log.exception("alert delivery failed for chat %s", chat_id)
                                continue
                            await self.repo.finish_alerts(result.check_id, chat_id, new_delayed, sent=True)
                    if not state.callbacks:
                        log.warning("stopping monitor %s: no reachable chats remain", job_id)
                        break
                except Exception as exc:
                    if isinstance(exc, MissingApiKeyError):
                        # The key was marked invalid by the preceding cycle. Do not
                        # keep a dead job alive for the remainder of its duration.
                        log.warning("stopping monitor %s: AeroAPI key is no longer valid", job_id)
                        current = self.jobs.get((actor_user_id, airport))
                        if current:
                            await self._notify_invalid_key(current)
                        break
                    log.exception("monitor cycle failed")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            # Only clean up our own registry entry: a newer job for the same
            # (user, airport) key may have been registered while this run was
            # finishing, and its callbacks must survive.
            if self.jobs.get((actor_user_id, airport)) is state:
                del self.jobs[(actor_user_id, airport)]
            if not state.preserve_on_exit:
                await self.repo.stop_monitor_job(job_id)
