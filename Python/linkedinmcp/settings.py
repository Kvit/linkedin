"""Environment-driven settings for the `linkedin-outreach` Cloud Run service.

The loading and error-redaction machinery lives in :mod:`lib.config`, shared with
`lib/unipile/config.py` and with anything else in this project that reads
configuration. Only the fields below belong to this service.

**Rate limits are not here.** Messages per day and profile fetches per day are
`UNIPILE_MAX_MESSAGES_PER_DAY` and `UNIPILE_MAX_PROFILE_FETCHES_PER_DAY` on
:class:`lib.unipile.config.UnipileSettings`, which the notebooks already use and
which `SendBudget` actually enforces on the call. Declaring a second, differently
named copy here would let this service believe one number while the client making
the request enforced another -- and the first sign of the disagreement would be a
restricted LinkedIn account. Read them from the Unipile client the service
already builds.

What remains here is campaign policy Unipile has no concept of: how many intros a
planning run may queue, how many times one contact may be touched, how far apart,
and how long a message may be. Same structure as everything else in the repo --
the value written beside the field is the default, and an `OUTREACH_*` variable
in the environment overrides it.

**Import the module, not the name.** Every other module in this service reads
settings through :func:`get_settings`, and must do it like this::

    from linkedinmcp import settings as cfg
    ...
    cfg.get_settings()          # resolved at call time

not like this::

    from linkedinmcp.settings import get_settings
    ...
    get_settings()              # a local name, bound once at import time

The second form binds ``get_settings`` into the importing module's own namespace.
A test that does ``monkeypatch.setattr(cfg, "get_settings", ...)`` patches the
attribute on *this* module; a consumer holding the second form's local name never
looks at this module again, so the patch has no effect and the resulting test
failure -- the real environment gets read instead of the fake settings -- is
baffling to chase down.
"""

from pathlib import Path
from typing import Annotated, Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import NoDecode, SettingsConfigDict

from lib.config import BaseConfig, ConfigError, split_list

__all__ = ["ConfigError", "OutreachSettings", "get_settings"]


class OutreachSettings(BaseConfig):
    """All ``OUTREACH_*`` configuration, validated once at service startup."""

    model_config = SettingsConfigDict(
        env_prefix="OUTREACH_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    subject: ClassVar[str] = "outreach settings"

    #: The single fixed token. Presented as `x-api-key` or
    #: `Authorization: Bearer` by the Claude agent platform, Claude connectors,
    #: Cloud Scheduler and the Unipile webhook alike.
    #:
    #: The length floor exists because authentication must never fail open.
    #: `SecretStr` accepts an empty string, and `hmac.compare_digest(b"", b"")`
    #: is True, so a service started with `OUTREACH_API_KEY=` would have
    #: authenticated every caller on a `--allow-unauthenticated` URL. Refusing
    #: to start is the safe direction. Sixteen is a floor against accidents --
    #: an empty value, a truncated paste, "changeme" -- not a recommendation:
    #: the deploy runbook generates `secrets.token_urlsafe(32)`, 43 characters.
    api_key: SecretStr = Field(min_length=16)

    #: IANA name driving the scheduler's working-hours window and every date the
    #: reports show. **Set this** -- the default is deliberately wrong for a
    #: human, so an unset value is visible rather than silently shifting a
    #: working-hours cron.
    tz: str = "UTC"

    #: How many intro messages one daily planning run may queue.
    intro_daily_cap: int = Field(default=10, ge=1)

    #: Minimum age, in days, of the newest outbound message before a follow-up
    #: may go out.
    min_days_between_touches: int = Field(default=5, ge=0)

    #: Maximum outbound messages per contact, counted from `sent_total`.
    max_touches: int = Field(default=3, ge=1)

    #: Longest message body the service will send, in characters.
    message_max_chars: int = Field(default=1200, ge=1)

    #: Domains a queued message may link to. Empty means no links at all.
    allowed_link_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )

    #: Industry labels eligible for an intro. Mirrors `TARGET_INDUSTRIES` in
    #: `send-intros.ipynb`. Setting this to an empty string makes **no contact
    #: eligible** -- a deliberate kill switch, and not the same as leaving it
    #: unset, which gives the four below.
    target_industries: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "RCM", "Pathology", "Medical Lab", "Physician Practice",
        ]
    )

    #: Message templates, relative to the working directory, which is `Python/`
    #: for both `uvicorn linkedinmcp.app:create_app` and the container.
    templates_dir: Path = Path("templates")

    #: When true, every queued message waits for a human before it may be sent.
    require_approval: bool = False

    #: How stale, in minutes, the cached account-wide send count may get before
    #: it is recounted from LinkedIn.
    budget_snapshot_max_age_minutes: int = Field(default=60, ge=1)

    #: Whether `POST /jobs/*?dry_run=1` is honoured, so the scheduler wiring can
    #: be exercised without touching LinkedIn.
    allow_http_dry_run: bool = True

    #: Signing key for the optional Claude platform webhook. Unset means that
    #: endpoint is off.
    anthropic_webhook_signing_key: SecretStr | None = None

    @field_validator("allowed_link_domains", "target_industries", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        """Accept `a.com, b.com` from the environment as well as a real list.

        The splitting itself is `lib.config.split_list`; its docstring explains
        why the `NoDecode` annotation above is required for the comma form to
        reach a validator at all.
        """
        return split_list(value)

    @field_validator("tz")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        """Fail at startup, not hours later inside whichever job first formats a
        date.

        `ZoneInfo` raises `ZoneInfoNotFoundError` for an unknown name, which
        subclasses `KeyError` -- pydantic only converts `ValueError` (and
        `AssertionError`) raised inside a validator into a `ValidationError`, so
        a bare `KeyError` would otherwise propagate unwrapped, straight past
        `from_env`'s `except ValidationError` clause.
        """
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError(f"unknown IANA time zone: {value!r}") from None
        return value


def get_settings() -> OutreachSettings:
    """Load settings from the environment. The single accessor every other
    module in the service uses.

    Deliberately **not** wrapped in `functools.lru_cache`. That looks like an
    obvious improvement, but a cached settings object would survive across tests
    that monkeypatch the environment -- defeating per-test monkeypatching and
    making test order matter, since whichever test ran first would permanently
    decide what every later test saw. Every caller pays one fresh environment
    read; that is the point.

    Import the *module* to call this (`from linkedinmcp import settings as cfg`,
    then `cfg.get_settings()`), not the name -- see the module docstring.
    """
    return OutreachSettings.from_env()
