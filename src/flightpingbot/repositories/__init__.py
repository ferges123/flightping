from __future__ import annotations

from ..database import Database
from .alerts import AlertRepository
from .audit import AuditRepository
from .checks import CheckRepository
from .credentials import CredentialRepository
from .favorites import FavoriteAirportRepository
from .monitors import MonitorJobRepository
from .users import UserRepository


class Repository(UserRepository, AuditRepository, CheckRepository, AlertRepository, MonitorJobRepository, CredentialRepository, FavoriteAirportRepository):
    """Facade over the domain repositories.

    Keeps the single-object API consumed by the bot, services, handlers and
    the web panel, while storage logic lives in one class per domain.
    """

    def __init__(self, db: Database, credentials_key: str | None = None):
        CredentialRepository.__init__(self, db, credentials_key)
        UserRepository.__init__(self, db)
        AuditRepository.__init__(self, db)
        CheckRepository.__init__(self, db)
        AlertRepository.__init__(self, db)
        MonitorJobRepository.__init__(self, db)
        FavoriteAirportRepository.__init__(self, db)


__all__ = ["Repository"]
