"""`webapp.projection`: one frame from `analysis`, `extracted`, `fetch_queue`
and `messages`, filtered, searched, sorted, paged and counted in polars.
Firestore is `FakeFirestore`; dates are the real `DatetimeWithNanoseconds`
the client returns."""

from datetime import UTC, datetime

import polars as pl
from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from tests.linkedinmcp.fake_firestore import FakeFirestore
from webapp import projection


def _when(day):
    return DatetimeWithNanoseconds(2026, 9, day, 12, 0, tzinfo=UTC)


ANN_CONNECTED = datetime(2020, 6, 22, 15, 1, 13, tzinfo=UTC)


def _message(db, message_id, contact, is_sender, day, text):
    db.collection("messages").document(message_id).set({
        "contact_doc_id": contact, "chat_id": f"chat-{contact}", "is_sender": is_sender, "timestamp": _when(day), "text": text,
    })


def _db():
    db = FakeFirestore()
    analysis = db.collection("analysis")
    analysis.document("ann").set({
        "firstName": "Ann", "lastName": "Lee", "industry": "Pathology", "function": "Executive",
        "seniority": "Owner", "pipeline_stage": "lead", "sent_total": 2, "replied_total": 1,
        "last_sent_date": _when(1), "last_reply_date": _when(3), "handling": " Manual ",
        "summary": "NEVER LOADED", "email1": "ann@example.com",
    })
    analysis.document("bob").set({"industry": "RCM", "seniority": "Staff", "sent_total": 1, "last_sent_date": _when(2)})
    analysis.document("cat").set({})
    extracted = db.collection("extracted")
    # A profile fetched through Unipile carries `occupation`; a LinkedIn Helper
    # document carries the headline inside `miniProfile`, and may carry the
    # connection date in `connect`, in milliseconds.
    extracted.document("bob").set({"fullName": "Bob Ray", "occupation": "Billing Manager at Acme"})
    extracted.document("ann").set({
        "fullName": "Ann Lee", "miniProfile": {"headline": "Owner, Lee Pathology", "avatar": "x"},
        "connect": {"connectedAt": int(ANN_CONNECTED.timestamp() * 1000), "connectedAtISO": "2020-06-22T15:01:13.000Z"},
    })
    extracted.document("zed").set({"fullName": "Not In Analysis"})
    db.collection("fetch_queue").document("bob").set({"connected_at": DatetimeWithNanoseconds(2026, 8, 20, 15, 0, tzinfo=UTC)})
    _message(db, "m1", "ann", 1, 1, "Hello Ann")
    _message(db, "m2", "bob", 1, 2, "Hello Bob")
    _message(db, "m3", "ann", 0, 3, "Tell me more")
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


def test_load_frame_reads_connection_dates_and_messages_received():
    rows = {row["doc_id"]: row for row in projection.load_frame(_db()).to_dicts()}
    assert rows["ann"]["connected_at"] == ANN_CONNECTED  # LinkedIn Helper's `connect.connectedAt`
    assert rows["bob"]["connected_at"] == datetime(2026, 8, 20, 15, 0, tzinfo=UTC)  # `fetch_queue.connected_at`
    assert rows["cat"]["connected_at"] is None
    assert [rows[doc_id]["inbound_total"] for doc_id in ("ann", "bob", "cat")] == [1, 0, 0]


def test_a_wrong_typed_value_is_dropped_not_fatal():
    """Notebooks write `analysis` too: a string date, a float count or a NaN
    name must not stop the whole frame from building."""
    db = _db()
    db.collection("analysis").document("dan").set({
        "firstName": float("nan"), "last_sent_date": "2026-09-11T10:00:00Z", "sent_total": 2.0, "hand_set": "industry",
    })
    db.collection("extracted").document("dan").set({"connect": {"connectedAt": "2020-06-22"}})
    rows = {row["doc_id"]: row for row in projection.load_frame(db).to_dicts()}
    assert rows["dan"]["firstName"] is None
    assert rows["dan"]["last_sent_date"] is None
    assert rows["dan"]["sent_total"] == 2
    assert rows["dan"]["hand_set"] is None
    assert rows["dan"]["connected_at"] is None


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


def test_filters_narrow_by_value_by_none_and_by_messages_sent_and_received():
    db = _db()
    # Dee wrote first and was never answered: a message from her, and the
    # zero tallies `messages_sync` writes for that.
    db.collection("analysis").document("dee").set({"industry": "RCM", "sent_total": 0, "replied_total": 0})
    _message(db, "m4", "dee", 0, 5, "Can we talk?")
    frame = projection.load_frame(db)

    def ids(**chosen):
        page, _total = projection.query(frame, filters=projection.Filters(**chosen))
        return set(page["doc_id"].to_list())

    assert ids(industry="RCM") == {"bob", "dee"}
    assert ids(industry="none") == {"cat"}
    assert ids(handling="manual") == {"ann"}  # stored as " Manual "
    assert ids(stage="lead") == {"ann"}
    assert ids(industry="RCM", seniority="Staff") == {"bob"}
    assert ids(sent=True) == {"ann", "bob"}
    assert ids(sent=False) == {"cat", "dee"}  # no `sent_total`, or 0
    assert ids(received=True) == {"ann", "dee"}  # Dee wrote to us, though her `replied_total` is 0
    assert ids(received=False) == {"bob", "cat"}
    assert ids(sent=False, received=False) == {"cat"}


def test_filters_from_a_query_string_ignore_values_the_data_does_not_hold():
    known = projection.choices(projection.load_frame(_db()))
    assert known["industry"] == ["Pathology", "RCM", "none"]
    assert known["handling"] == ["none", "manual"]
    filters = projection.Filters.from_params({"industry": "RCM", "stage": "nope", "received": "no", "sent": "maybe"}, known)
    assert filters == projection.Filters(industry="RCM", received=False)
    assert filters.params() == {"industry": "RCM", "received": "no"}


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
