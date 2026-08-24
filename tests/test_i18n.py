from flightpingbot.i18n import t


def test_t_accepts_language_as_format_value():
    """Regression: the format value `language=` must not collide with t()'s
    language parameter (broke /settings with TypeError)."""
    assert t("en", "settings_language", language="English") == "Language: English"
    assert t("pl", "settings_language", language="Polski") == "Język: Polski"


def test_t_falls_back_to_english_for_unknown_language():
    assert t("xx", "cancelled") == "↩️ Cancelled. Nothing was changed."
