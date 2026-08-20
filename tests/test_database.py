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
        job_id, new_subscription = await repo.create_monitor_job(200, 200, "TFS", 9, 30)
        assert new_subscription is True
        active = await (await db.execute("SELECT status FROM monitor_jobs WHERE id=?", (job_id,))).fetchone()
        assert active["status"] == "active"
        assert len(await repo.active_monitor_jobs()) == 1
        await repo.recover_monitors_after_restart()
        recovered = await (await db.execute("SELECT status FROM monitor_jobs WHERE id=?", (job_id,))).fetchone()
        assert recovered["status"] == "active"
        await repo.set_user_status(200, "revoked")
        await repo.recover_monitors_after_restart()
        recovered = await (await db.execute("SELECT status FROM monitor_jobs WHERE id=?", (job_id,))).fetchone()
        assert recovered["status"] == "stopped"
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
