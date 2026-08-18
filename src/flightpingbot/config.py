from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigError(ValueError):
    pass


def _int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_user_ids: frozenset[int]
    state_dir: Path
    monitor_interval_minutes: int = 30
    monitor_window_hours: int = 9
    monitor_duration_hours: int = 6
    min_delay_minutes: int = 60
    max_active_airports: int = 3
    observation_retention_days: int = 30
    audit_retention_days: int = 180
    api_request_retention_days: int = 90
    daily_api_request_limit: int = 0
    monthly_api_request_limit: int = 0
    usage_warning_percent: int = 80
    timezone_name: str = "Atlantic/Canary"
    credentials_key: str = ""
    user_request_cooldown_seconds: int = 5

    @property
    def database_path(self) -> Path:
        return self.state_dir / "flightpingbot.sqlite3"

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> "Settings":
        if env_file:
            load_dotenv(env_file, override=False)
        token = os.getenv("FPB_BOT_TOKEN", "").strip()
        if not token or token == "replace_me":
            raise ConfigError("FPB_BOT_TOKEN is required")
        raw_admins = os.getenv("FPB_ADMIN_USER_IDS", "").strip()
        if not raw_admins:
            raise ConfigError("FPB_ADMIN_USER_IDS must contain at least one ID")
        try:
            admins = frozenset(int(item.strip()) for item in raw_admins.split(",") if item.strip())
        except ValueError as exc:
            raise ConfigError("FPB_ADMIN_USER_IDS must be comma-separated integers") from exc
        if not admins or any(value <= 0 for value in admins):
            raise ConfigError("FPB_ADMIN_USER_IDS must contain positive IDs")
        credentials_key = os.getenv("FPB_CREDENTIALS_KEY", "").strip()
        if not credentials_key or credentials_key == "replace_me":
            raise ConfigError("FPB_CREDENTIALS_KEY is required")
        state_dir = Path(os.getenv("FPB_STATE_DIR", "/opt/flightping/state"))
        warning = _int("FPB_USAGE_WARNING_PERCENT", 80)
        if warning > 100:
            raise ConfigError("FPB_USAGE_WARNING_PERCENT must be <= 100")
        timezone_name = os.getenv("FPB_TIMEZONE", "Atlantic/Canary").strip()
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"FPB_TIMEZONE is not a valid IANA timezone: {timezone_name}") from exc
        return cls(
            token, admins, state_dir,
            _int("FPB_MONITOR_INTERVAL_MINUTES", 30, minimum=1),
            _int("FPB_MONITOR_WINDOW_HOURS", 9, minimum=1),
            _int("FPB_MONITOR_DURATION_HOURS", 6, minimum=1),
            _int("FPB_MIN_DELAY_MINUTES", 60),
            _int("FPB_MAX_ACTIVE_AIRPORTS", 3, minimum=1),
            _int("FPB_FLIGHT_OBSERVATION_RETENTION_DAYS", 30, minimum=1),
            _int("FPB_AUDIT_RETENTION_DAYS", 180, minimum=1),
            _int("FPB_API_REQUEST_RETENTION_DAYS", 90, minimum=1),
            _int("FPB_DAILY_API_REQUEST_LIMIT", 0),
            _int("FPB_MONTHLY_API_REQUEST_LIMIT", 0),
            warning,
            timezone_name,
            credentials_key,
            _int("FPB_USER_REQUEST_COOLDOWN_SECONDS", 5),
        )
