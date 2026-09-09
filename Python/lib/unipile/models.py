"""Pydantic models for the Unipile payloads this library touches.

Every model allows extra fields. The API adds keys without warning, and dropping
them would silently discard data a caller may need. Several fields are also
inconsistent across endpoints -- ``created_at`` is epoch milliseconds on
relations but an ISO string on accounts, and ``member_urn`` is a bare number on
the self profile but a full URN on relations -- so parsing is deliberately
permissive.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field, field_validator

NetworkDistance = Literal[
    "FIRST_DEGREE", "SECOND_DEGREE", "THIRD_DEGREE", "OUT_OF_NETWORK"
]

_DISTANCE_TO_INT: dict[str, int] = {
    "FIRST_DEGREE": 1,
    "SECOND_DEGREE": 2,
    "THIRD_DEGREE": 3,
    "OUT_OF_NETWORK": 0,
}

#: Sections whose returned length is compared against ``<name>_total_count``.
_COUNTED_SECTIONS = (
    "work_experience",
    "education",
    "skills",
    "languages",
    "certifications",
    "projects",
)


def _parse_timestamp(value: Any) -> Any:
    """Accept epoch milliseconds as well as ISO-8601 strings."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    return value


def _parse_slash_date(value: str | None) -> datetime | None:
    """Parse the ``M/D/YYYY`` dates the profile endpoint returns."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y")
    except ValueError:
        return None


class UnipileModel(BaseModel):
    """Base model: permissive by design."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Account(UnipileModel):
    id: str
    name: str | None = None
    type: str | None = None
    created_at: datetime | None = None

    _ts = field_validator("created_at", mode="before")(_parse_timestamp)


class Relation(UnipileModel):
    """A first-degree connection, as returned by ``GET /users/relations``."""

    public_identifier: str
    member_id: str
    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = None
    public_profile_url: str | None = None
    created_at: datetime | None = None

    _ts = field_validator("created_at", mode="before")(_parse_timestamp)

    @property
    def provider_id(self) -> str:
        """The id every write endpoint expects."""
        return self.member_id


class WorkExperience(UnipileModel):
    position: str | None = None
    company: str | None = None
    company_id: str | None = None
    location: str | None = None
    description: str | None = None
    status: str | None = None
    industry: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    start: str | None = None
    end: str | None = None

    @property
    def is_current(self) -> bool:
        """Absence of an end date is the only reliable signal.

        The API documents a ``current`` boolean on work experience, but no live
        response has ever included it.
        """
        return self.end is None

    @property
    def start_date(self) -> datetime | None:
        return _parse_slash_date(self.start)

    @property
    def end_date(self) -> datetime | None:
        return _parse_slash_date(self.end)


class Education(UnipileModel):
    school: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    description: str | None = None
    start: str | None = None
    end: str | None = None


class Skill(UnipileModel):
    name: str
    endorsement_count: int = 0


class Language(UnipileModel):
    name: str
    proficiency: str | None = None


class Certification(UnipileModel):
    name: str
    organization: str | None = None
    url: str | None = None


class Project(UnipileModel):
    name: str
    description: str | None = None
    skills: list[str] = Field(default_factory=list)


class ContactInfo(UnipileModel):
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)


class Profile(UnipileModel):
    """A LinkedIn profile from ``GET /users/{identifier}``.

    Only the fields the classification pipeline consumes are typed; everything
    else survives as an extra attribute.
    """

    provider_id: str
    public_identifier: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = None
    summary: str | None = None
    location: str | None = None
    public_profile_url: str | None = None
    network_distance: str | None = None
    contact_info: ContactInfo | None = None
    websites: list[str] = Field(default_factory=list)

    work_experience: list[WorkExperience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)

    throttled_sections: list[str] = Field(default_factory=list)

    @property
    def member_distance(self) -> int:
        """The integer degree the existing Firestore pipeline expects."""
        return _DISTANCE_TO_INT.get(self.network_distance or "", 0)

    @property
    def current_position(self) -> WorkExperience | None:
        return next((job for job in self.work_experience if job.is_current), None)

    @property
    def incomplete_sections(self) -> list[str]:
        """Sections LinkedIn withheld entirely.

        Throttling shows up as a section returned **empty** with HTTP 200, and
        LinkedIn does not always name it in ``throttled_sections`` -- so an
        empty section with a non-zero total counts as withheld too.

        A merely *short* section does not. A real profile returned 9 of 10 work
        experiences alongside 93/93 skills and a full About section, because
        LinkedIn collapses grouped roles at the same company; treating that as
        incomplete would discard excellent classification input for nothing.
        """
        missing = list(self.throttled_sections)
        extra = self.model_extra or {}
        for section in _COUNTED_SECTIONS:
            total = extra.get(f"{section}_total_count")
            if isinstance(total, int) and total > 0 and not getattr(self, section):
                if section not in missing:
                    missing.append(section)
        return missing

    @property
    def is_complete(self) -> bool:
        """False when LinkedIn withheld a requested section.

        An incomplete profile must never be written to Firestore: it would
        cache a classification derived from partial data, silently and
        permanently.
        """
        return not self.incomplete_sections


class Chat(UnipileModel):
    """A conversation from ``GET /chats``."""

    id: str
    account_id: str | None = None
    attendee_provider_id: str | None = None
    unread_count: int = 0
    unread: int = 0
    timestamp: datetime | None = None

    _ts = field_validator("timestamp", mode="before")(_parse_timestamp)

    @property
    def has_unread(self) -> bool:
        """A reply is waiting, so an outreach sequence must halt."""
        return bool(self.unread) or self.unread_count > 0


class Message(UnipileModel):
    """One message from ``/messages`` or ``/chats/{id}/messages``.

    The flag fields are 0/1 integers rather than booleans -- that is how the API
    sends them, and coercing to ``bool`` here would misrepresent a payload that
    also uses ``null`` for "not applicable".
    """

    id: str
    chat_id: str | None = None
    account_id: str | None = None
    text: str | None = None
    subject: str | None = None
    sender_id: str | None = None
    sender_attendee_id: str | None = None
    message_type: str | None = None
    is_sender: int | None = None
    deleted: int | None = None
    edited: int | None = None
    seen: int | None = None
    hidden: int | None = None
    is_event: int | None = None
    #: Nine provider-specific shapes (img, file, linkedin_post, contact_card,
    #: ...) across half a percent of messages. Modelling the union would buy
    #: nothing a caller storing the array verbatim can use, so it stays raw --
    #: but it is declared, so it defaults to empty rather than being absent.
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    timestamp: datetime | None = None

    _ts = field_validator("timestamp", mode="before")(_parse_timestamp)


class Attendee(UnipileModel):
    id: str
    provider_id: str | None = None
    name: str | None = None
    #: ``urn:li:member:<N>``. The API nests it under ``specifics``, and it is
    #: the only identity that reaches the legacy half of the contact store: of
    #: 28,328 stored profiles, 2,092 carry a member id and no provider hash. A
    #: top-level value is accepted too so hand-built payloads keep working.
    member_urn: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            AliasPath("specifics", "member_urn"), "member_urn"
        ),
    )


class SentInvitation(UnipileModel):
    """A pending outgoing invitation from ``GET /users/invite/sent``.

    ``invitation_text`` always comes back null, so the note that was actually
    sent is only ever recoverable from the caller's own records.
    """

    id: str
    invited_user_public_id: str | None = None
    invited_user_id: str | None = None
    invited_user: str | None = None
    invited_user_description: str | None = None
    parsed_datetime: datetime | None = None

    _ts = field_validator("parsed_datetime", mode="before")(_parse_timestamp)

    @property
    def public_identifier(self) -> str | None:
        """The Firestore document key for this contact."""
        return self.invited_user_public_id

    @property
    def provider_id(self) -> str | None:
        return self.invited_user_id


class ReceivedInvitation(UnipileModel):
    """An incoming connection request from ``GET /users/invite/received``.

    ``shared_secret`` is issued by LinkedIn alongside the invitation and is
    mandatory when accepting or declining it, so the invitation must be carried
    around rather than just its id.
    """

    id: str
    invitation_text: str | None = None
    #: The API nests this as ``specifics.shared_secret``; a top-level value is
    #: accepted too so hand-built payloads keep working.
    shared_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            AliasPath("specifics", "shared_secret"), "shared_secret"
        ),
    )


class InvitationSentResult(UnipileModel):
    """Response to ``POST /users/invite``.

    ``usage`` is LinkedIn's own quota signal, emitted only when a new threshold
    (50/75/90/95%) is crossed.
    """

    invitation_id: str | None = None
    usage: float | None = None


class ChatStarted(UnipileModel):
    chat_id: str | None = None
    message_id: str | None = None


class MessageSent(UnipileModel):
    message_id: str | None = None


class SearchParameter(UnipileModel):
    """A resolved search facet id, e.g. a LinkedIn location or industry."""

    id: str
    title: str | None = None


class SearchResult(UnipileModel):
    public_identifier: str | None = None
    provider_id: str | None = None
    name: str | None = None
    headline: str | None = None
