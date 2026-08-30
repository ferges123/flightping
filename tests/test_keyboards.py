from flightpingbot.i18n import MESSAGES, button_label, button_texts
from flightpingbot.keyboards import ALL_BUTTON_KEYS, main_keyboard

# Frozen snapshot: any change to an English label breaks saved keyboards on
# user devices, so it must be a conscious decision (update this list too).
EN_SNAPSHOT = {
    "btn_check": "🔎 Check",
    "btn_monitor": "▶️ Monitor",
    "btn_stop": "⏹ Stop",
    "btn_status": "📊 Status",
    "btn_hide": "✖ Hide",
    "btn_help": "❓ Help",
    "btn_aeroapi": "🔐 AeroAPI",
    "btn_settings": "⚙️ Settings",
    "btn_favorites": "⭐ Favorites",
    "btn_users": "👥 Users",
    "btn_usage": "📈 Usage",
    "btn_admin_status": "⚙️ Admin status",
    "btn_stop_all": "🛑 Stop all",
}


def test_every_button_key_exists_in_all_languages():
    for key in ALL_BUTTON_KEYS:
        for lang in ("en", "pl"):
            assert key in MESSAGES[lang], f"{key} missing in {lang}"


def test_button_labels_are_unique_across_keys_and_languages():
    owner = {}
    for lang, messages in MESSAGES.items():
        for key in ALL_BUTTON_KEYS:
            label = messages[key]
            assert owner.get(label, key) == key, f"label {label!r} ({lang}) collides with key {owner.get(label)}"
            owner[label] = key


def test_english_labels_are_stable_against_accidental_renames():
    assert {key: button_label(key, "en") for key in ALL_BUTTON_KEYS} == EN_SNAPSHOT


def test_button_texts_covers_both_languages_for_filters():
    for key in ALL_BUTTON_KEYS:
        texts = button_texts(key)
        assert texts == {button_label(key, "en"), button_label(key, "pl")}


def test_main_keyboard_renders_labels_for_requested_language():
    keyboard_en = main_keyboard(is_admin=False, language="en")
    labels_en = [button.text for row in keyboard_en.keyboard for button in row]
    assert "🔎 Check" in labels_en and "⭐ Favorites" in labels_en and len(labels_en) == 9

    keyboard_pl = main_keyboard(is_admin=False, language="pl")
    labels_pl = [button.text for row in keyboard_pl.keyboard for button in row]
    assert "🔎 Sprawdź" in labels_pl and "⚙️ Ustawienia" in labels_pl and "⭐ Ulubione" in labels_pl


def test_main_keyboard_appends_admin_rows_only_for_admins():
    user_rows = len(main_keyboard(False, "en").keyboard)
    admin_rows = len(main_keyboard(True, "en").keyboard)
    assert admin_rows == user_rows + 2
