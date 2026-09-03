from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from .i18n import button_label

# Single registry of reply-keyboard layout; handlers build their F.text.in_()
# filters from the same keys, so a label can never lose its handler.
USER_BUTTON_ROWS = (
    ("btn_check", "btn_monitor"),
    ("btn_stop", "btn_status"),
    ("btn_hide", "btn_help"),
    ("btn_aeroapi", "btn_settings"),
    ("btn_favorites",),
)
ADMIN_BUTTON_ROWS = (
    ("btn_users", "btn_usage"),
    ("btn_admin_status", "btn_stop_all"),
)
ALL_BUTTON_KEYS = frozenset(key for rows in (USER_BUTTON_ROWS, ADMIN_BUTTON_ROWS) for row in rows for key in row)
DELAY_REPORT_URL = "https://fdp.mastercard.com/pekao"


def main_keyboard(is_admin: bool, language: str) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=button_label(key, language)) for key in row] for row in USER_BUTTON_ROWS]
    if is_admin:
        rows += [[KeyboardButton(text=button_label(key, language)) for key in row] for row in ADMIN_BUTTON_ROWS]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def delay_report_keyboard(language: str, has_delays: bool) -> InlineKeyboardMarkup | None:
    """Offer the external report form only for delayed-flight notifications."""
    if not has_delays:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Zgłoś" if language == "pl" else "Submit", url=DELAY_REPORT_URL),
    ]])
