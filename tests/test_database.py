import asyncio

import pytest

from flightpingbot.database import Database
from flightpingbot.repositories import Repository


@pytest.mark.asyncio
async def test_migration_and_duplicate_access_request(tmp_path):
    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        assert (tmp_path.stat().st_mode & 0o777) == 0o700
        assert (db.path.stat().st_mode & 0o777) == 0o600
        repo = Repository(db)
        await repo.sync_admins(frozenset({100}))
        assert (await repo.user(100))["is_admin"] == 1
        assert await repo.upsert_access_request(200, 200, "pilot", "Pilot") == ("requested", 1)
        assert await repo.upsert_access_request(200, 200, "pilot", "Pilot") == ("pending", 1)
        assert (await repo.decide_request(1, 100, True)) == (True, 200)
        assert (await repo.user(200))["status"] == "approved"
        await repo.update_user_settings(200, language="pl", window_hours=6, interval_minutes=15, min_delay_minutes=45)
        settings = await repo.user_settings(200)
        assert dict(settings) == {
            "telegram_user_id": 200,
            "language": "pl",
            "window_hours": 6,
            "interval_minutes": 15,
            "min_delay_minutes": 45,
        }
        job_id, new_subscription = await repo.create_monitor_job(200, 200, "TFS", 6, 15, min_delay_minutes=45)
        assert new_subscription is True
        active = await (await db.execute("SELECT status, min_delay_minutes FROM monitor_jobs WHERE id=?", (job_id,))).fetchone()
        assert active["status"] == "active"
        assert active["min_delay_minutes"] == 45
        assert len(await repo.active_monitor_jobs()) == 1
        await repo.recover_monitors_after_restart()
        recovered = await (await db.execute("SELECT status FROM monitor_jobs WHERE id=?", (job_id,))).fetchone()
        assert recovered["status"] == "active"
        await repo.set_user_status(200, "revoked")
        await repo.recover_monitors_after_restart()
        recovered = await (await db.execute("SELECT status FROM monitor_jobs WHERE id=?", (job_id,))).fetchone()
        assert recovered["status"] == "stopped"
        jobs = await repo.monitor_jobs()
        assert [job["id"] for job in jobs] == [job_id]
        assert (await repo.monitor_job(job_id))["status"] == "stopped"
        check_id = await repo.create_check(200, "TFS", 9)
        flight = {"flight_id": "EWG253", "scheduled_departure": "2026-08-18T17:00:00Z", "delay_minutes": 100}
        assert await repo.claim_new_alerts(check_id, 200, [flight]) == [flight]
        assert await repo.claim_new_alerts(check_id, 200, [flight]) == []
        await repo.finish_alerts(check_id, 200, [flight], sent=True)
        alert = await (await db.execute("SELECT status FROM alerts WHERE flight_id='EWG253'")).fetchone()
        assert alert["status"] == "sent"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_failed_alert_is_claimed_again_on_a_later_check(tmp_path):
    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        repo = Repository(db)
        flight = {"flight_id": "EWG253", "scheduled_departure": "2026-08-18T17:00:00Z", "delay_minutes": 100}
        first_check = await repo.create_check(200, "TFS", 9)
        assert await repo.claim_new_alerts(first_check, 200, [flight]) == [flight]
        await repo.finish_alerts(first_check, 200, [flight], sent=False, error="temporary Telegram failure")
        retry_check = await repo.create_check(200, "TFS", 9)
        assert await repo.claim_new_alerts(retry_check, 200, [flight]) == [flight]
        await repo.finish_alerts(retry_check, 200, [flight], sent=True)
        alert = await (await db.execute("SELECT check_id, status, error FROM alerts WHERE flight_id='EWG253'")).fetchone()
        assert alert["check_id"] == retry_check
        assert alert["status"] == "sent"
        assert alert["error"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_recent_checks_supports_pagination(tmp_path):
    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        repo = Repository(db)
        check_ids = [await repo.create_check(200, "TFS", 9) for _ in range(6)]

        first_page = await repo.recent_checks(limit=5)
        second_page = await repo.recent_checks(limit=5, offset=5)

        assert [row["id"] for row in first_page] == list(reversed(check_ids[1:]))
        assert [row["id"] for row in second_page] == [check_ids[0]]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_monitor_jobs_supports_pagination(tmp_path):
    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        repo = Repository(db)
        for user_id in range(1, 7):
            await repo.create_monitor_job(user_id, user_id, f"A{user_id:02}", 9, 30)

        first_page = await repo.monitor_jobs(limit=5)
        second_page = await repo.monitor_jobs(limit=5, offset=5)

        assert len(first_page) == 5
        assert len(second_page) == 1
        assert [row["id"] for row in first_page] == [6, 5, 4, 3, 2]
        assert [row["id"] for row in second_page] == [1]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_web_access_decision_has_no_impersonated_admin(tmp_path):
    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        repo = Repository(db)
        await repo.upsert_access_request(200, 200, "pilot", "Pilot")
        assert await repo.decide_request(1, None, True) == (True, 200)
        request = await (await db.execute("SELECT decided_by FROM access_requests WHERE id=1")).fetchone()
        assert request["decided_by"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_monitor_creation_obeys_the_per_user_limit(tmp_path):
    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        repo = Repository(db)

        async def create(airport):
            return await repo.create_monitor_job(200, 200, airport, 9, 30, max_active_airports=1)

        results = await asyncio.gather(create("TFS"), create("WAW"), return_exceptions=True)
        assert sum(isinstance(result, tuple) for result in results) == 1
        assert sum(isinstance(result, RuntimeError) for result in results) == 1
        active = await (await db.execute("SELECT COUNT(*) FROM monitor_jobs WHERE status='active' AND actor_user_id=200")).fetchone()
        assert active[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delayed_observations_returns_only_latest_observation_per_flight(tmp_path):
    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        repo = Repository(db)
        flight = {
            "flight_id": "W61589",
            "origin": "WAW",
            "destination": "MLA",
            "scheduled_departure": "2026-08-19T14:15:00+00:00",
            "estimated_departure": "2026-08-19T15:15:00+00:00",
            "delay_minutes": 60,
        }
        first_check = await repo.create_check(100, "WAW", 9)
        await repo.record_observation(first_check, flight, threshold=60)
        latest_check = await repo.create_check(100, "WAW", 9)
        flight["delay_minutes"] = 90
        await repo.record_observation(latest_check, flight, threshold=60)

        rows = await repo.delayed_observations()

        assert len(rows) == 1
        assert rows[0]["check_id"] == latest_check
        assert rows[0]["delay_minutes"] == 90
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_record_observations_batches_a_check_into_one_commit(tmp_path, monkeypatch):
    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        repo = Repository(db)
        check_id = await repo.create_check(100, "WAW", 9)
        commits = 0
        original_commit = db.commit

        async def count_commit():
            nonlocal commits
            commits += 1
            await original_commit()

        monkeypatch.setattr(db, "commit", count_commit)
        await repo.record_observations(check_id, [
            {"flight_id": "W61234", "scheduled_departure": "2026-08-19T14:15:00+00:00"},
            {"flight_id": "W65678", "scheduled_departure": "2026-08-19T15:15:00+00:00"},
        ], threshold=60)
        assert commits == 1
        rows = await (await db.execute("SELECT COUNT(*) FROM flight_observations WHERE check_id=?", (check_id,))).fetchone()
        assert rows[0] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_latest_migration_adds_retention_and_query_indexes(tmp_path):
    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        rows = await (await db.execute("SELECT name FROM sqlite_master WHERE type='index'")).fetchall()
        names = {row["name"] for row in rows}
        assert {"api_requests_created_at", "flight_observations_observed_at", "delayed_flight_observations_latest", "alerts_created_at", "stopped_monitor_jobs_stopped_at"} <= names
    finally:
        await db.close()
