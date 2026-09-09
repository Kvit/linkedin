"""Reshape a Unipile profile into the LinkedIn Helper document shape.

The existing pipeline is keyed to LinkedIn Helper's field names: `functions.
join_keys` reads seven specific keys to build the plain-text `summary` that
Gemini classifies, and 28k documents already sit in Firestore under that shape.
Mapping here rather than changing the pipeline keeps the Gemini prompt, the
analysis notebook and every stored classification untouched.

This module is a pure transform: it never touches Firestore. It is, though,
the only way a Unipile profile becomes a Firestore document, which makes it the
one place that can guarantee a partial profile is never stored -- so it refuses
one rather than trusting every caller to check `Profile.is_complete` first.
"""

from typing import Any

from .errors import ProfileIncomplete
from .models import Profile, WorkExperience

#: The keys `functions.join_keys` reads to build the classification summary.
SUMMARY_KEYS = [
    "miniProfile",
    "currentPosition",
    "positions",
    "occupation",
    "extra",
    "skills",
    "educations",
]


def to_lh_document(profile: Profile) -> dict[str, Any]:
    """Build the Firestore document body for a Unipile profile.

    The returned dict deliberately omits LinkedIn Helper's own bookkeeping
    (`lhId`, `personId`, `campaign_*`, `fullMessagingHistory`): those described
    LinkedIn Helper's internal state, and message history is now served properly
    by `GET /chats/{id}/messages`.

    Note there is no top-level `summary` key. In the LinkedIn Helper shape that
    field holds the flattened blob `join_keys` produces, so the caller adds it:

        doc = to_lh_document(profile)
        doc["summary"] = join_keys(doc, SUMMARY_KEYS)

    Raises `ProfileIncomplete` when LinkedIn withheld a section. A document
    built from a throttled response would cache a classification derived from
    data that is missing rather than absent -- silently, and permanently, since
    nothing downstream ever revisits a stored profile.
    """
    if not profile.is_complete:
        raise ProfileIncomplete(
            type="local/profile_incomplete",
            title="LinkedIn withheld sections for "
            f"{profile.public_identifier or profile.provider_id}: "
            f"{', '.join(profile.incomplete_sections)}",
            detail="Fetch it again on a later run; do not store partial data.",
        )

    return {
        "id": _document_key(profile),
        "profileUrl": _profile_url(profile),
        "externalIds": _external_ids(profile),
        "fullName": _full_name(profile),
        "memberDistance": profile.member_distance,
        "email": _first(profile.contact_info.emails if profile.contact_info else []),
        "phoneNumbers": profile.contact_info.phones if profile.contact_info else [],
        "websites": profile.websites,
        "miniProfile": {
            "firstName": profile.first_name,
            "lastName": profile.last_name,
            "headline": profile.headline,
        },
        "occupation": profile.headline,
        "currentPosition": _current_position(profile),
        "positions": [_position(job) for job in profile.work_experience],
        "educations": [
            {
                "schoolName": education.school,
                "degreeName": education.degree,
                "fieldOfStudy": education.field_of_study,
                "description": education.description,
                "dateRange": _date_range(education.start, education.end),
            }
            for education in profile.education
        ],
        "skills": [
            {"name": skill.name, "endorsementsCount": skill.endorsement_count}
            for skill in profile.skills
        ],
        "extra": _extra(profile),
    }


def _extra(profile: Profile) -> dict[str, Any]:
    """
    if not profile.is_complete:
        raise ProfileIncomplete(
            type="local/profile_incomplete",
            title="LinkedIn withheld sections for "
            f"{profile.public_identifier or profile.provider_id}: "
            f"{', '.join(profile.incomplete_sections)}",
            detail="Fetch it again on a later run; do not store partial data.",
        )
Everything else the classifier should see.

    `join_keys` recurses through every key of `extra`, so sections placed here
    reach the Gemini summary with no pipeline change. Certifications matter most
    in this domain -- CRCR, CPC, RHIA and CCS are strong signals of an RCM role.

    `recommendations` is deliberately excluded: thousands of characters of
    third-party testimonial per profile, weak signal for industry, function or
    seniority, and a direct increase in Gemini input cost.
    """
    extra: dict[str, Any] = {
        "summary": profile.summary,
        "locationName": profile.location,
        "industry": _industry(profile),
    }
    if profile.certifications:
        extra["certifications"] = [
            _compact({"name": item.name, "organization": item.organization})
            for item in profile.certifications
        ]
    if profile.languages:
        extra["languages"] = [
            _compact({"name": item.name, "proficiency": item.proficiency})
            for item in profile.languages
        ]
    if profile.projects:
        extra["projects"] = [
            _compact({"name": item.name, "description": item.description})
            for item in profile.projects
        ]
    return _compact(extra)


def _industry(profile: Profile) -> str | None:
    """Best-effort member industry.

    LinkedIn Helper carried a member-level industry. Unipile documents one per
    work experience but has never returned it on a live response, so this is
    usually empty; the classifier derives industry from title and description
    text anyway.
    """
    for job in profile.work_experience:
        if job.industry:
            return job.industry[0]
    return None


def _position(job: WorkExperience) -> dict[str, Any]:
    return _compact(
        {
            "title": job.position,
            "companyName": job.company,
            "companyId": job.company_id,
            "locationName": job.location,
            "description": job.description,
            "skills": job.skills,
            "dateRange": _date_range(job.start, job.end),
        }
    )


def _current_position(profile: Profile) -> dict[str, Any] | None:
    job = profile.current_position
    if job is None:
        return None
    return _compact({"company": job.company, "position": job.position})


def _date_range(start: str | None, end: str | None) -> dict[str, Any]:
    """LinkedIn Helper's `{"start": {"year": ..., "month": ...}, "end": None}`."""
    from .models import _parse_slash_date

    def part(value: str | None) -> dict[str, int] | None:
        parsed = _parse_slash_date(value)
        return None if parsed is None else {"year": parsed.year, "month": parsed.month}

    return {"start": part(start), "end": part(end)}


def _external_ids(profile: Profile) -> list[dict[str, str]]:
    """Both identities: the public slug and the `ACoAAA...` provider id."""
    ids: list[dict[str, str]] = []
    if profile.public_identifier:
        ids.append({"type": "public-id", "externalId": profile.public_identifier})
    if profile.provider_id:
        ids.append({"type": "li-hash-id", "externalId": profile.provider_id})
    return ids


def _document_key(profile: Profile) -> str:
    """The Firestore document id for this contact.

    ``public_identifier`` is nullable in the API schema. Returning ``None`` made
    ``collection.document(None)`` generate a random 20-character id, so the same
    person was stored again as a fresh duplicate on every ingestion. The
    provider id is always present and just as stable, so it is the fallback.
    """
    return profile.public_identifier or profile.provider_id


def _profile_url(profile: Profile) -> str | None:
    if profile.public_profile_url:
        return profile.public_profile_url
    if profile.public_identifier:
        return f"https://www.linkedin.com/in/{profile.public_identifier}"
    return None


def _full_name(profile: Profile) -> str:
    return " ".join(part for part in (profile.first_name, profile.last_name) if part)


def _first(values: list[str]) -> str | None:
    return values[0] if values else None


def _compact(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values so they never reach the summary as noise."""
    return {key: value for key, value in mapping.items() if value not in (None, "", [])}
