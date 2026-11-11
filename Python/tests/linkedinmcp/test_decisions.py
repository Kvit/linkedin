"""Tests for `linkedinmcp.decisions`: the async inbox the agent uses to ask a
human a question in one session and read the answer in a later one.

`ask` (agent questions, auto id) and `raise_alert` (service alerts,
deterministic `alert:{kind}:{key}` id, create-only) are the two ways a
`decisions/{id}` document comes into being; `answer` and `mark_applied` are
the only two transitions, each a transaction that re-reads the current status
and refuses an illegal move by returning `None`/`False` rather than raising --
the same discipline `queue.py`'s transitions follow, tested against the same
`FakeFirestore`.
"""

from datetime import UTC, datetime, timedelta

import pytest

from linkedinmcp import decisions
from tests.linkedinmcp.fake_firestore import FakeFirestore

START = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


# --- ask() -------------------------------------------------------------------


def test_ask_stores_the_documented_fields_and_returns_its_id():
    db = FakeFirestore()

    decision_id = decisions.ask(
        db, "Send the intro to Jane?", ["yes", "no"], {"contact": "jane-1", "score": 7},
        START, session_id="session-1",
    )

    stored = decisions.get(db, decision_id)
    assert stored == {
        "id": decision_id,
        "question": "Send the intro to Jane?",
        "options": ["yes", "no"],
        "context": {"contact": "jane-1", "score": 7},
        "status": decisions.PENDING,
        "answer": None,
        "asked_by": "agent",
        "asked_at": START,
        "answered_at": None,
        "applied_at": None,
        "session_id": "session-1",
    }


def test_ask_defaults_asked_by_to_agent_and_session_id_to_none():
    db = FakeFirestore()

    decision_id = decisions.ask(db, "Question?", [], {}, START)

    stored = decisions.get(db, decision_id)
    assert stored["asked_by"] == "agent"
    assert stored["session_id"] is None


def test_ask_two_calls_return_different_ids():
    db = FakeFirestore()

    first = decisions.ask(db, "Q1?", [], {}, START)
    second = decisions.ask(db, "Q2?", [], {}, START)

    assert first != second


def test_ask_rejects_a_blank_question():
    db = FakeFirestore()

    with pytest.raises(ValueError):
        decisions.ask(db, "   ", [], {}, START)


def test_ask_rejects_options_that_is_not_a_list_of_strings():
    db = FakeFirestore()

    with pytest.raises(ValueError):
        decisions.ask(db, "Question?", [1, 2], {}, START)


def test_ask_rejects_a_context_that_is_not_a_dict():
    db = FakeFirestore()

    with pytest.raises(ValueError):
        decisions.ask(db, "Question?", [], ["not", "a", "dict"], START)


def test_ask_rejects_a_context_value_that_is_not_a_flat_scalar():
    db = FakeFirestore()

    with pytest.raises(ValueError):
        decisions.ask(db, "Question?", [], {"nested": {"a": 1}}, START)


# --- raise_alert() -------------------------------------------------------


def test_raise_alert_creates_a_document_with_acknowledged_options_and_service_asked_by():
    db = FakeFirestore()

    created = decisions.raise_alert(
        db, "restricted", "account-1", "LinkedIn restricted this account", {"code": 403}, START
    )

    assert created is True
    stored = decisions.get(db, "alert:restricted:account-1")
    assert stored["options"] == ["acknowledged"]
    assert stored["asked_by"] == "service"
    assert stored["status"] == decisions.PENDING
    assert stored["question"] == "LinkedIn restricted this account"
    assert stored["context"] == {"code": 403}


def test_raise_alert_a_second_time_with_the_same_kind_and_key_writes_nothing():
    db = FakeFirestore()
    decisions.raise_alert(db, "restricted", "account-1", "first", {}, START)

    created_again = decisions.raise_alert(
        db, "restricted", "account-1", "second", {}, START + timedelta(hours=1)
    )

    assert created_again is False
    stored = decisions.get(db, "alert:restricted:account-1")
    assert stored["question"] == "first"


def test_raise_alert_rejects_a_slash_in_kind():
    db = FakeFirestore()

    with pytest.raises(ValueError):
        decisions.raise_alert(db, "bad/kind", "key", "q", {}, START)


def test_raise_alert_rejects_a_slash_in_key():
    db = FakeFirestore()

    with pytest.raises(ValueError):
        decisions.raise_alert(db, "kind", "bad/key", "q", {}, START)


# --- get() -------------------------------------------------------------------


def test_get_returns_none_for_a_missing_decision():
    db = FakeFirestore()

    assert decisions.get(db, "nope") is None


# --- answer() / mark_applied() --------------------------------------------


def test_answer_then_mark_applied():
    db = FakeFirestore()
    decision_id = decisions.ask(db, "Send it?", ["yes", "no"], {}, START)

    answered = decisions.answer(db, decision_id, "yes", START + timedelta(minutes=1))

    assert answered["status"] == decisions.ANSWERED
    assert answered["answer"] == "yes"
    assert answered["answered_at"] == START + timedelta(minutes=1)

    applied = decisions.mark_applied(db, decision_id, START + timedelta(minutes=2))

    assert applied is True
    stored = decisions.get(db, decision_id)
    assert stored["status"] == decisions.APPLIED
    assert stored["applied_at"] == START + timedelta(minutes=2)


def test_answer_accepts_text_that_is_not_one_of_the_options():
    """The contract is explicit: the answer need not be one of `options`."""
    db = FakeFirestore()
    decision_id = decisions.ask(db, "Send it?", ["yes", "no"], {}, START)

    answered = decisions.answer(db, decision_id, "actually, wait a week", START)

    assert answered["answer"] == "actually, wait a week"


def test_answer_returns_none_for_an_already_answered_decision():
    db = FakeFirestore()
    decision_id = decisions.ask(db, "Send it?", ["yes", "no"], {}, START)
    decisions.answer(db, decision_id, "yes", START)

    second = decisions.answer(db, decision_id, "no", START + timedelta(minutes=1))

    assert second is None
    assert decisions.get(db, decision_id)["answer"] == "yes"


def test_answer_returns_none_for_a_blank_answer():
    db = FakeFirestore()
    decision_id = decisions.ask(db, "Send it?", ["yes", "no"], {}, START)

    result = decisions.answer(db, decision_id, "   ", START)

    assert result is None
    assert decisions.get(db, decision_id)["status"] == decisions.PENDING


def test_answer_returns_none_for_a_missing_decision():
    db = FakeFirestore()

    assert decisions.answer(db, "nope", "yes", START) is None


def test_mark_applied_returns_false_for_a_pending_decision():
    db = FakeFirestore()
    decision_id = decisions.ask(db, "Send it?", ["yes", "no"], {}, START)

    assert decisions.mark_applied(db, decision_id, START) is False


# --- list_decisions() ------------------------------------------------------


def test_list_decisions_orders_newest_asked_at_first():
    db = FakeFirestore()
    first = decisions.ask(db, "Q1?", [], {}, START)
    second = decisions.ask(db, "Q2?", [], {}, START + timedelta(minutes=1))

    items = decisions.list_decisions(db)

    assert [item["id"] for item in items] == [second, first]


def test_list_decisions_filters_by_status():
    db = FakeFirestore()
    pending_id = decisions.ask(db, "Pending?", [], {}, START)
    answered_id = decisions.ask(db, "Answered?", [], {}, START)
    decisions.answer(db, answered_id, "yes", START)

    items = decisions.list_decisions(db, status=decisions.PENDING)

    assert [item["id"] for item in items] == [pending_id]


def test_list_decisions_limit_clamps_to_a_minimum_of_1():
    db = FakeFirestore()
    decisions.ask(db, "Q1?", [], {}, START)
    decisions.ask(db, "Q2?", [], {}, START + timedelta(minutes=1))

    items = decisions.list_decisions(db, limit=0)

    assert len(items) == 1


def test_list_decisions_limit_clamps_to_a_maximum_of_100():
    db = FakeFirestore()
    for i in range(105):
        db.collection(decisions.DECISIONS_COLLECTION).document(f"raw-{i}").set(
            {
                "question": "q",
                "options": [],
                "context": {},
                "status": decisions.PENDING,
                "answer": None,
                "asked_by": "agent",
                "asked_at": START + timedelta(seconds=i),
                "answered_at": None,
                "applied_at": None,
                "session_id": None,
            }
        )

    items = decisions.list_decisions(db, limit=1000)

    assert len(items) == 100


# --- counts() -----------------------------------------------------------------


def test_counts_reports_zero_when_nothing_is_stored():
    db = FakeFirestore()

    assert decisions.counts(db) == {"pending": 0, "answered": 0}


def test_counts_reports_pending_and_answered_and_excludes_applied():
    db = FakeFirestore()
    decisions.ask(db, "Q1?", [], {}, START)
    answered_id = decisions.ask(db, "Q2?", [], {}, START)
    decisions.answer(db, answered_id, "yes", START)
    applied_id = decisions.ask(db, "Q3?", [], {}, START)
    decisions.answer(db, applied_id, "yes", START)
    decisions.mark_applied(db, applied_id, START)

    assert decisions.counts(db) == {"pending": 1, "answered": 1}
