from pathlib import Path


def test_production_env_is_explicitly_ignored():
    gitignore = Path(__file__).parents[1] / ".gitignore"
    assert "config/flightpingbot.env" in gitignore.read_text()

