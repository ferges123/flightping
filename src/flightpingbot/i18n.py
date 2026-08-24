from __future__ import annotations

from .locales import EN_MESSAGES, PL_MESSAGES


MESSAGES = {"en": EN_MESSAGES, "pl": PL_MESSAGES}


def t(lang: str | None, key: str, **values) -> str:
    """Return a formatted message, falling back to English for unknown languages."""
    return MESSAGES.get(lang, EN_MESSAGES)[key].format(**values)


USER_COMMANDS = {
    "en": [
        ("start", "Request access or show access status"),
        ("check", "Check scheduled departures and delays"),
        ("monitor", "Start airport monitoring"),
        ("stop", "Stop your monitoring subscriptions"),
        ("status", "Show monitoring status"),
        ("settings", "Change language and monitoring defaults"),
        ("help", "Show help and current settings"),
        ("aeroapi", "Configure your AeroAPI key"),
        ("hide", "Hide the keyboard"),
    ],
    "pl": [
        ("start", "Poproś o dostęp lub sprawdź jego stan"),
        ("check", "Sprawdź planowane odloty i opóźnienia"),
        ("monitor", "Uruchom monitorowanie lotniska"),
        ("stop", "Zatrzymaj swoje monitorowania"),
        ("status", "Pokaż stan monitorowania"),
        ("settings", "Zmień język i domyślne parametry monitorowania"),
        ("help", "Pokaż pomoc i bieżące ustawienia"),
        ("aeroapi", "Skonfiguruj swój klucz AeroAPI"),
        ("hide", "Ukryj klawiaturę"),
    ],
}

ADMIN_COMMANDS = {
    "en": [
        ("requests", "List pending access requests"),
        ("users", "List users"),
        ("usage", "Show API usage"),
        ("admin_status", "Show system status"),
        ("alerts", "Show alerts"),
        ("audit", "Show audit events"),
        ("db_status", "Show database status"),
        ("stopall", "Stop all monitors"),
    ],
    "pl": [
        ("requests", "Lista oczekujących próśb o dostęp"),
        ("users", "Lista użytkowników"),
        ("usage", "Pokaż użycie API"),
        ("admin_status", "Pokaż status systemu"),
        ("alerts", "Pokaż alerty"),
        ("audit", "Pokaż zdarzenia audytu"),
        ("db_status", "Pokaż status bazy danych"),
        ("stopall", "Zatrzymaj wszystkie monitorowania"),
    ],
}


def telegram_commands(language: str = "en", is_admin: bool = False) -> list[tuple[str, str]]:
    """(command, description) pairs for Telegram's menu in the given language."""
    language = language if language in MESSAGES else "en"
    commands = list(USER_COMMANDS[language])
    if is_admin:
        commands += ADMIN_COMMANDS[language]
    return commands
