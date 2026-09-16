"""Environment-driven settings for the contacts webapp (`WEBAPP_*`).

Everything else the webapp needs -- the outreach API key, the time zone,
the message templates directory, the message rules -- is read from
`linkedinmcp.settings.OutreachSettings`, the same values the service runs
with. Import that module, not its names (`from linkedinmcp import settings
as cfg`), for the reason its docstring gives.
"""

from typing import ClassVar

from pydantic import model_validator
from pydantic_settings import SettingsConfigDict

from lib.config import BaseConfig, ConfigError

__all__ = ["ConfigError", "WebappSettings", "get_settings"]


class WebappSettings(BaseConfig):
    """All `WEBAPP_*` configuration, validated once at startup."""

    model_config = SettingsConfigDict(env_prefix="WEBAPP_", env_file_encoding="utf-8", extra="ignore")

    subject: ClassVar[str] = "webapp settings"

    #: The one Google account allowed in. Checked against the IAP assertion
    #: on every request; a valid assertion for anyone else is a 403.
    allowed_email: str

    #: The outreach service's MCP endpoint, `OUTREACH_URL` in
    #: `linkedinmcp/platform/ids.env` (with the trailing slash), which the
    #: Home screen's buttons call (`routine.py`).
    outreach_url: str

    #: The IAP JWT audience for this Cloud Run service:
    #: `/projects/<project number>/locations/us-central1/services/linkedin-contacts`.
    iap_audience: str | None = None

    #: Local runs only: treat every request as this user. `deploy.cmd`
    #: never sets it; `tests/webapp/test_deploy.py` proves that.
    dev_user: str | None = None

    @model_validator(mode="after")
    def _auth_configured(self) -> "WebappSettings":
        """Fail closed: without an audience there is nothing to verify, and
        without a dev user there is nobody to let in."""
        if self.iap_audience is None and self.dev_user is None:
            raise ValueError("set WEBAPP_IAP_AUDIENCE, or WEBAPP_DEV_USER for a local run")
        return self


def get_settings() -> WebappSettings:
    """Load settings from the environment; not cached, as in `linkedinmcp`."""
    return WebappSettings.from_env()
