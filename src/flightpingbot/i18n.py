from __future__ import annotations

from .locales import EN_MESSAGES, PL_MESSAGES


MESSAGES = {"en": EN_MESSAGES, "pl": PL_MESSAGES}


def t(lang: str | None, key: str, **values) -> str:
    """Return a formatted message, falling back to English for unknown languages."""
    return MESSAGES.get(lang, EN_MESSAGES)[key].format(**values)
