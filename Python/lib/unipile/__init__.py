"""A Unipile client for LinkedIn contact and messaging operations.

Replaces LinkedIn Helper for invitations, messaging, profiles and search. This
is a pure API client: it performs HTTP, validates responses, paginates and
enforces send budgets. It does not touch Firestore and does not run campaigns --
orchestration lives in the calling scripts and notebooks.

    from lib.unipile import UnipileClient

    with UnipileClient.from_env() as li:
        profile = li.users.get_profile("some-slug")
        if profile.is_complete:
            document = to_lh_document(profile)
"""

from .budget import SendBudget
from .client import UnipileClient
from .compat import SUMMARY_KEYS, to_lh_document
from .config import UnipileSettings
from .errors import (
    AccountDisconnected,
    AccountRestricted,
    AlreadyConnected,
    AlreadyInvited,
    AuthenticationError,
    BudgetExhausted,
    CannotResendYet,
    CircuitOpen,
    ConfigError,
    ConnectionLimitReached,
    FeatureNotSubscribed,
    InsufficientCredits,
    NoConnectionWithRecipient,
    NotFound,
    PermissionDenied,
    ProfileIncomplete,
    RateLimited,
    ServerError,
    UnipileError,
    UnprocessableError,
    UserUnreachable,
)
from .pacing import HumanCadence
from .models import (
    Account,
    Attendee,
    Chat,
    ChatStarted,
    Message,
    MessageSent,
    Profile,
    ReceivedInvitation,
    Relation,
    SearchParameter,
    SearchResult,
    SentInvitation,
)

__all__ = [
    "Account",
    "AccountDisconnected",
    "AccountRestricted",
    "AlreadyConnected",
    "AlreadyInvited",
    "Attendee",
    "AuthenticationError",
    "BudgetExhausted",
    "CannotResendYet",
    "Chat",
    "ChatStarted",
    "CircuitOpen",
    "ConfigError",
    "ConnectionLimitReached",
    "FeatureNotSubscribed",
    "HumanCadence",
    "InsufficientCredits",
    "Message",
    "MessageSent",
    "NoConnectionWithRecipient",
    "NotFound",
    "PermissionDenied",
    "Profile",
    "ProfileIncomplete",
    "RateLimited",
    "ReceivedInvitation",
    "Relation",
    "SUMMARY_KEYS",
    "SearchParameter",
    "SearchResult",
    "SendBudget",
    "SentInvitation",
    "ServerError",
    "UnipileClient",
    "UnipileError",
    "UnipileSettings",
    "UnprocessableError",
    "UserUnreachable",
    "to_lh_document",
]
