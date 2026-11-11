"""Tests for `linkedinmcp.ledger`: the append-only record of every
service-side LinkedIn action, and the one query the daily-cap check runs
against it.

Ledger ruling P2-3 forbids a second `where` or an `order_by` on this
collection, because either would need a composite index this project does
not create -- `count_since` filters by `kind` and `results` in Python
instead. That "only one query" shape is a design constraint documented in
`ledger.py` itself; these tests cannot distinguish it from a second
server-side filter (both would produce the same counts against
`FakeFirestore`), so what they check is narrower but still real: the counts
`count_since` returns are correct even when a stray `kind` or an
out-of-`results` row is sitting in the same collection.
"""

from datetime import UTC, datetime, timedelta

import pytest

from linkedinmcp import ledger
from tests.linkedinmcp.fake_firestore import FakeFirestore

# --- entry() -------------------------------------------------------------


def test_entry_returns_exactly_the_five_documented_fields():
    at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    data = ledger.entry("message", "contact-1", "sent", at, queue_id="q1")

    assert data == {
        "kind": "message",
        "contact_doc_id": "contact-1",
        "result": "sent",
        "queue_id": "q1",
        "at": at,
    }


def test_entry_queue_id_defaults_to_none():
    at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    data = ledger.entry("message", "contact-1", "sent", at)

    assert data["queue_id"] is None


def test_entry_rejects_an_unknown_kind():
    at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    with pytest.raises(ValueError):
        ledger.entry("carrier_pigeon", "contact-1", "sent", at)


def test_entry_rejects_an_unknown_result():
    at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    with pytest.raises(ValueError):
        ledger.entry("message", "contact-1", "maybe", at)


def test_entry_rejects_a_naive_at():
    naive = datetime(2026, 9, 8, 12, 0)

    with pytest.raises(ValueError):
        ledger.entry("message", "contact-1", "sent", naive)


# --- entry() per-kind result validation (task 3a) -------------------------


def test_kinds_is_message_and_profile():
    assert ledger.KINDS == ("message", "profile")


def test_profile_results_are_the_four_documented_values():
    assert ledger.PROFILE_RESULTS == ("stored", "short", "incomplete", "failed")


def test_entry_accepts_a_profile_result_for_the_profile_kind():
    at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    data = ledger.entry("profile", "contact-1", "short", at)

    assert data == {
        "kind": "profile",
        "contact_doc_id": "contact-1",
        "result": "short",
        "queue_id": None,
        "at": at,
    }


def test_entry_rejects_a_message_only_result_for_the_profile_kind():
    """`sent`/`unknown` are message results; a profile row spelling one of
    them is a bug, not a legal outcome."""
    at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    with pytest.raises(ValueError):
        ledger.entry("profile", "contact-1", "sent", at)


def test_entry_rejects_a_profile_only_result_for_the_message_kind():
    """`stored`/`short`/`incomplete` are profile results; a message row
    spelling one of them is a bug, not a legal outcome."""
    at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    with pytest.raises(ValueError):
        ledger.entry("message", "contact-1", "short", at)


def test_entry_accepts_failed_for_both_kinds():
    """`failed` is the one result the two kinds' result sets share."""
    at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    assert ledger.entry("message", "contact-1", "failed", at)["result"] == "failed"
    assert ledger.entry("profile", "contact-1", "failed", at)["result"] == "failed"


# --- record() -------------------------------------------------------------


def test_record_stores_one_document_per_call_with_auto_ids():
    db = FakeFirestore()
    at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    ledger.record(db, "message", "contact-1", "sent", at)
    ledger.record(db, "message", "contact-2", "sent", at)

    docs = list(db.collection(ledger.LEDGER_COLLECTION).stream())
    assert len(docs) == 2
    assert docs[0].id != docs[1].id


def test_record_writes_the_entry_shape():
    db = FakeFirestore()
    at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    ledger.record(db, "message", "contact-1", "sent", at, queue_id="q1")

    [doc] = list(db.collection(ledger.LEDGER_COLLECTION).stream())
    assert doc.to_dict() == {
        "kind": "message",
        "contact_doc_id": "contact-1",
        "result": "sent",
        "queue_id": "q1",
        "at": at,
    }


def test_record_with_a_batch_stores_nothing_until_commit():
    """`queue.settle` (2c) writes the queue update and this ledger row as one
    atomic batch -- this pins the half of that contract that belongs to
    `ledger.record`: with `batch=`, it only buffers `batch.set(ref, data)`.
    """
    db = FakeFirestore()
    at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    batch = db.batch()

    ledger.record(db, "message", "contact-1", "sent", at, batch=batch)

    assert list(db.collection(ledger.LEDGER_COLLECTION).stream()) == []

    batch.commit()

    docs = list(db.collection(ledger.LEDGER_COLLECTION).stream())
    assert len(docs) == 1
    assert docs[0].to_dict()["contact_doc_id"] == "contact-1"


# --- count_since() -------------------------------------------------------------


def test_count_since_includes_a_row_at_exactly_cutoff_and_excludes_one_before_it():
    db = FakeFirestore()
    cutoff = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)
    ledger.record(db, "message", "at-cutoff", "sent", cutoff)
    ledger.record(db, "message", "before-cutoff", "sent", cutoff - timedelta(microseconds=1))

    assert ledger.count_since(db, "message", cutoff) == 1


def test_count_since_filters_by_kind():
    """`count_since` must exclude a row of a different `kind` from the count
    rather than assume every row the range query returns already matches
    `kind` -- true of `"profile"` (task 3a's kind) just as much as any other.
    Writes a non-"message" row straight to the collection (bypassing
    `entry`'s own validation, the way a stray write from elsewhere could) to
    prove the exclusion actually happens.
    """
    db = FakeFirestore()
    cutoff = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    ledger.record(db, "message", "contact-1", "sent", cutoff)
    db.collection(ledger.LEDGER_COLLECTION).document().set(
        {
            "kind": "other",
            "contact_doc_id": "contact-2",
            "result": "sent",
            "queue_id": None,
            "at": cutoff,
        }
    )

    assert ledger.count_since(db, "message", cutoff) == 1


def test_count_since_filters_by_results_when_given():
    db = FakeFirestore()
    cutoff = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    ledger.record(db, "message", "contact-1", "sent", cutoff)
    ledger.record(db, "message", "contact-2", "failed", cutoff)

    assert ledger.count_since(db, "message", cutoff, results=("sent",)) == 1
    assert ledger.count_since(db, "message", cutoff) == 2
