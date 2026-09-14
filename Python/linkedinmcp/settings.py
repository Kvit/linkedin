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

import os
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import NoDecode, SettingsConfigDict

from lib.config import BaseConfig, ConfigError, split_list

__all__ = ["SERVICE_PACING", "ConfigError", "OutreachSettings", "get_settings", "load_environment"]

#: The service's pacing: no sleep between LinkedIn calls, no long breaks, no
#: throttle retries. `load_environment` writes these four into `os.environ`,
#: where `lib.unipile.config.UnipileSettings` -- which reads both the
#: environment and the `.env` file, the environment winning -- picks them up.
#: The notebooks pace like a person, inside one long process; this service is
#: paced by its scheduler instead, and a sleep inside a leased tick is billed
#: wall-clock time that can outlast the lease.
SERVICE_PACING = {
    "UNIPILE_MIN_DELAY_SECONDS": "0",
    "UNIPILE_MAX_DELAY_SECONDS": "0",
    "UNIPILE_LONG_PAUSE_EVERY": "0",
    "UNIPILE_THROTTLE_RETRIES": "0",
}


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

    #: The random gap between one queued intro's `due_at` and the next, in
    #: minutes (ruling P5-3). Both 0 by default since 2026-09-14, at the
    #: user's direction (1 to 5 before): every intro is due at once, and
    #: `send_messages` spaces the sends, one a minute by default.
    intro_gap_min_minutes: int = Field(default=0, ge=0)
    intro_gap_max_minutes: int = Field(default=0, ge=0)

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

    #: How many days back the daily job looks for connections whose profile
    #: is not stored yet (ledger ruling P3-1): the user keeps loading the
    #: historical backlog with `new-contacts.ipynb`; this service handles
    #: only the day's increment.
    new_connection_days: int = Field(default=14, ge=1)

    #: How many days back a connection may have been made and still get the
    #: daily job's intro. 0 means every eligible connection, however old --
    #: v1's behaviour. The backlog of older connections belongs to
    #: `send-intros.ipynb` (MCP v2 design, 2026-09-11).
    intro_connection_days: int = Field(default=14, ge=0)

    #: How a job the MCP tools start is run (`monitor.py`): `inline` in the
    #: same call -- tests and local runs -- or `cloud_tasks`, an HTTP task
    #: that calls back `POST /jobs/run/{job_id}` on this service.
    job_executor: Literal["inline", "cloud_tasks"] = "inline"

    #: This service's own base URL, without `/mcp/` -- where a Cloud Task
    #: sends a job back to. `deploy.cmd` sets it; `cloud_tasks` needs it.
    service_url: str | None = None

    #: The Cloud Tasks queue jobs go through, and where it lives.
    jobs_queue: str = "linkedin-jobs"
    jobs_location: str = "us-central1"

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

    @model_validator(mode="after")
    def _check_intro_gap(self) -> "OutreachSettings":
        """An upper bound below the lower one is a typo, not a range: the
        draw would silently run between the two anyway."""
        if self.intro_gap_max_minutes < self.intro_gap_min_minutes:
            raise ValueError(
                f"INTRO_GAP_MAX_MINUTES ({self.intro_gap_max_minutes}) is below INTRO_GAP_MIN_MINUTES "
                f"({self.intro_gap_min_minutes})"
            )
        return self


def load_environment() -> None:
    """Load the service's environment files and force its zero pacing.

    Called by `app.create_app` (unless it is handed settings) and by the
    `run_jobs` command line, before `get_settings()`. The notebooks never call
    it, so their pacing is unaffected.

    Three steps, in this order:

    1. `.env` -- the project's shared file, the same one the notebooks read.
       It carries every credential -- `OUTREACH_API_KEY` included, alongside
       `UNIPILE_*` and `GOOGLE_API_KEY` -- and the rate limits `SendBudget`
       enforces. Loaded without `override`, so a variable already set in the
       process environment keeps its value.
    2. `SERVICE_PACING` written into `os.environ`, replacing whatever step 1
       loaded or the process already had.
    3. `linkedinmcp/.env` -- optional. `override=True` is what makes this file
       win wherever it names the same variable as step 1 or step 2, so it is
       where a deliberate non-zero pacing for the service would go.

    Why step 2 exists: `linkedinmcp/.env` is gitignored, and on 2026-09-10 it
    was found missing from the working tree -- every image built until then
    had run with the notebooks' 20-40 s pacing and multi-minute breaks.
    Harmless for the read tools; fatal for a tick, whose lease a long break
    outlasts. A gitignored file going missing must never re-enable sleeps.

    Both files are named by explicit relative path and never loaded through a
    bare `load_dotenv()` -- the bare form calls `find_dotenv()`, which walks up
    the directory tree and would pick up whatever it found first. The working
    directory is `Python/` locally and `/app` in the container, which is the
    same directory, so both paths resolve identically in both places. Nothing
    from either file is ever printed.
    """
    load_dotenv(".env")
    for name, value in SERVICE_PACING.items():
        os.environ[name] = value
    load_dotenv("linkedinmcp/.env", override=True)


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
