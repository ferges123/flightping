"""Supported per-user monitoring preferences.

Keep these values in one place: they are both persisted by the repository and
presented in Telegram setting keyboards.
"""

LANGUAGES = ("en", "pl")
WINDOW_HOURS = (3, 6, 9, 12)
INTERVAL_MINUTES = (15, 30, 45, 60)
MIN_DELAY_MINUTES = (30, 45, 60, 90, 120)
DURATION_HOURS = (3, 6, 12, 24)

OPTIONS_BY_KIND = {
    "window": WINDOW_HOURS,
    "interval": INTERVAL_MINUTES,
    "delay": MIN_DELAY_MINUTES,
    "duration": DURATION_HOURS,
}
