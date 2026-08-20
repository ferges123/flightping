import pytest

from flightpingbot.config import ConfigError, Settings


def test_config_requires_secrets(monkeypatch):
    monkeypatch.delenv("FPB_BOT_TOKEN", raising=False)
    monkeypatch.setenv("FPB_ADMIN_USER_IDS", "123")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_config_parses_admins(monkeypatch, tmp_path):
    monkeypatch.setenv("FPB_BOT_TOKEN", "token")
    monkeypatch.setenv("FPB_ADMIN_USER_IDS", "123, 456")
    monkeypatch.setenv("FPB_CREDENTIALS_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.setenv("FPB_STATE_DIR", str(tmp_path))
    settings = Settings.from_env()
    assert settings.admin_user_ids == frozenset({123, 456})
    assert settings.database_path == tmp_path / "flightpingbot.sqlite3"
    assert settings.observation_retention_days == 7
    assert settings.web_host == "127.0.0.1"
