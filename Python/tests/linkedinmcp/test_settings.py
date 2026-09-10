"""OutreachSettings is env-driven; `api_key` has no default on purpose.

Every test that calls `from_env` directly passes `env_file=None`, so a
developer's real `.env` in `Python/` can never leak into those results. The
one test that calls `get_settings()` instead -- which has no such parameter
and reads the default `.env` from the current directory -- `chdir`s to an
empty `tmp_path` first, for the same reason.
"""

import os

import pytest

from linkedinmcp import settings as cfg
from linkedinmcp.settings import ConfigError, OutreachSettings

REQUIRED = {"OUTREACH_API_KEY": "k-123-not-a-real-secret"}


def _env(monkeypatch, **overrides):
    """Clear every OUTREACH_* var first so a developer's shell can't leak in,
    then set the required key plus whatever this test overrides."""
    for key in [k for k in os.environ if k.startswith("OUTREACH_")]:
        monkeypatch.delenv(key, raising=False)
    for key, value in {**REQUIRED, **overrides}.items():
        monkeypatch.setenv(key, value)


def test_missing_api_key_raises_config_error(monkeypatch):
    _env(monkeypatch)
    monkeypatch.delenv("OUTREACH_API_KEY", raising=False)

    with pytest.raises(ConfigError) as excinfo:
        OutreachSettings.from_env(env_file=None)

    assert "api_key" in str(excinfo.value).lower()


def test_a_failure_in_one_field_names_that_field_and_carries_no_other_value(monkeypatch):
    """A mistyped cap reports the cap, and the key set beside it stays out of it.

    Note what this does *not* prove. Pydantic scopes each error's `input` to the
    field that failed, so a valid `api_key` could not appear in another field's
    error even with `include_input=True` -- this test cannot fail by that route
    and must not be read as proof that `_safe_detail` works.
    `test_the_refusal_never_echoes_the_rejected_key` is the one that proves that,
    because there the key is the failing field.
    """
    _env(monkeypatch, OUTREACH_API_KEY="SECRET-SENTINEL-VALUE",
         OUTREACH_MAX_TOUCHES="-5")

    with pytest.raises(ConfigError) as excinfo:
        OutreachSettings.from_env(env_file=None)

    error = excinfo.value
    exposed = " ".join([str(error), repr(error), error.title, error.detail or ""])
    assert "SECRET-SENTINEL-VALUE" not in exposed
    assert "max_touches" in error.title


def test_defaults_match_the_table(monkeypatch):
    _env(monkeypatch)

    settings = OutreachSettings.from_env(env_file=None)

    assert settings.tz == "UTC"
    assert settings.intro_daily_cap == 10
    assert settings.min_days_between_touches == 5
    assert settings.max_touches == 3
    assert settings.message_max_chars == 1200
    assert settings.allowed_link_domains == []
    assert settings.target_industries == [
        "RCM", "Pathology", "Medical Lab", "Physician Practice",
    ]
    assert settings.require_approval is False
    assert settings.allow_http_dry_run is True
    assert settings.budget_snapshot_max_age_minutes == 60
    assert settings.anthropic_webhook_signing_key is None


def test_allowed_link_domains_parses_from_csv(monkeypatch):
    _env(monkeypatch, OUTREACH_ALLOWED_LINK_DOMAINS="a.com, b.com")

    settings = OutreachSettings.from_env(env_file=None)

    assert settings.allowed_link_domains == ["a.com", "b.com"]


def test_allowed_link_domains_empty_string_gives_empty_list(monkeypatch):
    _env(monkeypatch, OUTREACH_ALLOWED_LINK_DOMAINS="")

    settings = OutreachSettings.from_env(env_file=None)

    assert settings.allowed_link_domains == []


def test_allowed_link_domains_real_list_passes_through_unchanged(monkeypatch):
    """Direct construction, bypassing the env-var string source entirely --
    exercises the validator's `isinstance(value, str)` early return, the
    branch that lets a real `list[str]` (as opposed to a CSV string) through
    untouched.
    """
    _env(monkeypatch)

    settings = OutreachSettings(api_key="k-123-not-a-real-secret",
                                 allowed_link_domains=["a.com", "b.com"])

    assert settings.allowed_link_domains == ["a.com", "b.com"]


def test_target_industries_also_parses_from_csv(monkeypatch):
    """Same `NoDecode` + splitter as `allowed_link_domains`; both fields share
    one validator, so this is the regression test for the second field."""
    _env(monkeypatch, OUTREACH_TARGET_INDUSTRIES="RCM, Pathology")

    settings = OutreachSettings.from_env(env_file=None)

    assert settings.target_industries == ["RCM", "Pathology"]


def test_a_cap_below_its_floor_raises_config_error(monkeypatch):
    _env(monkeypatch, OUTREACH_MAX_TOUCHES="-1")

    with pytest.raises(ConfigError):
        OutreachSettings.from_env(env_file=None)


def test_zero_max_touches_raises_config_error(monkeypatch):
    """`max_touches` is `ge=1`, not `ge=0`: zero would silently disable the
    per-contact touch cap rather than loudly refusing to start."""
    _env(monkeypatch, OUTREACH_MAX_TOUCHES="0")

    with pytest.raises(ConfigError):
        OutreachSettings.from_env(env_file=None)


def test_invalid_timezone_raises_config_error(monkeypatch):
    _env(monkeypatch, OUTREACH_TZ="Not/AZone")

    with pytest.raises(ConfigError) as excinfo:
        OutreachSettings.from_env(env_file=None)

    assert "tz" in str(excinfo.value).lower()


def test_valid_timezone_is_accepted(monkeypatch):
    _env(monkeypatch, OUTREACH_TZ="America/New_York")

    settings = OutreachSettings.from_env(env_file=None)

    assert settings.tz == "America/New_York"


def test_api_key_is_not_exposed_in_repr(monkeypatch):
    _env(monkeypatch, OUTREACH_API_KEY="k-secret-999-not-a-real")

    settings = OutreachSettings.from_env(env_file=None)

    assert "k-secret-999-not-a-real" not in repr(settings)
    assert settings.api_key.get_secret_value() == "k-secret-999-not-a-real"


def test_get_settings_returns_a_fresh_object_each_call(monkeypatch, tmp_path):
    """No `lru_cache` on `get_settings()`: a cached object would survive past
    the environment change below, and this test would then fail on the
    second assertion -- that is the point of it.

    `get_settings()` takes no `env_file` argument and reads the default
    `.env` from the current directory, unlike every other test in this file.
    `chdir` into an empty `tmp_path` first so a real `.env` in `Python/` --
    which might set an unrelated `OUTREACH_*` field to something invalid --
    can't turn this into a spurious `ConfigError` unrelated to what this test
    checks.
    """
    monkeypatch.chdir(tmp_path)
    _env(monkeypatch, OUTREACH_API_KEY="first-key-not-a-real-secret")
    first = cfg.get_settings()

    monkeypatch.setenv("OUTREACH_API_KEY", "second-key-not-a-real-secret")
    second = cfg.get_settings()

    assert first.api_key.get_secret_value() == "first-key-not-a-real-secret"
    assert second.api_key.get_secret_value() == "second-key-not-a-real-secret"


def test_an_empty_api_key_is_refused(monkeypatch):
    """Authentication must never fail open. `SecretStr` has no length floor of
    its own, and `hmac.compare_digest(b"", b"")` is True -- so a service that
    started with an empty key would authenticate every caller on a public URL."""
    _env(monkeypatch, OUTREACH_API_KEY="")

    with pytest.raises(ConfigError) as error:
        OutreachSettings.from_env(env_file=None)

    assert "api_key" in str(error.value.detail)


def test_a_too_short_api_key_is_refused(monkeypatch):
    """A floor against accidents -- "changeme", "test", a truncated paste --
    not a recommendation. The deploy runbook generates 43 characters."""
    _env(monkeypatch, OUTREACH_API_KEY="short-key")

    with pytest.raises(ConfigError):
        OutreachSettings.from_env(env_file=None)


def test_the_refusal_never_echoes_the_rejected_key(monkeypatch):
    """A rejected key's *length* is fine to report; its content is not.

    This is the case where `_safe_detail` earns its keep. Pydantic puts the raw
    input on the error object, and a key rejected by `min_length` is its own
    failing field -- so `include_input=True` would print the mistyped key into a
    startup log. The sentinel here is 14 characters, deliberately under the
    floor, because an over-length one would pass validation and leave this test
    asserting nothing.
    """
    _env(monkeypatch, OUTREACH_API_KEY="SHORT-SENTINEL")

    with pytest.raises(ConfigError) as excinfo:
        OutreachSettings.from_env(env_file=None)

    error = excinfo.value
    exposed = " ".join([str(error), repr(error), error.title, error.detail or ""])
    assert "SHORT-SENTINEL" not in exposed
    assert "api_key" in exposed


def test_rate_limits_are_not_declared_here(monkeypatch):
    """Messages and profile fetches per day belong to `UnipileSettings`, which
    is what `SendBudget` enforces on the call itself.

    A second copy under an `OUTREACH_` name would let this service be told one
    ceiling while a different one was applied, and the first sign of the
    disagreement would be a restricted LinkedIn account. Fails if anyone
    reintroduces one.
    """
    _env(monkeypatch)

    settings = OutreachSettings.from_env(env_file=None)

    for banned in (
        "max_messages_per_day",
        "max_profile_fetches_per_day",
        "max_profiles_per_day",
        "account_messages_ceiling_24h",
    ):
        assert not hasattr(settings, banned), (
            f"{banned} belongs to lib/unipile/config.py, not to this service"
        )
