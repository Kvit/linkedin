"""Settings machinery shared by every configurable component in this project.

Each component owns its own settings class and its own error type -- the Unipile
client refuses to start on a bad `UNIPILE_*` value, the outreach service on a bad
`OUTREACH_*` one -- but the *machinery* underneath is the same every time: read
the environment, fall back to a default written next to the field, and turn a
validation failure into an error that names the fields without printing their
values.

That last part is the reason this module exists rather than each component
keeping its own copy. `safe_detail` is subtle, and a review of the second copy
found the subtlety had already been misunderstood: pydantic puts the offending
value on the error as `input`, so `str(exc)` prints a rejected API key verbatim.
The protection is building the message from `loc` and `msg` alone. One copy of
that reasoning is the only safe number.

A component's settings class inherits :class:`BaseConfig`, sets `error_class`
and `subject`, and declares its fields. Nothing else is required.
"""

import json
from pathlib import Path
from typing import Any, ClassVar, Self

from pydantic import ValidationError
from pydantic_settings import BaseSettings


class ConfigError(Exception):
    """Settings are missing or invalid; the component refuses to start.

    The default `error_class` for :class:`BaseConfig`. A component that needs
    its failures caught by a wider `except` -- as the Unipile client does, whose
    callers write `except UnipileError` -- overrides `error_class` with a class
    of its own instead. That is the only reason to.
    """

    def __init__(self, *, type: str, title: str, detail: str | None = None) -> None:
        self.type = type
        self.title = title
        self.detail = detail
        super().__init__(f"{type}: {title}" if type else title)


class BaseConfig(BaseSettings):
    """Environment-driven settings that fail loudly and quietly at once.

    Loudly: a bad value stops the component from starting, rather than letting
    it run against a wrong host or an unenforced cap.

    Quietly: the error names the fields that failed and says what is wrong with
    them, and never reproduces what was supplied.
    """

    #: The exception raised when validation fails. Each component keeps its own
    #: so that callers can catch by component -- `except UnipileError` has to
    #: keep catching a Unipile configuration failure, which it would not if this
    #: module owned a single shared class.
    error_class: ClassVar[type[Exception]] = ConfigError

    #: Machine-readable tag on that error. Shared by default because every
    #: component means the same thing by it.
    error_type: ClassVar[str] = "config/invalid_settings"

    #: What the error calls this configuration, as in "Invalid <subject>: dns".
    subject: ClassVar[str] = "settings"

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> Self:
        """Load settings, turning a validation failure into `error_class`.

        `from None` matters: the chained `ValidationError` embeds the raw
        values, so letting it ride along would defeat `safe_detail` entirely
        the first time anyone printed a traceback.
        """
        try:
            return cls(_env_file=env_file)  # type: ignore[call-arg]
        except ValidationError as exc:
            raise cls.error_class(
                type=cls.error_type,
                title=f"Invalid {cls.subject}: {field_list(exc)}",
                detail=safe_detail(exc),
            ) from None


def field_list(exc: ValidationError) -> str:
    """The names of the fields that failed, for a one-line summary."""
    return ", ".join(str(err["loc"][0]) for err in exc.errors() if err["loc"])


def safe_detail(exc: ValidationError) -> str:
    """Describe what is wrong without ever echoing a value.

    Pydantic puts the offending value on the error as `input`, and for a secret
    it is still a plain string at that point -- `SecretStr` has not been applied
    yet. So `str(exc)`, and any chained traceback, prints a rejected key
    verbatim.

    The protection is that this builds the message from `loc` and `msg` only.
    `include_input=False` is belt and braces: inert while the format string
    never reads `input`, and correct the moment someone adds it.

    The case that matters is a secret failing *its own* validation -- a
    truncated paste, a value below a length floor. A secret that merely sits
    beside some other failing field is not at risk, because pydantic scopes each
    error's `input` to the field that failed.

    One trap this does not cover: a hand-written validator that interpolates the
    value into its own `ValueError` puts it in `msg`, which this does render.
    That is fine for a hostname or a timezone; never do it for a secret.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    )


def split_list(value: Any) -> Any:
    """Accept ``a,b,c`` as well as ``["a", "b", "c"]`` for a list field.

    Pair it with `Annotated[list[str], NoDecode]`. Without `NoDecode`,
    pydantic-settings JSON-decodes complex-typed fields in the environment
    source *before* any validator runs, so the comma form raises `SettingsError`
    -- which is a `ValueError`, not a `ValidationError`, and so escapes
    `from_env`'s handling entirely. `NoDecode` turns that off, which means the
    JSON form has to be decoded here instead.

    Anything that is not a string passes through untouched, so a real list
    supplied in code is left alone.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("["):
        return json.loads(text)
    return [part.strip() for part in text.split(",") if part.strip()]
