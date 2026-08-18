from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🔎 Check"), KeyboardButton(text="▶️ Monitor")],
        [KeyboardButton(text="⏹ Stop"), KeyboardButton(text="📊 Status"), KeyboardButton(text="✖ Hide"), KeyboardButton(text="❓ Help")],
        [KeyboardButton(text="🔐 AeroAPI")],
    ]
    if is_admin:
        rows.extend([
            [KeyboardButton(text="👥 Users"), KeyboardButton(text="📈 Usage")],
            [KeyboardButton(text="⚙️ Admin status"), KeyboardButton(text="🛑 Stop all")],
        ])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)
