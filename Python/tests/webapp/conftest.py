"""Fixtures and builders every webapp test shares. Settings are constructed,
never read from the environment (`_env_file=None`), the same way
`tests/linkedinmcp` builds `OutreachSettings`."""

import pytest

from linkedinmcp import clients
from linkedinmcp.settings import OutreachSettings
from tests.linkedinmcp.fake_storage import FakeStorage
from webapp.settings import WebappSettings

ME = "me@example.com"
AUDIENCE = "/projects/1/locations/us-central1/services/linkedin-contacts"


@pytest.fixture
def anyio_backend():
    """asyncio only: anyio would also try trio, which is not installed."""
    return "asyncio"


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    """An empty bucket for every webapp test, so none can reach the real one."""
    fake = FakeStorage()
    monkeypatch.setattr(clients, "storage_client", lambda: fake)
    return fake


def webapp_settings(*, dev_user=ME, audience=None) -> WebappSettings:
    return WebappSettings(
        allowed_email=ME, outreach_url="https://outreach.example/mcp/", dev_user=dev_user,
        iap_audience=audience, _env_file=None,
    )


def outreach_settings(**overrides) -> OutreachSettings:
    """`api_key` has a 16-character floor (`linkedinmcp/settings.py`)."""
    return OutreachSettings(api_key="test-key-not-a-real-secret", tz="America/Chicago", _env_file=None, **overrides)
