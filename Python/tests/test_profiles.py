"""Tests for the profile classifier lifted out of analysis.ipynb and new-contacts.ipynb.

`analysis_body` builds the merge document Phase E writes to the `analysis`
collection; a wrong key or an unconverted type would corrupt records that also
hold contact names, emails and message tallies found nowhere else. The
`Industry`/`Function`/`Seniority` vocabulary and `ProfileAnalysis` are the
contract Gemini's structured output must satisfy; a silent edit to any of them
would relabel contacts without anyone deciding to. `classify_profile` is
exercised here with a small fake client, never a real one -- the live model
contract is checked separately, in test_gemini.py.
"""

import json
from types import SimpleNamespace
from typing import get_args

import pytest
from google.cloud import firestore
from pydantic import ValidationError

from profiles import (
    SUMMARY_MIN_LEN,
    Function,
    Industry,
    ProfileAnalysis,
    Seniority,
    analysis_body,
    classify_profile,
)

# --- analysis_body --------------------------------------------------------


def _extracted(**overrides):
    """One `extracted` document, projected to what `analysis_body` reads."""
    base = {
        "profileUrl": "https://www.linkedin.com/in/jane-doe",
        "lhId": "12345",
        "summary": "Director of Revenue Cycle at Example Pathology",
        "memberDistance": "DISTANCE_2",
    }
    base.update(overrides)
    return base


def _result(industry="RCM", function="Operations", seniority="Director"):
    return ProfileAnalysis(industry=industry, function=function, seniority=seniority)


def test_analysis_body_has_exactly_the_eight_expected_keys():
    body = analysis_body(_extracted(), _result())

    assert set(body.keys()) == {
        "profileUrl", "lh_id", "industry", "function", "seniority",
        "summary", "memberDistance", "created_at",
    }


def test_analysis_body_collapses_newlines_in_the_summary_to_spaces():
    """The classifier reads a flattened dump; a stored summary with the
    newlines still in it would not be what was actually classified."""
    body = analysis_body(_extracted(summary="Line one\nLine two"), _result())

    assert body["summary"] == "Line one Line two"


def test_analysis_body_coerces_lhId_to_str():
    body = analysis_body(_extracted(lhId=12345), _result())

    assert body["lh_id"] == "12345"
    assert isinstance(body["lh_id"], str)


def test_analysis_body_maps_a_missing_profileUrl_to_empty_string_not_none():
    extracted = _extracted()
    del extracted["profileUrl"]

    assert analysis_body(extracted, _result())["profileUrl"] == ""


def test_analysis_body_reads_memberDistance_through_get_member_distance():
    """Delegates to `get_member_distance` instead of reading the field
    directly, so all four historical shapes of that field are handled."""
    extracted = _extracted(memberDistance={"memberDistance": "DISTANCE_3"})

    assert analysis_body(extracted, _result())["memberDistance"] == 3


def test_analysis_body_sets_created_at_to_the_server_timestamp():
    body = analysis_body(_extracted(), _result())

    assert body["created_at"] == firestore.SERVER_TIMESTAMP


# --- ProfileAnalysis / vocabulary ------------------------------------------


def test_profile_analysis_rejects_an_industry_outside_the_vocabulary():
    with pytest.raises(ValidationError):
        ProfileAnalysis(industry="Bank", function="Operations", seniority="Director")


def test_the_three_vocabularies_contain_exactly_the_expected_members():
    """Written out in full so a silent edit to the vocabulary -- a typo, a
    reorder, an addition -- fails here instead of surfacing later as a
    contact mislabeled by a taxonomy nobody agreed to change."""
    assert get_args(Industry) == (
        "RCM", "Pathology", "Medical Lab", "Physician Practice", "Hospital",
        "Health IT", "Health Insurance Payer", "Pharma", "Other Healthcare",
        "Non-Healthcare",
    )
    assert len(get_args(Industry)) == 10

    assert get_args(Function) == (
        "Operations", "Finance", "IT", "Clinical", "Executive", "Consulting",
        "Owner", "Sales & Marketing", "Other",
    )
    assert len(get_args(Function)) == 9

    assert get_args(Seniority) == (
        "Executive", "VP", "Director", "Manager", "Staff", "Owner", "Unknown",
    )
    assert len(get_args(Seniority)) == 7


def test_summary_min_len_is_50():
    """A shorter summary is a stub LinkedIn withheld, not a profile -- both
    notebooks skip classifying it, and a changed threshold changes who gets
    skipped."""
    assert SUMMARY_MIN_LEN == 50


# --- classify_profile -------------------------------------------------------


def _client(generate_content):
    """A stand-in for `genai.Client`: only `.models.generate_content` is read."""
    return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))


def test_classify_profile_returns_the_parsed_result_on_a_good_response():
    parsed = _result(industry="Pathology", function="Clinical", seniority="Staff")
    client = _client(lambda **kwargs: SimpleNamespace(parsed=parsed, text="{}"))

    assert classify_profile(client, "some profile text") is parsed


def test_classify_profile_falls_back_to_response_text_when_parsed_is_none():
    """The SDK leaves `.parsed` None whenever it could not build the model
    itself; the raw JSON in `.text` may still satisfy the schema."""
    text = json.dumps({"industry": "RCM", "function": "Operations", "seniority": "Manager"})
    client = _client(lambda **kwargs: SimpleNamespace(parsed=None, text=text))

    result = classify_profile(client, "some profile text")

    assert result == ProfileAnalysis(industry="RCM", function="Operations", seniority="Manager")


def test_classify_profile_returns_none_for_a_label_outside_the_vocabulary():
    text = json.dumps({"industry": "Bank", "function": "Operations", "seniority": "Manager"})
    client = _client(lambda **kwargs: SimpleNamespace(parsed=None, text=text))

    assert classify_profile(client, "some profile text") is None


def test_classify_profile_returns_none_when_the_client_raises():
    def _boom(**kwargs):
        raise RuntimeError("network error")

    assert classify_profile(_client(_boom), "some profile text") is None


def test_classify_profile_returns_none_for_a_response_that_is_not_json():
    """A truncated or prose response is a different failure from a bad label,
    and it must not escape as a raw parse error into a caller counting Nones."""
    client = _client(lambda **kwargs: SimpleNamespace(parsed=None, text="not json {{{"))

    assert classify_profile(client, "some profile text") is None


def test_classify_profile_logs_which_profile_failed(caplog):
    """The notebook this replaced printed the summary alongside the error. A
    service running unattended over hundreds of connections needs the same:
    without it a warning says only that something failed, not what."""
    def _boom(**kwargs):
        raise RuntimeError("upstream is down")

    with caplog.at_level("WARNING", logger="profiles"):
        classify_profile(_client(_boom), "Director of Revenue Cycle at Example Pathology")

    logged = "".join(record.getMessage() for record in caplog.records)
    assert "Director of Revenue Cycle" in logged
    assert "upstream is down" in logged


def test_classify_profile_truncates_a_long_summary_in_the_log(caplog):
    """Summaries run to 35 KB. A failure log is a breadcrumb, not a copy of the
    profile, and an unbounded one would bury every other line in the log."""
    def _boom(**kwargs):
        raise RuntimeError("upstream is down")

    with caplog.at_level("WARNING", logger="profiles"):
        classify_profile(_client(_boom), "x" * 5000)

    logged = "".join(record.getMessage() for record in caplog.records)
    assert len(logged) < 500
    assert "..." in logged
