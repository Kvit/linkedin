"""`webapp.settings`: the WEBAPP_* values, failing closed on auth."""

import pytest

from webapp import settings as cfg


def test_iap_audience_or_dev_user_is_required(monkeypatch):
    """Through `get_settings`: only `BaseConfig.from_env` turns pydantic's
    `ValidationError` into `ConfigError`; direct construction raises the
    former. The `delenv` lines matter: another test sets the audience and
    pytest order is not guaranteed."""
    monkeypatch.setenv("WEBAPP_ALLOWED_EMAIL", "me@example.com")
    monkeypatch.setenv("WEBAPP_OUTREACH_URL", "https://x/mcp/")
    monkeypatch.delenv("WEBAPP_IAP_AUDIENCE", raising=False)
    monkeypatch.delenv("WEBAPP_DEV_USER", raising=False)
    with pytest.raises(cfg.ConfigError) as caught:
        cfg.get_settings()
    assert "WEBAPP_IAP_AUDIENCE" in caught.value.detail


def test_dev_user_alone_is_enough_for_a_local_run():
    settings = cfg.WebappSettings(
        allowed_email="me@example.com", outreach_url="https://x/mcp/", dev_user="me@example.com", _env_file=None
    )
    assert settings.iap_audience is None
    assert settings.dev_user == "me@example.com"


def test_get_settings_reads_the_environment(monkeypatch):
    monkeypatch.setenv("WEBAPP_ALLOWED_EMAIL", "me@example.com")
    monkeypatch.setenv("WEBAPP_OUTREACH_URL", "https://x/mcp/")
    monkeypatch.setenv("WEBAPP_IAP_AUDIENCE", "/projects/1/locations/us-central1/services/linkedin-contacts")
    settings = cfg.get_settings()
    assert settings.iap_audience.endswith("/linkedin-contacts")
