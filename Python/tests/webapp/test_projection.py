"""`webapp.projection`: one frame from `analysis` + `extracted`, searched,
sorted, paged and counted in polars. Firestore is `FakeFirestore`; dates
are the real `DatetimeWithNanoseconds` the client returns."""

from datetime import UTC, datetime

import polars as pl
from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from tests.linkedinmcp.fake_firestore import FakeFirestore
from webapp import projection


def _when(day):
    return DatetimeWithNanoseconds(2026, 9, day, 12, 0, tzinfo=UTC)


def _db():
    db = FakeFirestore()
    analysis = db.collection("analysis")
    analysis.document("ann").set({
        "firstName": "Ann", "lastName": "Lee", "industry": "Pathology", "function": "Executive",
        "seniority": "Owner", "pipeline_stage": "lead", "sent_total": 2, "replied_total": 1,
        "last_sent_date": _when(1), "last_reply_date": _when(3), "handling": " Manual ",
        "summary": "NEVER LOADED", "email1": "ann@example.com",
    })
    analysis.document("bob").set({"industry": "RCM", "seniority": "Staff", "last_sent_date": _when(2)})
    analysis.document("cat").set({})
    extracted = db.collection("extracted")
    # A profile fetched through Unipile carries `occupation`; a LinkedIn Helper
    # document carries the headline inside `miniProfile`.
    extracted.document("bob").set({"fullName": "Bob Ray", "occupation": "Billing Manager at Acme"})
    extracted.document("ann").set({"fullName": "Ann Lee", "miniProfile": {"headline": "Owner, Lee Pathology", "avatar": "x"}})
    extracted.document("zed").set({"fullName": "Not In Analysis"})
    return db


def test_load_frame_joins_names_and_never_loads_summary_or_email():
    frame = projection.load_frame(_db())
    assert frame.height == 3
    rows = {row["doc_id"]: row for row in frame.to_dicts()}
    assert rows["ann"]["name"] == "Ann Lee"
    assert rows["bob"]["name"] == "Bob Ray"
    assert rows["bob"]["headline"] == "Billing Manager at Acme"
    assert rows["ann"]["headline"] == "Owner, Lee Pathology"
    assert rows["cat"]["name"] is None and rows["cat"]["headline"] is None
    assert "miniProfile" not in frame.columns
    assert "summary" not in frame.columns and "email1" not in frame.columns
    assert rows["ann"]["activity_at"] == datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    assert frame.schema["last_sent_date"] == pl.Datetime("us", "UTC")


def test_a_wrong_typed_value_is_dropped_not_fatal():
    """Notebooks write `analysis` too: a string date, a float count or a NaN
    name must not stop the whole frame from building."""
    db = _db()
    db.collection("analysis").document("dan").set({
        "firstName": float("nan"), "last_sent_date": "2026-09-11T10:00:00Z", "sent_total": 2.0, "hand_set": "industry",
    })
    rows = {row["doc_id"]: row for row in projection.load_frame(db).to_dicts()}
    assert rows["dan"]["firstName"] is None
    assert rows["dan"]["last_sent_date"] is None
    assert rows["dan"]["sent_total"] == 2
    assert rows["dan"]["hand_set"] is None


def test_query_searches_name_and_headline_case_insensitively_and_pages():
    frame = projection.load_frame(_db())
    page, total = projection.query(frame, q="billing")
    assert total == 1 and page["doc_id"].to_list() == ["bob"]
    page, total = projection.query(frame, q="ANN")
    assert page["doc_id"].to_list() == ["ann"]
    page, total = projection.query(frame, q="lee pathology")
    assert page["doc_id"].to_list() == ["ann"]
    page, total = projection.query(frame, per_page=2, page=2)
    assert total == 3 and page.height == 1


def test_query_sorts_newest_activity_first_by_default_with_nulls_last():
    frame = projection.load_frame(_db())
    page, _total = projection.query(frame)
    assert page["doc_id"].to_list() == ["ann", "bob", "cat"]
    page, _total = projection.query(frame, sort="industry", descending=False)
    assert page["doc_id"].to_list() == ["ann", "bob", "cat"]
    page, _total = projection.query(frame, sort="industry", descending=True)
    assert page["doc_id"].to_list() == ["bob", "ann", "cat"]


def test_query_falls_back_to_activity_for_an_unknown_sort_column():
    frame = projection.load_frame(_db())
    page, _total = projection.query(frame, sort="email1")
    assert page["doc_id"].to_list() == ["ann", "bob", "cat"]


def test_counts_name_the_unset_value_none_and_normalize_handling():
    frame = projection.load_frame(_db())
    assert projection.counts(frame, "industry") == [("Pathology", 1), ("RCM", 1), ("none", 1)]  # ties in ASCII order
    assert projection.counts(frame, "handling") == [("none", 2), ("manual", 1)]


def test_contact_row_finds_one_contact():
    frame = projection.load_frame(_db())
    assert projection.contact_row(frame, "bob")["headline"] == "Billing Manager at Acme"
    assert projection.contact_row(frame, "nobody") is None
    assert projection.contact_row(None, "bob") is None


def test_contacts_holds_the_frame_and_the_build_time():
    db = _db()
    contacts = projection.Contacts(lambda: db)
    assert contacts.frame is None and contacts.built_at is None
    contacts.rebuild()
    assert contacts.frame.height == 3
    assert contacts.built_at is not None
