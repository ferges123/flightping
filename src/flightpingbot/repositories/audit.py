from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .base import BaseRepository, serialized_write, utcnow


class AuditRepository(BaseRepository):
    """Append-only audit trail of administrative and user actions."""

    @serialized_write
    async def audit(self, actor_user_id: int | None, action: str, object_type: str | None = None, object_id: str | None = None, metadata: dict | None = None) -> None:
        await self.db.execute("""INSERT INTO audit_events(actor_user_id,action,object_type,object_id,metadata_json,created_at)
            VALUES(?,?,?,?,?,?)""", (actor_user_id, action, object_type, object_id, json.dumps(metadata or {}, sort_keys=True), utcnow()))
        await self.db.commit()

    async def list_audit(self, days: int = 7, limit: int = 100):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return await (await self.db.execute("SELECT * FROM audit_events WHERE created_at >= ? ORDER BY id DESC LIMIT ?", (cutoff, limit))).fetchall()
