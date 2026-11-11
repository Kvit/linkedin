"""The LinkedIn Helper compatibility mapper.

`to_lh_document` reshapes a Unipile profile into the dict the existing Firestore
pipeline expects, so `join_keys`, the Gemini prompt, the analysis notebook and
the 28k already-classified rows all stay untouched.
"""

import pytest

from functions import join_keys
from lib.unipile.compat import SUMMARY_KEYS, to_lh_document
from lib.unipile.errors import ProfileIncomplete
from lib.unipile.models import Profile


def build(profile_body, **overrides) -> dict:
    return to_lh_document(Profile.model_validate(profile_body | overrides))


def test_document_is_keyed_by_the_public_identifier(profile_body):
    assert build(profile_body)["id"] == "khvatkov"


def test_profile_url_and_external_ids_carry_both_identities(profile_body):
    doc = build(profile_body)

    assert doc["profileUrl"].endswith("/khvatkov")
    by_type = {entry["type"]: entry["externalId"] for entry in doc["externalIds"]}
    assert by_type["public-id"] == "khvatkov"
    assert by_type["li-hash-id"] == "ACoAAAAZ0JEBdxllEow1i5AAvLdaATMLQCdXF6c"


def test_every_key_join_keys_reads_is_present(profile_body):
    doc = build(profile_body)

    missing = [key for key in SUMMARY_KEYS if key not in doc]
    assert missing == []


def test_current_position_is_the_entry_without_an_end_date(profile_body):
    current = build(profile_body)["currentPosition"]

    assert current["company"] == "Pinnacle Services Corporation"
    assert current["position"] == "Founder"


def test_positions_use_the_legacy_date_range_shape(profile_body):
    positions = build(profile_body)["positions"]

    assert positions[0]["title"] == "Founder"
    assert positions[0]["companyName"] == "Pinnacle Services Corporation"
    assert positions[0]["dateRange"]["start"] == {"year": 2016, "month": 5}
    assert positions[0]["dateRange"]["end"] is None
    assert positions[1]["dateRange"]["end"] == {"year": 2023, "month": 4}


def test_positions_carry_their_per_role_skills(profile_body):
    """Full section depth returns skills per role; LinkedIn Helper never had these."""
    assert "Revenue Cycle Management" in build(profile_body)["positions"][0]["skills"]


def test_educations_use_the_legacy_field_names(profile_body):
    education = build(profile_body)["educations"][0]

    assert education["schoolName"] == "Rice Business"
    assert education["degreeName"] == "Master, Business Administration"


def test_skills_use_the_legacy_endorsement_field(profile_body):
    skills = build(profile_body)["skills"]

    assert skills[0] == {"name": "Revenue Cycle Management", "endorsementsCount": 16}


def test_extra_carries_the_about_text_not_the_top_level_summary(profile_body):
    """LinkedIn Helper's top-level `summary` is the flattened blob join_keys
    builds; the About text belongs under `extra`."""
    doc = build(profile_body)

    assert doc["extra"]["summary"].startswith("I help independent labs")
    assert "summary" not in doc


def test_extra_carries_location_and_enrichment_sections(profile_body):
    extra = build(profile_body)["extra"]

    assert extra["locationName"] == "Houston, Texas, United States"
    assert extra["certifications"] == [{"name": "CRCR", "organization": "HFMA"}]
    assert extra["languages"] == [
        {"name": "English", "proficiency": "Native or bilingual proficiency"}
    ]
    assert extra["projects"][0]["name"] == "Immunoscore"


def test_recommendations_are_excluded_from_enrichment(profile_body):
    """Thousands of characters of third-party testimonial, weak signal for
    industry/function/seniority, and it inflates Gemini input cost."""
    body = profile_body | {"recommendations": {"received": [{"text": "great"}]}}

    assert "recommendations" not in build(body)["extra"]


def test_member_distance_is_the_legacy_integer(profile_body):
    assert build(profile_body)["memberDistance"] == 2


def test_email_is_lifted_from_contact_info(profile_body):
    assert build(profile_body)["email"] == "redacted@example.com"


def test_occupation_falls_back_to_the_headline(profile_body):
    assert build(profile_body)["occupation"] == profile_body["headline"]


def test_linkedin_helper_bookkeeping_is_not_reproduced(profile_body):
    doc = build(profile_body)

    for dead_key in ("lhId", "personId", "campaign_id", "fullMessagingHistory"):
        assert dead_key not in doc


# --- integration with the real pipeline --------------------------------------


def test_join_keys_produces_a_usable_summary_from_the_mapped_document(profile_body):
    summary = join_keys(build(profile_body), SUMMARY_KEYS)

    assert len(summary) > 50, "shorter than the notebook's SUMMARY_MIN_LEN cutoff"
    assert "Pinnacle Services Corporation" in summary
    assert "Revenue Cycle Management" in summary
    assert "CRCR" in summary, "certifications must reach the classifier"


def test_join_keys_on_the_mapped_document_is_deterministic(profile_body):
    doc = build(profile_body)

    assert join_keys(doc, SUMMARY_KEYS) == join_keys(doc, SUMMARY_KEYS)


def test_mapping_an_incomplete_profile_is_refused(throttled_profile_body):
    """`to_lh_document` is the only door into Firestore, so it is where the
    "never store a partial profile" rule has to be enforced. A caller that
    forgets to check `is_complete` must not be able to cache a classification
    built from sections LinkedIn withheld."""
    profile = Profile.model_validate(throttled_profile_body)

    with pytest.raises(ProfileIncomplete) as excinfo:
        to_lh_document(profile)

    assert "skills" in str(excinfo.value)


def test_a_profile_without_a_public_identifier_falls_back_to_the_provider_id():
    """public_identifier is nullable in the API schema.

    Emitting id=None makes Firestore's .document(None) generate a random 20-char
    id, so the same person lands as a fresh duplicate on every ingestion.
    """
    from lib.unipile.models import Profile

    doc = to_lh_document(
        Profile.model_validate(
            {"provider_id": "ACoAA-STABLE", "public_identifier": None, "websites": []}
        )
    )

    assert doc["id"] == "ACoAA-STABLE"


def test_the_public_identifier_still_wins_when_present(profile_body):
    assert build(profile_body)["id"] == "khvatkov"
