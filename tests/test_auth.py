import pytest

from flightpingbot.auth import Auth
from flightpingbot.config import Settings


class FakeRepo:
    def __init__(self, status=None):
        self.status = status

    async def user(self, user_id):
        return {"status": self.status} if self.status else None


def make_settings(tmp_path):
    return Settings("bot", frozenset({100}), tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id,status,approved", [(100, None, True), (200, "approved", True), (200, "pending", False), (200, "revoked", False), (200, "blocked", False)])
async def test_admin_and_user_authorization(tmp_path, user_id, status, approved):
    auth = Auth(make_settings(tmp_path), FakeRepo(status))
    assert auth.is_admin(user_id) is (user_id == 100)
    assert await auth.is_approved(user_id) is approved
