"""Tests for `linkedinmcp.contacts`: the read helpers behind the read-only MCP
tools -- `list_contacts`, `get_contact`, `get_conversation` -- run directly
against a seeded `FakeFirestore`, never through the MCP layer.

Every query here is single-field (ruling P2-3): one `where`, chosen by
priority (`stage` > `industry` > `needs_touch` > `since`), or with none of
them two single-field `order_by`s, with every other requested filter applied
in Python. Since
`FakeFirestore` does not log the calls made against it, "which query ran" is
proven by the RESULT SET each filter choice produces (the brief's own
fallback: "assert which query ran if the fake records calls, otherwise the
result set") -- most tests below seed contacts that would appear in the
result under one query shape and not another.

`analysis` and `extracted` both carry fields (`email1`, `email`,
`phoneNumbers`, ...) this module must never return; several tests seed those
fields explicitly and then assert their absence from every returned key.
"""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from linkedinmcp import contacts, queue
from tests.linkedinmcp.fake_firestore import FakeFirestore

# Deliberately unlike `OutreachSettings`' defaults (5 and 3), matching the
# CAPS_ENV values `test_mcp_server.py` already uses -- a test that passed
# against the real defaults by accident fails here.
SETTINGS = SimpleNamespace(tz="Asia/Kolkata", min_days_between_touches=9, max_touches=2)

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def seed_analysis(db, doc_id, **fields):
    db.collection(contacts.ANALYSIS_COLLECTION).document(doc_id).set(fields)
    return doc_id


def seed_extracted(db, doc_id, **fields):
    db.collection(contacts.EXTRACTED_COLLECTION).document(doc_id).set(fields)


def seed_message(db, msg_id, **fields):
    db.collection("messages").document(msg_id).set(fields)


# --- list_contacts(): server-side filter choice -----------------------------


def test_list_contacts_with_stage_returns_only_that_stage():
    db = FakeFirestore()
    seed_analysis(db, "a", pipeline_stage="lead", last_reply_date=NOW)
    seed_analysis(db, "b", pipeline_stage="prospect", last_reply_date=NOW)

    rows = contacts.list_contacts(db, SETTINGS, NOW, stage="lead")

    assert [row["doc_id"] for row in rows] == ["a"]
    assert rows[0]["stage"] == "lead"


def test_list_contacts_with_industry_returns_only_that_industry():
    db = FakeFirestore()
    seed_analysis(db, "a", industry="Pathology", last_reply_date=NOW)
    seed_analysis(db, "b", industry="RCM", last_reply_date=NOW)

    rows = contacts.list_contacts(db, SETTINGS, NOW, industry="Pathology")

    assert [row["doc_id"] for row in rows] == ["a"]
    assert rows[0]["industry"] == "Pathology"


def test_list_contacts_with_needs_touch_returns_only_prospects_needing_one():
    db = FakeFirestore()
    seed_analysis(
        db, "due", pipeline_stage="prospect", last_sent_date=NOW - timedelta(days=10),
        sent_total=1,
    )
    seed_analysis(db, "not-prospect", pipeline_stage="lead", last_sent_date=NOW - timedelta(days=10))

    rows = contacts.list_contacts(db, SETTINGS, NOW, needs_touch=True)

    assert [row["doc_id"] for row in rows] == ["due"]


def test_list_contacts_with_since_returns_only_replies_on_or_after_it():
    db = FakeFirestore()
    seed_analysis(db, "recent", last_reply_date=datetime(2026, 9, 6, tzinfo=UTC))
    seed_analysis(db, "old", last_reply_date=datetime(2026, 9, 1, tzinfo=UTC))

    rows = contacts.list_contacts(db, SETTINGS, NOW, since=date(2026, 9, 5))

    assert [row["doc_id"] for row in rows] == ["recent"]


def test_list_contacts_with_no_filters_orders_by_most_recent_activity():
    db = FakeFirestore()
    seed_analysis(db, "older", last_reply_date=datetime(2026, 9, 1, tzinfo=UTC))
    seed_analysis(db, "newer", last_reply_date=datetime(2026, 9, 8, tzinfo=UTC))

    rows = contacts.list_contacts(db, SETTINGS, NOW)

    assert [row["doc_id"] for row in rows] == ["newer", "older"]


def test_list_contacts_with_no_filters_includes_someone_messaged_recently_who_never_replied():
    """The default page is the most recently active, a send counting as
    much as a reply: the contact messaged an hour ago leads it, though they
    never replied and older repliers alone would fill the page."""
    db = FakeFirestore()
    for n in range(3):
        seed_analysis(db, f"replied-{n}", last_reply_date=NOW - timedelta(days=10 + n))
    seed_analysis(db, "messaged-today", last_sent_date=NOW - timedelta(hours=1))

    rows = contacts.list_contacts(db, SETTINGS, NOW, limit=2)

    assert [row["doc_id"] for row in rows] == ["messaged-today", "replied-0"]


def test_list_contacts_combines_primary_query_with_python_side_industry_filter():
    """`stage` outranks `industry`, so `industry` is applied in Python on top
    of the server-side `stage` query -- a contact matching `stage` but not
    `industry` must still be excluded.
    """
    db = FakeFirestore()
    seed_analysis(db, "match", pipeline_stage="lead", industry="Pathology", last_reply_date=NOW)
    seed_analysis(db, "wrong-industry", pipeline_stage="lead", industry="RCM", last_reply_date=NOW)

    rows = contacts.list_contacts(db, SETTINGS, NOW, stage="lead", industry="Pathology")

    assert [row["doc_id"] for row in rows] == ["match"]


# --- list_contacts(): needs_touch, one condition per test -------------------


def _qualifying_prospect(db, doc_id="p", **overrides):
    fields = dict(
        pipeline_stage="prospect",
        last_sent_date=NOW - timedelta(days=10),  # >= 9 days (SETTINGS.min_days_between_touches)
        last_reply_date=None,
        sent_total=1,  # < 2 (SETTINGS.max_touches)
        handling=None,
    )
    fields.update(overrides)
    return seed_analysis(db, doc_id, **fields)


def test_needs_touch_baseline_contact_is_included():
    db = FakeFirestore()
    _qualifying_prospect(db, "p")

    rows = contacts.list_contacts(db, SETTINGS, NOW, needs_touch=True)

    assert [row["doc_id"] for row in rows] == ["p"]


def test_needs_touch_excludes_a_stage_other_than_prospect():
    db = FakeFirestore()
    _qualifying_prospect(db, "p")
    seed_analysis(
        db, "not-prospect", pipeline_stage="lead", last_sent_date=NOW - timedelta(days=10),
        sent_total=1, handling=None,
    )

    rows = contacts.list_contacts(db, SETTINGS, NOW, needs_touch=True)

    assert [row["doc_id"] for row in rows] == ["p"]


def test_needs_touch_excludes_a_contact_never_sent_to():
    db = FakeFirestore()
    _qualifying_prospect(db, "p")
    _qualifying_prospect(db, "never-sent", last_sent_date=None)

    rows = contacts.list_contacts(db, SETTINGS, NOW, needs_touch=True)

    assert [row["doc_id"] for row in rows] == ["p"]


def test_needs_touch_excludes_a_contact_sent_too_recently():
    db = FakeFirestore()
    _qualifying_prospect(db, "p")
    _qualifying_prospect(db, "too-recent", last_sent_date=NOW - timedelta(days=3))

    rows = contacts.list_contacts(db, SETTINGS, NOW, needs_touch=True)

    assert [row["doc_id"] for row in rows] == ["p"]


def test_needs_touch_excludes_a_contact_who_replied_after_the_last_send():
    db = FakeFirestore()
    _qualifying_prospect(db, "p")
    last_sent = NOW - timedelta(days=10)
    _qualifying_prospect(
        db, "already-replied", last_sent_date=last_sent, last_reply_date=last_sent + timedelta(days=1),
    )

    rows = contacts.list_contacts(db, SETTINGS, NOW, needs_touch=True)

    assert [row["doc_id"] for row in rows] == ["p"]


def test_needs_touch_excludes_a_contact_at_the_touch_cap():
    db = FakeFirestore()
    _qualifying_prospect(db, "p")
    _qualifying_prospect(db, "capped", sent_total=SETTINGS.max_touches)  # 2 is not < 2

    rows = contacts.list_contacts(db, SETTINGS, NOW, needs_touch=True)

    assert [row["doc_id"] for row in rows] == ["p"]


def test_needs_touch_excludes_a_handling_hold_trimmed_and_lowercased():
    db = FakeFirestore()
    _qualifying_prospect(db, "p")
    _qualifying_prospect(db, "held", handling="  Exclude  ")  # functions.HANDLING_HOLDS

    rows = contacts.list_contacts(db, SETTINGS, NOW, needs_touch=True)

    assert [row["doc_id"] for row in rows] == ["p"]


def test_needs_touch_excludes_a_contact_with_an_open_queue_item():
    db = FakeFirestore()
    _qualifying_prospect(db, "p")
    _qualifying_prospect(db, "already-queued")
    queue.enqueue(
        db, "agent:already-queued:20260910",
        {"contact_doc_id": "already-queued", "kind": "follow_up", "text": "hi"},
        require_approval=False, now=NOW,
    )

    rows = contacts.list_contacts(db, SETTINGS, NOW, needs_touch=True)

    assert [row["doc_id"] for row in rows] == ["p"]


# --- list_contacts(): name fallback order -----------------------------------


def test_name_uses_analysis_first_and_last_name_when_present():
    db = FakeFirestore()
    seed_analysis(db, "a", firstName="Jane", lastName="Doe", last_reply_date=NOW)
    seed_extracted(db, "a", fullName="Someone Else")

    rows = contacts.list_contacts(db, SETTINGS, NOW)

    assert rows[0]["name"] == "Jane Doe"


def test_name_falls_back_to_extracted_full_name():
    db = FakeFirestore()
    seed_analysis(db, "a", last_reply_date=NOW)
    seed_extracted(db, "a", fullName="Jane Doe")

    rows = contacts.list_contacts(db, SETTINGS, NOW)

    assert rows[0]["name"] == "Jane Doe"


def test_name_is_none_when_neither_source_has_one():
    db = FakeFirestore()
    seed_analysis(db, "a", last_reply_date=NOW)

    rows = contacts.list_contacts(db, SETTINGS, NOW)

    assert rows[0]["name"] is None


def test_headline_comes_from_extracted_occupation():
    db = FakeFirestore()
    seed_analysis(db, "a", last_reply_date=NOW)
    seed_extracted(db, "a", occupation="VP of Revenue Cycle")

    rows = contacts.list_contacts(db, SETTINGS, NOW)

    assert rows[0]["headline"] == "VP of Revenue Cycle"


# --- list_contacts(): profile_url fallback -----------------------------------


def test_profile_url_falls_back_to_the_linkedin_slug_url():
    db = FakeFirestore()
    seed_analysis(db, "jane-doe-123", last_reply_date=NOW)

    rows = contacts.list_contacts(db, SETTINGS, NOW)

    assert rows[0]["profile_url"] == "https://www.linkedin.com/in/jane-doe-123"


# --- list_contacts(): sort order --------------------------------------------


def test_list_contacts_sorts_by_most_recent_activity_neither_last():
    db = FakeFirestore()
    day1, day2, day3, day4 = (datetime(2026, 9, d, tzinfo=UTC) for d in (1, 2, 3, 4))
    # Shared `pipeline_stage` so the server query is `stage`, and a missing
    # `last_reply_date` does not exclude a candidate the way the default
    # order_by("last_reply_date") branch would.
    seed_analysis(db, "reply-newest", pipeline_stage="prospect", last_reply_date=day3, last_sent_date=day1)
    seed_analysis(db, "sent-newest", pipeline_stage="prospect", last_sent_date=day4)
    seed_analysis(db, "reply-only", pipeline_stage="prospect", last_reply_date=day2)
    seed_analysis(db, "neither", pipeline_stage="prospect")

    rows = contacts.list_contacts(db, SETTINGS, NOW, stage="prospect")

    assert [row["doc_id"] for row in rows] == ["sent-newest", "reply-newest", "reply-only", "neither"]


# --- list_contacts(): since as a local-midnight bound ------------------------


def test_since_is_local_midnight_in_settings_tz_not_utc_midnight():
    """Asia/Kolkata is UTC+05:30, so local midnight of 2026-09-05 is
    2026-09-04T18:30:00Z. A reply 30 minutes on either side of that instant
    proves the bound is the LOCAL midnight, not a UTC one -- a UTC-midnight
    implementation would place both replies on the same side.
    """
    db = FakeFirestore()
    seed_analysis(db, "just-after", last_reply_date=datetime(2026, 9, 4, 19, 0, tzinfo=UTC))
    seed_analysis(db, "just-before", last_reply_date=datetime(2026, 9, 4, 18, 0, tzinfo=UTC))

    rows = contacts.list_contacts(db, SETTINGS, NOW, since=date(2026, 9, 5))

    assert [row["doc_id"] for row in rows] == ["just-after"]


def test_since_accepts_an_iso_date_string():
    db = FakeFirestore()
    seed_analysis(db, "recent", last_reply_date=datetime(2026, 9, 6, tzinfo=UTC))

    rows = contacts.list_contacts(db, SETTINGS, NOW, since="2026-09-05")

    assert [row["doc_id"] for row in rows] == ["recent"]


def test_since_with_an_unparseable_string_raises_value_error():
    """The MCP tool layer, not this module, is on the hook for turning this
    into `{"ok": false, ...}` -- contract §4's never-raise rule binds tools,
    not `contacts.py`'s plain helpers.
    """
    db = FakeFirestore()

    with pytest.raises(ValueError):
        contacts.list_contacts(db, SETTINGS, NOW, since="not-a-date")


# --- list_contacts(): limit clamp -------------------------------------------


def test_limit_below_one_is_clamped_to_one():
    db = FakeFirestore()
    seed_analysis(db, "a", last_reply_date=datetime(2026, 9, 1, tzinfo=UTC))
    seed_analysis(db, "b", last_reply_date=datetime(2026, 9, 2, tzinfo=UTC))

    rows = contacts.list_contacts(db, SETTINGS, NOW, limit=0)

    assert len(rows) == 1


def test_limit_above_100_is_clamped_to_100():
    db = FakeFirestore()
    for i in range(105):
        seed_analysis(db, f"c{i:03d}", pipeline_stage="prospect", last_reply_date=NOW - timedelta(days=i))

    rows = contacts.list_contacts(db, SETTINGS, NOW, stage="prospect", limit=1000)

    assert len(rows) == 100


# --- list_contacts(): never leaks personal fields ---------------------------


def test_list_contacts_rows_never_carry_email_phone_or_summary_keys():
    db = FakeFirestore()
    seed_analysis(
        db, "a", last_reply_date=NOW, email1="jane@example.com", email2="alt@example.com",
        summary="x" * 100,
    )
    seed_extracted(db, "a", email="jane@example.com", phoneNumbers=["+15551234567"], fullName="Jane Doe")

    rows = contacts.list_contacts(db, SETTINGS, NOW)

    assert len(rows) == 1
    leaked = [key for key in rows[0] if key.lower().startswith(("email", "phone"))]
    assert leaked == []
    assert "summary" not in rows[0]


# --- get_contact() -----------------------------------------------------------


def test_get_contact_returns_none_for_a_missing_document():
    db = FakeFirestore()

    assert contacts.get_contact(db, SETTINGS, "missing") is None


def test_get_contact_includes_the_row_fields_plus_the_extra_ones():
    db = FakeFirestore()
    seed_analysis(
        db, "a", firstName="Jane", lastName="Doe", industry="Pathology",
        pipeline_stage="lead", pipeline_reason="asked for a demo",
        pipeline_classified_at=datetime(2026, 9, 1, tzinfo=UTC),
        intro_sent_at=datetime(2026, 8, 1, tzinfo=UTC),
        summary="short summary",
    )
    seed_extracted(db, "a", occupation="VP")

    contact = contacts.get_contact(db, SETTINGS, "a")

    assert contact["doc_id"] == "a"
    assert contact["name"] == "Jane Doe"
    assert contact["headline"] == "VP"
    assert contact["stage"] == "lead"
    assert contact["summary"] == "short summary"
    assert contact["summary_truncated"] is False
    assert contact["pipeline_classified_at"] is not None
    assert contact["intro_sent_at"] is not None
    assert contact["queue"] == []


def test_get_contact_truncates_summary_to_4000_chars_unless_full():
    db = FakeFirestore()
    long_summary = "x" * 5000
    seed_analysis(db, "a", summary=long_summary)

    default = contacts.get_contact(db, SETTINGS, "a")
    full = contacts.get_contact(db, SETTINGS, "a", full=True)

    assert len(default["summary"]) == 4000
    assert default["summary_truncated"] is True
    assert full["summary"] == long_summary
    assert full["summary_truncated"] is False


def test_get_contact_includes_up_to_10_queue_items_newest_first():
    db = FakeFirestore()
    seed_analysis(db, "a")
    for i in range(12):
        queue.enqueue(
            db, f"agent:a:{i:02d}", {"contact_doc_id": "a", "kind": "follow_up", "text": f"msg {i}"},
            require_approval=False, now=NOW + timedelta(minutes=i),
        )

    contact = contacts.get_contact(db, SETTINGS, "a")

    assert len(contact["queue"]) == 10
    assert contact["queue"][0]["text"] == "msg 11"  # newest created_at first
    assert set(contact["queue"][0]) == {"id", "kind", "status", "due_at", "created_by", "text", "tags"}


def test_get_contact_never_carries_email_phone_or_extra_extracted_fields():
    db = FakeFirestore()
    seed_analysis(db, "a", email1="jane@example.com", summary="hi")
    seed_extracted(db, "a", email="jane@example.com", phoneNumbers=["+1"], fullName="Jane Doe")

    contact = contacts.get_contact(db, SETTINGS, "a")

    leaked = [key for key in contact if key.lower().startswith(("email", "phone"))]
    assert leaked == []


# --- get_conversation() -------------------------------------------------------


def test_get_conversation_returns_none_when_there_is_no_readable_message():
    db = FakeFirestore()

    assert contacts.get_conversation(db, "a") is None


def test_get_conversation_returns_the_full_shape():
    db = FakeFirestore()
    seed_message(
        db, "m1", contact_doc_id="a", chat_id="chat-1", is_sender=0,
        timestamp=datetime(2026, 9, 1, 10, 0, tzinfo=UTC), text="hello there",
    )
    seed_message(
        db, "m2", contact_doc_id="a", chat_id="chat-1", is_sender=1,
        timestamp=datetime(2026, 9, 1, 11, 0, tzinfo=UTC), text="hi, thanks for connecting",
    )

    conversation = contacts.get_conversation(db, "a")

    assert conversation["doc_id"] == "a"
    assert "hello there" in conversation["transcript"]
    assert conversation["truncated"] is False
    assert conversation["message_count"] == 2
    assert conversation["inbound_total"] == 1
    assert conversation["newest_inbound_date"] == "2026-09-01T10:00:00+00:00"
    assert conversation["chat_ids"] == ["chat-1"]


def test_get_conversation_chat_ids_are_distinct_and_sorted():
    db = FakeFirestore()
    seed_message(
        db, "m1", contact_doc_id="a", chat_id="chat-b", is_sender=0,
        timestamp=datetime(2026, 9, 1, tzinfo=UTC), text="first",
    )
    seed_message(
        db, "m2", contact_doc_id="a", chat_id="chat-a", is_sender=0,
        timestamp=datetime(2026, 9, 2, tzinfo=UTC), text="second",
    )
    seed_message(
        db, "m3", contact_doc_id="a", chat_id="chat-b", is_sender=0,
        timestamp=datetime(2026, 9, 3, tzinfo=UTC), text="third",
    )

    conversation = contacts.get_conversation(db, "a")

    assert conversation["chat_ids"] == ["chat-a", "chat-b"]


def test_get_conversation_truncates_to_the_last_max_chars_keeping_the_newest():
    db = FakeFirestore()
    seed_message(
        db, "m1", contact_doc_id="a", chat_id="c", is_sender=0,
        timestamp=datetime(2026, 9, 1, tzinfo=UTC), text="a" * 200,
    )
    seed_message(
        db, "m2", contact_doc_id="a", chat_id="c", is_sender=1,
        timestamp=datetime(2026, 9, 2, tzinfo=UTC), text="b" * 200,
    )

    full = contacts.get_conversation(db, "a", max_chars=100000)
    clipped = contacts.get_conversation(db, "a", max_chars=50)

    assert clipped["truncated"] is True
    assert clipped["transcript"] == full["transcript"][-50:]
    assert clipped["transcript"].endswith("b" * 50)
