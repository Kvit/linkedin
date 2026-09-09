"""Settings are env-driven; api_key and dns have no defaults on purpose.

The OpenAPI spec ships a default host (api1.unipile.com:13111) that is wrong for
every real tenant and fails with 503 no_client_session, so guessing is worse than
refusing to start.
"""

import pytest

from lib.unipile.config import UnipileSettings
from lib.unipile.errors import ConfigError

REQUIRED = {"UNIPILE_API_KEY": "k-123",
            "UNIPILE_DNS": "api62.unipile.com:19262"}


def _env(monkeypatch, **overrides):
    for key in list(os_environ_keys()):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**REQUIRED, **overrides}.items():
        monkeypatch.setenv(key, value)


def os_environ_keys():
    import os

    return [k for k in os.environ if k.startswith("UNIPILE_")]


def test_missing_api_key_raises_config_error(monkeypatch):
    _env(monkeypatch)
    monkeypatch.delenv("UNIPILE_API_KEY", raising=False)

    with pytest.raises(ConfigError) as excinfo:
        UnipileSettings.from_env(env_file=None)

    assert "api_key" in str(excinfo.value).lower()


def test_base_url_is_built_from_dns(monkeypatch):
    _env(monkeypatch)

    settings = UnipileSettings.from_env(env_file=None)

    assert settings.base_url == "https://api62.unipile.com:19262"


def test_defaults_match_the_conservative_budget(monkeypatch):
    _env(monkeypatch)

    settings = UnipileSettings.from_env(env_file=None)

    assert settings.max_invites_per_day == 25
    assert settings.max_messages_per_day == 50
    assert settings.max_profile_fetches_per_day == 250
    assert settings.account_id is None


def test_profile_sections_parse_from_a_comma_separated_string(monkeypatch):
    """pydantic-settings parses list fields as JSON by default; commas must work."""
    _env(monkeypatch, UNIPILE_PROFILE_SECTIONS="about, experience,skills")

    settings = UnipileSettings.from_env(env_file=None)

    assert settings.profile_sections == ["about", "experience", "skills"]


def test_api_key_is_not_exposed_in_repr(monkeypatch):
    _env(monkeypatch)

    settings = UnipileSettings.from_env(env_file=None)

    assert "k-123" not in repr(settings)
    assert settings.api_key.get_secret_value() == "k-123"


def test_config_errors_never_carry_the_api_key(monkeypatch):
    """Pydantic prints the raw input dict before SecretStr coercion, so a
    missing-DNS error would otherwise render the key in full:

        dns Field required [input_value={'api_key': 'SECRET-KEY-123'}, ...]

    That reaches any log or unhandled traceback.
    """
    _env(monkeypatch)
    monkeypatch.delenv("UNIPILE_DNS", raising=False)
    monkeypatch.setenv("UNIPILE_API_KEY", "SECRET-KEY-123")

    with pytest.raises(ConfigError) as excinfo:
        UnipileSettings.from_env(env_file=None)

    error = excinfo.value
    exposed = " ".join(
        [str(error), repr(error), error.title,
         error.detail or "", str(error.__cause__)]
    )
    assert "SECRET-KEY-123" not in exposed
    assert "dns" in error.title, "the error must still say which field is wrong"


def test_profile_sections_also_accept_a_json_list(monkeypatch):
    _env(monkeypatch, UNIPILE_PROFILE_SECTIONS='["about", "experience"]')

    settings = UnipileSettings.from_env(env_file=None)

    assert settings.profile_sections == ["about", "experience"]


def test_pacing_settings_come_from_the_environment(monkeypatch):
    """Cadence is operational policy, so it is tuned in .env, not in code."""
    _env(
        monkeypatch,
        UNIPILE_MIN_DELAY_SECONDS="30",
        UNIPILE_MAX_DELAY_SECONDS="120",
        UNIPILE_LONG_PAUSE_EVERY="15",
        UNIPILE_LONG_PAUSE_MIN_SECONDS="300",
        UNIPILE_LONG_PAUSE_MAX_SECONDS="900",
        UNIPILE_THROTTLE_RETRIES="4",
        UNIPILE_MAX_CONSECUTIVE_THROTTLED="9",
    )

    settings = UnipileSettings.from_env(env_file=None)

    assert settings.min_delay_seconds == 30.0
    assert settings.max_delay_seconds == 120.0
    assert settings.long_pause_every == 15
    assert settings.long_pause_min_seconds == 300.0
    assert settings.long_pause_max_seconds == 900.0
    assert settings.throttle_retries == 4
    assert settings.max_consecutive_throttled == 9


def test_pacing_defaults_are_slower_than_a_script(monkeypatch):
    """The default rhythm has to look like reading, not like fetching."""
    _env(monkeypatch)

    settings = UnipileSettings.from_env(env_file=None)

    assert settings.min_delay_seconds >= 15.0
    assert settings.max_delay_seconds >= 60.0
    assert settings.long_pause_every > 0
