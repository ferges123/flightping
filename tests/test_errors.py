import pytest

from flightpingbot.errors import InvalidApiKeyFormat, MissingApiKeyError, RateLimited, user_facing_error


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
