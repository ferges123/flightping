from __future__ import annotations

from enum import StrEnum


class UserStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    REVOKED = "revoked"
    BLOCKED = "blocked"


class MonitorJobStatus(StrEnum):
    ACTIVE = "active"
    STOPPED = "stopped"
