"""Profiles, relations and invitations."""

import logging
from collections.abc import Callable, Iterator
from typing import Any, Literal

from ..budget import SendBudget
from ..errors import ProfileIncomplete
from ..models import (
    InvitationSentResult,
    Profile,
    ReceivedInvitation,
    Relation,
    SentInvitation,
)
from ..pagination import iter_account_scoped
from ..transport import Transport

#: LinkedIn truncates invitation notes; the API rejects anything longer.
MAX_INVITATION_NOTE = 300

InvitationAction = Literal["accept", "decline"]

log = logging.getLogger(__name__)


class UsersResource:
    """Everything under `/users`."""

    def __init__(
        self,
        transport: Transport,
        account_id: Callable[[], str],
        budget: SendBudget,
        default_sections: list[str] | None = None,
        throttle_retries: int = 2,
    ) -> None:
        self._transport = transport
        self._account_id = account_id
        self._budget = budget
        self._default_sections = default_sections
        self._throttle_retries = throttle_retries

    # --- profiles -------------------------------------------------------------

    def get_profile(
        self,
        identifier: str,
        *,
        sections: list[str] | None = None,
        notify: bool = False,
        require_complete: bool = False,
    ) -> Profile:
        """Retrieve a profile by public slug or provider id.

        `sections` selects how much LinkedIn returns. Without it the response
        carries no experience, education, skills or About text at all, so the
        default comes from settings rather than being omitted.

        Profile fetches are the read LinkedIn throttles hardest, so this is
        budgeted and delayed exactly like a write.

        Throttling does not arrive as an error. The withheld sections come back
        empty with HTTP 200, named in `throttled_sections`, and the fetch still
        counted -- so it is still recorded. That silence is the reason for the
        retry loop: a partial profile means the account is being throttled right
        now, so each attempt backs the cadence off further before asking again,
        and the widened gap stays in force for whatever the caller does next.
        Attempts are bounded by `throttle_retries`; if the budget runs out
        first, `check` refuses and the caller sees `BudgetExhausted`.
        """
        attempts = self._throttle_retries + 1

        for attempt in range(1, attempts + 1):
            profile = self._fetch_profile(identifier, sections=sections, notify=notify)

            if profile.is_complete:
                if attempt > 1:
                    log.warning(
                        "LinkedIn returned the full profile for %s on attempt %d -- "
                        "throttling has cleared, resuming the normal pace.",
                        identifier,
                        attempt,
                    )
                self._budget.recovered()
                return profile

            withheld = ", ".join(profile.incomplete_sections)
            if attempt < attempts:
                log.warning(
                    "LinkedIn withheld %s for %s (attempt %d of %d) -- throttling "
                    "detected; pausing longer, then trying again.",
                    withheld,
                    identifier,
                    attempt,
                    attempts,
                )
            else:
                log.warning(
                    "LinkedIn still withheld %s for %s after %d attempts -- "
                    "skipping it; it will be retried on a later run.",
                    withheld,
                    identifier,
                    attempts,
                )
            self._budget.back_off()

        if require_complete:
            raise ProfileIncomplete(
                type="local/profile_incomplete",
                title=f"LinkedIn withheld sections for {identifier} after "
                f"{attempts} attempts: {', '.join(profile.incomplete_sections)}",
                detail="Storing this profile would cache a classification built "
                "from partial data. Retry on a later run.",
            )
        return profile

    def _fetch_profile(
        self, identifier: str, *, sections: list[str] | None, notify: bool
    ) -> Profile:
        self._budget.check("profile")
        self._budget.throttle()
        params: dict[str, Any] = {
            "account_id": self._account_id(),
            "notify": "true" if notify else "false",
            "linkedin_sections": sections if sections is not None else self._default_sections,
        }
        body = self._transport.get(f"/api/v1/users/{identifier}", params=params)
        self._budget.record("profile")
        return Profile.model_validate(body)

    # --- relations ------------------------------------------------------------

    def iter_relations(self, *, page_size: int = 100, filter: str | None = None) -> Iterator[Relation]:
        """Every first-degree connection."""
        return self._list("/api/v1/users/relations", Relation, page_size, filter=filter)

    def iter_followers(self, *, page_size: int = 100) -> Iterator[Relation]:
        return self._list("/api/v1/users/followers", Relation, page_size)

    def iter_following(self, *, page_size: int = 100) -> Iterator[Relation]:
        return self._list("/api/v1/users/following", Relation, page_size)

    # --- invitations ----------------------------------------------------------

    def iter_invitations_sent(self, *, page_size: int = 100) -> Iterator[SentInvitation]:
        """Pending outgoing invitations.

        Each entry carries `invited_user_public_id`, which is the Firestore
        document key -- so a slug that disappears from this set was accepted,
        withdrawn or ignored.
        """
        return self._list("/api/v1/users/invite/sent", SentInvitation, page_size)

    def iter_invitations_received(self, *, page_size: int = 100) -> Iterator[ReceivedInvitation]:
        return self._list("/api/v1/users/invite/received", ReceivedInvitation, page_size)

    def send_invitation(
        self,
        provider_id: str,
        *,
        message: str | None = None,
        user_email: str | None = None,
    ) -> InvitationSentResult:
        """Send a connection request, optionally with a note.

        The note is capped at 300 characters by LinkedIn, so for a prospect who
        is not yet a connection the note *is* the intro message.
        """
        if message is not None and len(message) > MAX_INVITATION_NOTE:
            raise ValueError(
                f"Invitation note is {len(message)} characters; "
                f"LinkedIn allows at most {MAX_INVITATION_NOTE}."
            )

        self._budget.check("invite")
        self._budget.throttle()
        payload: dict[str, Any] = {
            "provider_id": provider_id,
            "account_id": self._account_id(),
        }
        if message is not None:
            payload["message"] = message
        if user_email is not None:
            payload["user_email"] = user_email

        body = self._transport.post_json("/api/v1/users/invite", json=payload)
        self._budget.record("invite")

        result = InvitationSentResult.model_validate(body)
        # LinkedIn reports its own quota usage when a new threshold is crossed.
        self._budget.note_usage(result.usage)
        return result

    def cancel_invitation(self, invitation_id: str) -> None:
        """Withdraw a pending invitation.

        ``account_id`` is required in the query string on this route.
        """
        self._transport.delete(
            f"/api/v1/users/invite/sent/{invitation_id}",
            params={"account_id": self._account_id()},
        )

    def handle_invitation(
        self, invitation: ReceivedInvitation, action: InvitationAction
    ) -> None:
        """Accept or decline an invitation someone sent us.

        Takes the invitation rather than its id because LinkedIn requires the
        ``shared_secret`` it issued alongside it, which is only available from
        `iter_invitations_received`.
        """
        if not invitation.shared_secret:
            raise ValueError(
                f"Invitation {invitation.id} has no shared_secret; LinkedIn "
                "requires it to accept or decline. Re-read it from "
                "iter_invitations_received()."
            )
        self._transport.post_json(
            f"/api/v1/users/invite/received/{invitation.id}",
            json={
                "provider": "LINKEDIN",
                "shared_secret": invitation.shared_secret,
                "account_id": self._account_id(),
                "action": action,
            },
        )

    # --- internals ------------------------------------------------------------

    def _list[T](self, path: str, model: type[T], page_size: int, **extra: Any) -> Iterator[T]:
        return iter_account_scoped(
            self._transport.get, self._account_id, path, model,
            page_size=page_size, **extra,
        )
