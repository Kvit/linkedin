"""Models parse real API payloads and tolerate the API's inconsistencies."""

from datetime import datetime

from lib.unipile.models import Chat, Profile, Relation, SentInvitation


def test_relation_exposes_both_identifiers(relations_body):
    relation = Relation.model_validate(relations_body["items"][0])

    assert relation.public_identifier == "danareyesrn"
    assert relation.member_id == "ACoAAFIXTURERELATION0000000000000000000"
    assert relation.provider_id == relation.member_id


def test_relation_created_at_parses_epoch_millis(relations_body):
    """Relations timestamp in epoch millis while accounts use ISO strings."""
    relation = Relation.model_validate(relations_body["items"][0])

    assert isinstance(relation.created_at, datetime)
    assert relation.created_at.year == 2026


def test_unknown_fields_are_preserved(relations_body):
    body = relations_body["items"][0] | {"brand_new_field": "surprise"}

    relation = Relation.model_validate(body)

    assert relation.brand_new_field == "surprise"


def test_profile_reports_complete_when_nothing_was_throttled(profile_body):
    profile = Profile.model_validate(profile_body)

    assert profile.throttled_sections == []
    assert profile.is_complete is True


def test_profile_with_throttled_sections_is_incomplete(throttled_profile_body):
    profile = Profile.model_validate(throttled_profile_body)

    assert profile.is_complete is False


def test_profile_is_incomplete_when_a_section_is_empty_despite_a_total(profile_body):
    """An empty section with a non-zero total is the throttling signature.

    LinkedIn withholds sections by returning them empty with HTTP 200, and it
    does not always name them in throttled_sections.
    """
    body = profile_body | {"skills": [], "skills_total_count": 23}

    assert Profile.model_validate(body).is_complete is False


def test_a_slightly_short_section_is_still_complete(profile_body):
    """Observed live: a real profile returned 9 of 10 work experiences with
    93/93 skills, 5/5 education and a full About section. LinkedIn collapses
    grouped roles at the same company, so exact counts do not match. Rejecting
    that profile would discard excellent classification input over nothing.
    """
    body = profile_body | {"work_experience_total_count": 3}

    assert Profile.model_validate(body).is_complete is True


def test_incomplete_sections_names_what_is_missing(profile_body):
    body = profile_body | {"skills": [], "skills_total_count": 23}

    assert Profile.model_validate(body).incomplete_sections == ["skills"]


def test_current_position_comes_from_the_entry_with_no_end_date(profile_body):
    """The documented `current` boolean is never returned by the live API."""
    profile = Profile.model_validate(profile_body)

    assert profile.current_position is not None
    assert profile.current_position.company == "Pinnacle Services Corporation"


def test_work_experience_dates_parse_from_the_api_slash_format(profile_body):
    profile = Profile.model_validate(profile_body)

    assert profile.work_experience[0].start_date.year == 2016
    assert profile.work_experience[0].start_date.month == 5
    assert profile.work_experience[0].end_date is None


def test_network_distance_maps_to_the_legacy_member_distance_integer(profile_body):
    assert Profile.model_validate(profile_body).member_distance == 2


def test_out_of_network_maps_to_zero(profile_body):
    body = profile_body | {"network_distance": "OUT_OF_NETWORK"}

    assert Profile.model_validate(body).member_distance == 0


def test_missing_network_distance_maps_to_zero(profile_body):
    body = {k: v for k, v in profile_body.items() if k != "network_distance"}

    assert Profile.model_validate(body).member_distance == 0


def test_chat_exposes_the_attendee_provider_id_used_for_lookup(chats_body):
    chat = Chat.model_validate(chats_body["items"][0])

    assert chat.attendee_provider_id == "ACoAAFIXTUREATTENDEE0000000000000000000"
    assert chat.has_unread is True


def test_sent_invitation_exposes_the_firestore_key(invitations_sent_body):
    invitation = SentInvitation.model_validate(invitations_sent_body["items"][0])

    assert invitation.public_identifier == "alex-morgan-4070701b"
    assert invitation.provider_id == "ACoAAFIXTUREINVITEE00000000000000000000"
    assert invitation.id == "7503196768394842113"


def test_shared_secret_is_read_from_the_nested_specifics_object():
    """The API nests it: specifics.{provider, shared_secret}.

    Reading it top-level left it None on every real payload, which made
    handle_invitation() raise ValueError before it could ever reach the API.
    """
    from lib.unipile.models import ReceivedInvitation

    invitation = ReceivedInvitation.model_validate(
        {
            "object": "InvitationReceived",
            "id": "inv-7",
            "invited_user_public_id": "a-person",
            "invitation_text": None,
            "specifics": {"provider": "LINKEDIN", "shared_secret": "SECRET-XYZ"},
        }
    )

    assert invitation.shared_secret == "SECRET-XYZ"


def test_shared_secret_also_accepts_a_top_level_value():
    from lib.unipile.models import ReceivedInvitation

    invitation = ReceivedInvitation.model_validate(
        {"id": "inv-8", "shared_secret": "TOP-LEVEL"}
    )

    assert invitation.shared_secret == "TOP-LEVEL"
