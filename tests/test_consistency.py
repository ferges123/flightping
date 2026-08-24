import asyncio

import pytest

from flightpingbot.database import Database
from flightpingbot.repositories import Repository
from flightpingbot.repositories.base import serialized_write


@pytest.mark.asyncio
async def test_failed_serialized_write_rolls_back_partial_data(tmp_path):
    """Regression: a write that fails between execute and commit must not
    leave its partial statements in the open transaction for the next writer
    to accidentally commit."""

    class FlakyRepository(Repository):
        @serialized_write
        async def flaky_write(self, user_id: int):
            await self.db.execute(
                "INSERT INTO users(telegram_user_id,chat_id,username,display_name,status,is_admin,created_at,updated_at)"
                " VALUES(?,?,?,?,'pending',0,?,?)",
                (user_id, user_id, None, "Ghost", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            raise RuntimeError("boom after execute")

    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        repo = FlakyRepository(db)
        with pytest.raises(RuntimeError, match="boom"):
            await repo.flaky_write(999)

        ghosts = await (await db.execute("SELECT COUNT(*) FROM users WHERE telegram_user_id=999")).fetchone()
        assert ghosts[0] == 0

        # A subsequent normal write must still work and must not resurrect
        # the rolled-back row.
        await repo.sync_admins(frozenset({100}))
        total = await (await db.execute("SELECT COUNT(*) FROM users")).fetchone()
        assert total[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_consistent_reads_delays_concurrent_writer(tmp_path):
    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        repo = Repository(db)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def reader():
            async with db.consistent_reads():
                entered.set()
                await release.wait()

        reader_task = asyncio.create_task(reader())
        await entered.wait()

        writer_task = asyncio.create_task(repo.create_check(200, "TFS", 9))
        await asyncio.sleep(0.05)
        # The writer waits for the read section to finish.
        assert not writer_task.done()

        release.set()
        check_id = await writer_task
        await reader_task

        checks = await (await db.execute("SELECT COUNT(*) FROM checks")).fetchone()
        assert check_id is not None and checks[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rollback_is_a_safe_noop_outside_a_transaction(tmp_path):
    db = await Database(tmp_path / "test.sqlite3").connect()
    try:
        await db.rollback()  # must not raise without an active transaction
    finally:
        await db.close()
