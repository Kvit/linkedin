"""Environment-driven settings for the Unipile client.

``api_key`` and ``dns`` deliberately have no defaults. The published OpenAPI spec
carries a default host of ``api1.unipile.com:13111`` which is wrong for every
real tenant and fails with ``503 no_client_session`` — refusing to start beats
silently talking to the wrong host.
"""

import json
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .errors import ConfigError

#: Sections requested from LinkedIn on every profile fetch. Deliberately narrow.
#: Section 10 of the design spec chose all seven, but LinkedIn stalls on the
#: wider list; on these two a 119-profile run completed ~99% of fetches, and
#: they carry enough signal to classify. The cost is that `skills` and
#: `educations` reach the Gemini summary empty (`compat.SUMMARY_KEYS` reads
#: both) -- an accepted trade, not an oversight. Widen for a single call with
#: `get_profile(sections=...)` rather than raising this default.
DEFAULT_PROFILE_SECTIONS = [
    "about",
    "experience"
]


class UnipileSettings(BaseSettings):
    """All ``UNIPILE_*`` configuration, validated once at client construction."""

    model_config = SettingsConfigDict(
        env_prefix="UNIPILE_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: SecretStr
    dns: str
    account_id: str | None = None

    #: Invitations per UTC day, well under LinkedIn's own weekly ceiling.
    max_invites_per_day: int = 25
    #: Messages per UTC day, across new chats and replies alike.
    max_messages_per_day: int = 50
    #: Profile reads per UTC day -- the binding constraint in practice, and the
    #: read LinkedIn throttles hardest. Retries of a throttled fetch count too.
    max_profile_fetches_per_day: int = 250

    # Pacing. A person opening one profile, reading it and moving on takes far
    # longer than a script needs to, so every budgeted call waits first. See
    # `pacing.HumanCadence` for how a gap is drawn.

    #: Shortest gap between two calls.
    min_delay_seconds: float = 20.0
    #: Longest ordinary gap. Draws are skewed toward `min_delay_seconds`, so the
    #: mean sits near a third of the way up the range, not at the midpoint.
    max_delay_seconds: float = 40.0
    #: Average number of calls between long breaks; 0 disables them.
    long_pause_every: int = 10
    #: Shortest long break.
    long_pause_min_seconds: float = 2*60.0
    #: Longest long break, drawn uniformly against the minimum.
    long_pause_max_seconds: float = 5*60.0

    #: Extra attempts for a profile whose sections LinkedIn withheld, each after
    #: a doubled pause. Every attempt is charged to
    #: `max_profile_fetches_per_day`, so this trades daily reach for hit rate.
    #: 0 skips a throttled profile immediately, leaving it for a later run.
    throttle_retries: int = 2

    #: How many consecutive profiles may exhaust their retries before the client
    #: gives up on the whole run. Without this a throttled account keeps fetching
    #: at the 8x pace until the daily budget is gone, storing nothing. A single
    #: complete profile resets the count; 0 disables the stop.
    max_consecutive_throttled: int = 5

    usage_warn_pct: float = 75.0
    usage_halt_pct: float = 90.0

    # NoDecode: pydantic-settings would otherwise JSON-decode this at the source
    # level, before any field validator runs, so "a,b,c" would be a parse error.
    profile_sections: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_PROFILE_SECTIONS)
    )
    timeout_seconds: float = 30.0

    @field_validator("profile_sections", mode="before")
    @classmethod
    def _parse_sections(cls, value: Any) -> Any:
        """Accept ``about,experience`` as well as ``["about", "experience"]``.

        ``NoDecode`` turns off pydantic-settings' own JSON decoding (it runs at
        the source level, before validators, so the comma form would be a parse
        error). That means the JSON form has to be decoded here instead.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("["):
            return json.loads(text)
        return [part.strip() for part in text.split(",") if part.strip()]

    @property
    def base_url(self) -> str:
        """Full origin, e.g. ``https://api62.unipile.com:19262``."""
        dns = self.dns.strip().rstrip("/")
        if dns.startswith(("http://", "https://")):
            return dns
        return f"https://{dns}"

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> Self:
        """Load settings, turning validation failures into :class:`ConfigError`."""
        try:
            return cls(_env_file=env_file)  # type: ignore[call-arg]
        except ValidationError as exc:
            raise ConfigError(
                type="config/invalid_settings",
                title=f"Invalid Unipile settings: {_field_list(exc)}",
                detail=_safe_detail(exc),
            ) from None  # the chained error embeds the raw API key


def _field_list(exc: ValidationError) -> str:
    return ", ".join(str(err["loc"][0]) for err in exc.errors() if err["loc"])


def _safe_detail(exc: ValidationError) -> str:
    """Describe what is wrong without ever echoing a value.

    Pydantic renders the raw input alongside each error, and at that point the
    API key is still a plain string -- ``SecretStr`` has not been applied. So
    ``str(exc)`` and the chained traceback would both print the key verbatim
    whenever some *other* field fails validation, which is exactly the
    "key set, DNS forgotten" case.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    )
