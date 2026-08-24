from __future__ import annotations

from .config import Settings
from .repositories import Repository
from .statuses import UserStatus


class Auth:
    def __init__(self, settings: Settings, repo: Repository):
        self.settings = settings
        self.repo = repo

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_user_ids

    async def is_approved(self, user_id: int) -> bool:
        user = await self.repo.user(user_id)
        return bool(user and user["status"] == UserStatus.APPROVED) or self.is_admin(user_id)

    async def request_access(self, user_id: int, chat_id: int, username: str | None, display_name: str):
        return await self.repo.upsert_access_request(user_id, chat_id, username, display_name)

