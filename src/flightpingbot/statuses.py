from __future__ import annotations

from enum import StrEnum


class UserStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    REVOKED = "revoked"
    BLOCKED = "blocked"


class AccessRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class MonitorJobStatus(StrEnum):
    ACTIVE = "active"
    STOPPED = "stopped"


class CheckStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class AlertStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    ERROR = "error"
    DUPLICATE = "duplicate"
