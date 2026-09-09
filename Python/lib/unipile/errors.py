"""Typed exceptions for Unipile API responses.

Exceptions are keyed on the API""s ``type`` string rather than the HTTP status,
because the status alone is not actionable: ``errors/already_connected`` and
``errors/already_invited_recently`` are both 422, and a caller driving outreach
must branch between them.

An unrecognised ``type`` falls back to the class registered for its status, and
the raw ``type`` is always preserved so new provider codes stay inspectable.
"""

from typing import Any


class UnipileError(Exception):
    """Base for every error returned by the Unipile API."""

    def __init__(
        self,
        type: str = "",
        status: int = 0,
        title: str = "",
        detail: str | None = None,
        instance: str | None = None,
    ) -> None:
        self.type = type
        self.status = status
        self.title = title
        self.detail = detail
        self.instance = instance
        super().__init__(f"{status} {type}: {title}" if type else title)


# --- by status class ---------------------------------------------------------

class AuthenticationError(UnipileError):
    """401 — credentials missing, invalid, or expired."""


class AccountDisconnected(AuthenticationError):
    """The provider account needs reconnecting before anything will work."""


class PermissionDenied(UnipileError):
    """403 — authenticated, but not allowed to do this."""


class AccountRestricted(PermissionDenied):
    """LinkedIn has restricted the account. Stop writing immediately."""


class FeatureNotSubscribed(PermissionDenied):
    """The feature (e.g. Sales Navigator) is not on this subscription."""


class NotFound(UnipileError):
    """404 — no such account, chat, message, or user."""


class RateLimited(UnipileError):
    """429 — the provider is refusing further requests. Never auto-retried.

    ``retry_after`` carries the ``Retry-After`` header when the provider sends
    one. It is deliberately not slept on: a 429 means stop, and the daily
    runner decides when to come back.
    """

    def __init__(self, *args, retry_after: float | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class UnprocessableError(UnipileError):
    """422 — understood, but the provider refused to act on it."""


class ServerError(UnipileError):
    """5xx — provider-side failure. Safe to retry on reads only."""


# --- 422 outcomes an outreach runner must branch on --------------------------

class AlreadyConnected(UnprocessableError):
    """The target is already a first-degree connection: message instead."""


class AlreadyInvited(UnprocessableError):
    """An invitation was already sent recently: leave it pending."""


class NoConnectionWithRecipient(UnprocessableError):
    """Not connected, so a direct message is impossible: invite instead."""


class InsufficientCredits(UnprocessableError):
    """Out of InMail credits."""


class ConnectionLimitReached(UnprocessableError):
    """LinkedIn""s pending-invitation ceiling has been hit."""


class UserUnreachable(UnprocessableError):
    """The profile cannot be reached (privacy settings, deleted, blocked)."""


class CannotResendYet(UnprocessableError):
    """Too soon to resend to this recipient. Back off."""


_BY_TYPE: dict[str, type[UnipileError]] = {
    "errors/disconnected_account": AccountDisconnected,
    "errors/account_restricted": AccountRestricted,
    "errors/feature_not_subscribed": FeatureNotSubscribed,
    "errors/subscription_required": FeatureNotSubscribed,
    "errors/too_many_requests": RateLimited,
    "errors/already_connected": AlreadyConnected,
    "errors/already_invited_recently": AlreadyInvited,
    "errors/no_connection_with_recipient": NoConnectionWithRecipient,
    "errors/insufficient_credits": InsufficientCredits,
    "errors/connection_limit_reached": ConnectionLimitReached,
    "errors/user_unreachable": UserUnreachable,
    "errors/cannot_resend_yet": CannotResendYet,
    "errors/cannot_resend_within_24hrs": CannotResendYet,
}

_BY_STATUS: dict[int, type[UnipileError]] = {
    401: AuthenticationError,
    403: PermissionDenied,
    404: NotFound,
    422: UnprocessableError,
    429: RateLimited,
    500: ServerError,
    501: ServerError,
    503: ServerError,
    504: ServerError,
}


def _by_status_class(status: int) -> type[UnipileError]:
    """Fallback for statuses the map does not name explicitly.

    Any 5xx is a server-side failure: listing them one by one left holes (502
    was retried by the transport but escaped as a bare UnipileError, past every
    ``except ServerError`` handler).
    """
    return ServerError if status >= 500 else UnipileError


def raise_for_response(status: int, body: dict[str, Any]) -> None:
    """Raise the exception matching ``body["type"]``, else the status default."""
    error_type = body.get("type", "")
    cls = _BY_TYPE.get(error_type) or _BY_STATUS.get(
        status) or _by_status_class(status)
    raise cls(
        type=error_type,
        status=body.get("status", status),
        title=body.get("title", ""),
        detail=body.get("detail"),
        instance=body.get("instance"),
    )


# --- local errors, raised by the client rather than the provider -------------

class ConfigError(UnipileError):
    """Settings are missing or invalid; the client refuses to start."""


class CircuitOpen(UnipileError):
    """A restricted account tripped the breaker; writes are blocked."""


class ProfileIncomplete(UnipileError):
    """LinkedIn withheld or truncated requested profile sections.

    The response was a 200 with empty sections, so this is not a transport
    failure. Storing such a profile would cache a classification derived from
    partial data, silently and permanently.
    """


class ThrottleLockout(UnipileError):
    """Enough profiles in a row came back withheld that the account, not the
    profile, is the problem. Stop the run and resume later.

    Distinct from :class:`ProfileIncomplete`, which is about one slug and is
    safe to skip past. This one means every further fetch would spend daily
    budget on data LinkedIn has already decided not to give.
    """


class BudgetExhausted(UnipileError):
    """The daily cap for this action is spent, or the provider usage signal
    crossed the halt threshold. Resume tomorrow."""
