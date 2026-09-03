import pytest

from flightpingbot.errors import AEROAPI_KEY_REJECTED_MESSAGE, InvalidApiKeyFormat, MissingApiKeyError, RateLimited, user_facing_error


def test_rate_limited_carries_retry_after_and_message():
    exc = RateLimited(4)
    assert exc.retry_after == 4
    assert "Please wait 4 seconds" in str(exc)
    assert user_facing_error(RateLimited(4)) == str(exc)
    assert user_facing_error(RateLimited(4), "pl") == "Poczekaj 4 sekund przed kolejnym sprawdzeniem."


@pytest.mark.parametrize("exc_type", [MissingApiKeyError, InvalidApiKeyFormat])
def test_dedicated_exceptions_are_catchable_via_base_types(exc_type):
    hierarchy = (RuntimeError, ValueError) if exc_type is not InvalidApiKeyFormat else (ValueError,)
    assert issubclass(exc_type, hierarchy)


def test_user_facing_error_uses_exception_types_not_message_sniffing():
    assert user_facing_error(MissingApiKeyError()) == "Configure your AeroAPI key first with /aeroapi."
    assert user_facing_error(MissingApiKeyError(), "pl") == "Najpierw skonfiguruj klucz AeroAPI przez /aeroapi."
    assert user_facing_error(InvalidApiKeyFormat()) == "The AeroAPI key must contain only ASCII characters and no spaces."
    # A generic RuntimeError whose text merely contains those words must not
    # be mistaken for the key-format error.
    decoy = RuntimeError("ascii art encode failed")
    assert "AeroAPI key" not in user_facing_error(decoy)


def test_key_rejected_message_is_localized_for_monitor_notifications():
    wrapped = RuntimeError(AEROAPI_KEY_REJECTED_MESSAGE)
    assert user_facing_error(wrapped) == AEROAPI_KEY_REJECTED_MESSAGE
    assert user_facing_error(wrapped, "pl") == "Autoryzacja AeroAPI nie powiodła się. Podmień klucz przez /aeroapi."


def test_user_facing_error_accepts_recorded_error_text():
    assert user_facing_error("AeroAPI returned HTTP 500: outage") == "AeroAPI is temporarily unavailable. Please try again later."


def test_user_facing_error_localizes_recorded_network_errors():
    assert user_facing_error("Read timed out", "pl") == "Usługa lotów przekroczyła limit czasu. Spróbuj ponownie później."
    assert user_facing_error("[Errno 111] Connection refused") == "The flight service is temporarily unreachable. Please try again later."
