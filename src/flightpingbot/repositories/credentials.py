from __future__ import annotations

from ..credentials import CredentialCipher
from ..errors import InvalidApiKeyFormat
from .base import BaseRepository, serialized_write, utcnow


class CredentialRepository(BaseRepository):
    """Encrypted AeroAPI keys owned by individual users."""

    def __init__(self, db, credentials_key: str | None = None):
        super().__init__(db)
        self.credentials = CredentialCipher(credentials_key) if credentials_key else None

    @serialized_write
    async def set_aeroapi_key(self, user_id: int, api_key: str) -> None:
        if not self.credentials:
            raise RuntimeError("AeroAPI credential encryption is not configured")
        if not api_key or not api_key.isascii() or any(character.isspace() for character in api_key):
            raise InvalidApiKeyFormat()
        now = utcnow()
        encrypted = self.credentials.encrypt(api_key)
        await self.db.execute("""INSERT INTO user_aeroapi_credentials
            (telegram_user_id, encrypted_api_key, key_suffix, created_at, updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(telegram_user_id) DO UPDATE SET
            encrypted_api_key=?, key_suffix=?, updated_at=?, needs_reauth=0, invalid_at=NULL""",
            (user_id, encrypted, api_key[-4:], now, now, encrypted, api_key[-4:], now))
        await self.db.commit()

    async def aeroapi_key(self, user_id: int) -> str | None:
        if not self.credentials:
            return None
        row = await (await self.db.execute("SELECT encrypted_api_key, needs_reauth FROM user_aeroapi_credentials WHERE telegram_user_id=?", (user_id,))).fetchone()
        if row and row[1]:
            return None
        return self.credentials.decrypt(row[0]) if row else None

    async def aeroapi_key_suffix(self, user_id: int) -> str | None:
        row = await (await self.db.execute("SELECT key_suffix, needs_reauth FROM user_aeroapi_credentials WHERE telegram_user_id=?", (user_id,))).fetchone()
        return f"invalid:{row[0]}" if row and row[1] else (row[0] if row else None)

    @serialized_write
    async def mark_aeroapi_key_invalid(self, user_id: int) -> None:
        await self.db.execute("UPDATE user_aeroapi_credentials SET needs_reauth=1, invalid_at=?, updated_at=? WHERE telegram_user_id=?", (utcnow(), utcnow(), user_id))
        await self.db.commit()

    @serialized_write
    async def remove_aeroapi_key(self, user_id: int) -> bool:
        cursor = await self.db.execute("DELETE FROM user_aeroapi_credentials WHERE telegram_user_id=?", (user_id,))
        await self.db.commit()
        return cursor.rowcount == 1
