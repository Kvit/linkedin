"""Tests for `linkedinmcp.queue`: the `outreach_queue` state machine -- one
document per outbound LinkedIn message, claimed exactly once before a send is
attempted and settled exactly once after.

Every transition function (`claim`, `release`, `mark_skipped`, `mark_unknown`,
`resolve_unknown`, `approve`, `cancel`) is a transaction that re-reads the
current status and refuses an illegal move by returning `False`/`None` --
never by raising. `settle` is the one exception: it is a plain read followed
by ONE `db.batch()` (queue update + ledger row +, for a sent/unknown intro, an
`analysis` merge), and it DOES raise (`RuntimeError`) when the item is not
`sending`, because settle is only ever called right after a `claim` the
caller itself just won -- there is no concurrent caller to race against, so a
wrong status there is a bug, not a lost race.

`db.contend_once()` proves `claim`'s transaction is driven by the REAL
`@firestore.transactional` decorator (see `fake_firestore.py`'s own
docstring) rather than a plain read-then-write.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from linkedinmcp import ledger, queue
from tests.linkedinmcp.fake_firestore import FakeFirestore

START = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def approved_item(db, queue_id="q1", contact_doc_id="contact-1", kind="follow_up", text="hello", now=START, **overrides):
    """Enqueue with `require_approval=False` and a non-`reply` kind, so the
    item lands `approved` -- the starting point most transition tests need.
    """
    item = {"contact_doc_id": contact_doc_id, "kind": kind, "text": text, **overrides}
    stored, _created = queue.enqueue(db, queue_id, item, require_approval=False, now=now)
    return stored


def pending_item(db, queue_id="q1", contact_doc_id="contact-1", kind="follow_up", text="hello", now=START, **overrides):
    """Enqueue with `require_approval=True`, so the item lands `pending`."""
    item = {"contact_doc_id": contact_doc_id, "kind": kind, "text": text, **overrides}
    stored, _created = queue.enqueue(db, queue_id, item, require_approval=True, now=now)
    return stored


def claimed_item(db, queue_id="q1", contact_doc_id="contact-1", kind="follow_up", owner="owner-1", now=START, **overrides):
    """An `approved` item immediately claimed, so it lands `sending`."""
    approved_item(db, queue_id=queue_id, contact_doc_id=contact_doc_id, kind=kind, now=now, **overrides)
    return queue.claim(db, queue_id, owner, now)


# --- intro_id() / agent_id() ----------------------------------------------


def test_intro_id_is_prefixed_with_the_doc_id():
    assert queue.intro_id("contact-1") == "intro:contact-1"


def test_agent_id_formats_the_day_as_yyyymmdd():
    from datetime import date

    assert queue.agent_id("contact-1", date(2026, 9, 8)) == "agent:contact-1:20260908"


# --- enqueue() --------------------------------------------------------------


def test_enqueue_with_approval_off_is_approved_with_auto_approval_fields():
    db = FakeFirestore()

    item, created = queue.enqueue(
        db, "q1", {"contact_doc_id": "contact-1", "kind": "follow_up", "text": "hi"},
        require_approval=False, now=START,
    )

    assert created is True
    assert item["id"] == "q1"
    assert item["status"] == queue.APPROVED
    assert item["approved_by"] == "auto"
    assert item["approved_at"] == START
    assert item["created_at"] == START
    assert item["due_at"] == START
    assert item["created_by"] == "agent"


def test_enqueue_always_stores_tags():
    """Every queue item carries `tags` -- `[]` when none were given -- so a
    campaign's messages can be found, and every item has the same shape."""
    db = FakeFirestore()

    untagged = approved_item(db, "q1")
    tagged = approved_item(db, "q2", tags=["recovr", "stage-1"])

    assert (untagged["tags"], tagged["tags"]) == ([], ["recovr", "stage-1"])


def test_clean_tags_lowercases_and_dedupes_and_refuses_the_rest():
    assert queue.clean_tags(None) == []
    assert queue.clean_tags([" Recovr ", "stage-1", "recovr", "iter_2.b"]) == ["recovr", "stage-1", "iter_2.b"]
    for bad in ("recovr", [1], [""], ["has space"], ["-leading"], ["x" * 41], [f"t{n}" for n in range(11)]):
        with pytest.raises(ValueError):
            queue.clean_tags(bad)


def test_tagged_sends_names_each_contact_sent_a_message_with_every_tag():
    """Sent items only, carrying all the tags asked for; each contact once,
    with the time their newest such message went."""
    db = FakeFirestore()
    both = ["recovr", "stage-1"]
    for queue_id, contact, tags, day in (
        ("q1", "a", both, 1), ("q2", "a", both, 3), ("q3", "b", ["recovr"], 1), ("q4", "c", both, 1),
    ):
        claimed_item(db, queue_id, contact, now=START + timedelta(days=day), tags=tags)
        if queue_id != "q4":
            queue.settle(db, queue_id, queue.SENT, now=START + timedelta(days=day), message_id=f"m-{queue_id}")

    assert queue.tagged_sends(db, both) == {"a": START + timedelta(days=3)}


def test_enqueue_with_approval_on_is_pending():
    db = FakeFirestore()

    item, created = queue.enqueue(
        db, "q1", {"contact_doc_id": "contact-1", "kind": "follow_up", "text": "hi"},
        require_approval=True, now=START,
    )

    assert created is True
    assert item["status"] == queue.PENDING
    assert item.get("approved_by") is None
    assert item.get("approved_at") is None


def test_enqueue_of_a_reply_with_approval_off_is_still_pending():
    """A `reply` always needs a human's yes before it goes out, regardless of
    `require_approval` -- it is text a stranger has not seen yet.
    """
    db = FakeFirestore()

    item, created = queue.enqueue(
        db, "q1", {"contact_doc_id": "contact-1", "kind": "reply", "text": "hi"},
        require_approval=False, now=START,
    )

    assert created is True
    assert item["status"] == queue.PENDING


def test_enqueue_of_an_existing_id_returns_created_false_and_the_stored_text_unchanged():
    db = FakeFirestore()
    queue.enqueue(
        db, "q1", {"contact_doc_id": "contact-1", "kind": "follow_up", "text": "first text"},
        require_approval=False, now=START,
    )

    item, created = queue.enqueue(
        db, "q1", {"contact_doc_id": "contact-1", "kind": "follow_up", "text": "second text"},
        require_approval=False, now=START + timedelta(minutes=5),
    )

    assert created is False
    assert item["text"] == "first text"


def test_enqueue_rejects_an_unknown_key():
    db = FakeFirestore()

    with pytest.raises(ValueError):
        queue.enqueue(
            db, "q1",
            {"contact_doc_id": "contact-1", "kind": "follow_up", "text": "hi", "surprise": "!"},
            require_approval=False, now=START,
        )


def test_enqueue_rejects_a_kind_outside_kinds():
    db = FakeFirestore()

    with pytest.raises(ValueError):
        queue.enqueue(
            db, "q1", {"contact_doc_id": "contact-1", "kind": "carrier_pigeon", "text": "hi"},
            require_approval=False, now=START,
        )


def test_enqueue_rejects_a_blank_text():
    db = FakeFirestore()

    with pytest.raises(ValueError):
        queue.enqueue(
            db, "q1", {"contact_doc_id": "contact-1", "kind": "follow_up", "text": "   "},
            require_approval=False, now=START,
        )


def test_enqueue_rejects_an_empty_contact_doc_id():
    db = FakeFirestore()

    with pytest.raises(ValueError):
        queue.enqueue(
            db, "q1", {"contact_doc_id": "", "kind": "follow_up", "text": "hi"},
            require_approval=False, now=START,
        )


def test_enqueue_rejects_a_naive_due_at_and_writes_nothing():
    """A `due_at` given without tzinfo raises `ValueError` naming the field,
    before any Firestore write -- mirrors `ledger.entry`'s "never guess a
    timezone" rule. The error names `due_at` but never echoes `text`, and the
    queue collection stays empty (not just that `get()` returns `None`).
    """
    db = FakeFirestore()
    naive_due_at = datetime(2026, 9, 9, 12, 0, 0)  # no tzinfo

    with pytest.raises(ValueError, match="due_at") as exc_info:
        queue.enqueue(
            db, "q1",
            {
                "contact_doc_id": "contact-1",
                "kind": "follow_up",
                "text": "NEVER-IN-AN-ERROR-MESSAGE",
                "due_at": naive_due_at,
            },
            require_approval=False, now=START,
        )

    assert "NEVER-IN-AN-ERROR-MESSAGE" not in str(exc_info.value)
    assert list(db.collection(queue.QUEUE_COLLECTION).stream()) == []


def test_enqueue_rejects_a_non_datetime_due_at_and_writes_nothing():
    """A `due_at` that is not a `datetime` at all (a plain string, say) also
    raises `ValueError` naming the field, rather than letting a bare
    `AttributeError` escape from a `.tzinfo` lookup on a value that has no
    such attribute -- and, like the naive case, writes nothing.
    """
    db = FakeFirestore()

    with pytest.raises(ValueError, match="due_at") as exc_info:
        queue.enqueue(
            db, "q1",
            {
                "contact_doc_id": "contact-1",
                "kind": "follow_up",
                "text": "NEVER-IN-AN-ERROR-MESSAGE",
                "due_at": "2026-09-09T12:00:00Z",
            },
            require_approval=False, now=START,
        )

    assert "NEVER-IN-AN-ERROR-MESSAGE" not in str(exc_info.value)
    assert list(db.collection(queue.QUEUE_COLLECTION).stream()) == []


def test_enqueue_accepts_an_aware_due_at_in_a_non_utc_zone_and_next_due_orders_it_correctly():
    """A `due_at` carrying tzinfo other than UTC is accepted -- only a naive
    or non-datetime `due_at` is rejected -- and `next_due` still ranks it
    correctly against both an aware `now` and another item's UTC `due_at`:
    07:00 in UTC+5 is 02:00 UTC, earlier than a plain 12:00 UTC due date, so
    the UTC+5 item must win both `next_due`'s `<= now` filter and its sort
    key, which only works if aware datetimes with different offsets are
    compared as absolute time, not rejected or mis-ordered by raw wall-clock
    numbers.
    """
    db = FakeFirestore()
    plus_five = timezone(timedelta(hours=5))
    earlier_elsewhere = datetime(2026, 9, 8, 7, 0, 0, tzinfo=plus_five)  # == 2026-09-08T02:00:00 UTC

    item, created = queue.enqueue(
        db, "tz-item",
        {"contact_doc_id": "contact-1", "kind": "follow_up", "text": "hi", "due_at": earlier_elsewhere},
        require_approval=False, now=START,
    )
    approved_item(db, queue_id="utc-item", due_at=START)  # == 2026-09-08T12:00:00 UTC, later

    assert created is True
    assert item["due_at"] == earlier_elsewhere

    due = queue.next_due(db, START)

    assert due["id"] == "tz-item"


# --- get() -------------------------------------------------------------------


def test_get_returns_none_for_a_missing_item():
    db = FakeFirestore()

    assert queue.get(db, "nope") is None


def test_get_returns_the_item_with_its_id():
    db = FakeFirestore()
    approved_item(db, queue_id="q1")

    item = queue.get(db, "q1")

    assert item["id"] == "q1"
    assert item["contact_doc_id"] == "contact-1"


# --- next_due() ---------------------------------------------------------------


def test_next_due_skips_a_future_due_at():
    db = FakeFirestore()
    approved_item(db, queue_id="q1", due_at=START + timedelta(hours=1))

    assert queue.next_due(db, START) is None


def test_next_due_skips_a_non_approved_item():
    db = FakeFirestore()
    pending_item(db, queue_id="q1", due_at=START)

    assert queue.next_due(db, START) is None


def test_next_due_returns_none_when_nothing_is_due():
    db = FakeFirestore()

    assert queue.next_due(db, START) is None


def test_next_due_orders_by_the_earliest_due_at():
    db = FakeFirestore()
    approved_item(db, queue_id="later", due_at=START + timedelta(minutes=5))
    approved_item(db, queue_id="earlier", due_at=START)

    item = queue.next_due(db, START + timedelta(hours=1))

    assert item["id"] == "earlier"


def test_next_due_breaks_a_due_at_tie_by_created_at():
    db = FakeFirestore()
    due = START + timedelta(minutes=10)
    approved_item(db, queue_id="created-second", due_at=due, now=START + timedelta(minutes=1))
    approved_item(db, queue_id="created-first", due_at=due, now=START)

    item = queue.next_due(db, START + timedelta(hours=1))

    assert item["id"] == "created-first"


# --- claim() ------------------------------------------------------------------


def test_claim_transitions_approved_to_sending_and_returns_the_updated_item():
    db = FakeFirestore()
    approved_item(db, queue_id="q1")

    item = queue.claim(db, "q1", "owner-1", START)

    assert item["id"] == "q1"
    assert item["status"] == queue.SENDING
    assert item["sending_at"] == START
    assert item["lease_owner"] == "owner-1"
    assert queue.get(db, "q1")["status"] == queue.SENDING


def test_second_claim_of_the_same_item_returns_none():
    db = FakeFirestore()
    approved_item(db, queue_id="q1")
    first = queue.claim(db, "q1", "owner-1", START)

    second = queue.claim(db, "q1", "owner-2", START)

    assert first is not None
    assert second is None
    assert queue.get(db, "q1")["lease_owner"] == "owner-1"


def test_claiming_a_pending_item_returns_none():
    db = FakeFirestore()
    pending_item(db, queue_id="q1")

    assert queue.claim(db, "q1", "owner-1", START) is None


def test_claiming_a_missing_item_returns_none():
    db = FakeFirestore()

    assert queue.claim(db, "nope", "owner-1", START) is None


def test_claim_still_succeeds_exactly_once_when_the_transaction_contends_once():
    """`db.contend_once()` forces the first commit attempt inside `claim`'s
    transaction to abort; the real `@firestore.transactional` decorator must
    retry, and the item it finally returns must be the one actually stored --
    and only stored once, so a second `claim` afterwards still returns
    `None`.
    """
    db = FakeFirestore()
    approved_item(db, queue_id="q1")
    db.contend_once()

    item = queue.claim(db, "q1", "owner-1", START)

    assert item is not None
    assert item["lease_owner"] == "owner-1"
    assert db._contend_once_armed is False  # the arm was consumed by a real _commit()
    assert queue.claim(db, "q1", "owner-2", START) is None


# --- release() ----------------------------------------------------------------


def test_release_refuses_the_wrong_owner():
    db = FakeFirestore()
    claimed_item(db, queue_id="q1", owner="owner-1")

    released = queue.release(db, "q1", "owner-2", START, "not it")

    assert released is False
    item = queue.get(db, "q1")
    assert item["status"] == queue.SENDING
    assert item["lease_owner"] == "owner-1"


def test_release_refuses_a_non_sending_item():
    db = FakeFirestore()
    approved_item(db, queue_id="q1")

    assert queue.release(db, "q1", "owner-1", START, "not sending") is False


def test_release_with_the_right_owner_returns_to_approved_and_clears_the_lease():
    db = FakeFirestore()
    claimed_item(db, queue_id="q1", owner="owner-1")

    released = queue.release(db, "q1", "owner-1", START, "rate limited")

    assert released is True
    item = queue.get(db, "q1")
    assert item["status"] == queue.APPROVED
    assert item["sending_at"] is None
    assert item["lease_owner"] is None
    assert item["error"] == "rate limited"


# --- settle() -----------------------------------------------------------------


def test_settle_rejects_a_status_outside_sent_failed_unknown():
    db = FakeFirestore()
    claimed_item(db, queue_id="q1")

    with pytest.raises(ValueError):
        queue.settle(db, "q1", "pending", now=START)


def test_settle_of_a_non_sending_item_raises_and_writes_nothing():
    db = FakeFirestore()
    approved_item(db, queue_id="q1")  # approved, not sending

    with pytest.raises(RuntimeError):
        queue.settle(db, "q1", queue.SENT, now=START)

    assert queue.get(db, "q1")["status"] == queue.APPROVED
    assert list(db.collection(ledger.LEDGER_COLLECTION).stream()) == []


def test_settle_of_a_missing_item_raises():
    db = FakeFirestore()

    with pytest.raises(RuntimeError):
        queue.settle(db, "nope", queue.SENT, now=START)


def test_settle_sent_for_an_intro_writes_the_item_the_ledger_row_and_intro_sent_at_in_one_batch():
    db = FakeFirestore()
    db.collection("analysis").document("contact-1").set({"name": "Jane"})
    claimed_item(db, queue_id="q1", kind="intro")

    queue.settle(db, "q1", queue.SENT, now=START + timedelta(minutes=1), message_id="msg-1")

    item = queue.get(db, "q1")
    assert item["status"] == queue.SENT
    assert item["settled_at"] == START + timedelta(minutes=1)
    assert item["sent_at"] == START + timedelta(minutes=1)
    assert item["message_id"] == "msg-1"

    [ledger_doc] = list(db.collection(ledger.LEDGER_COLLECTION).stream())
    ledger_data = ledger_doc.to_dict()
    assert ledger_data["result"] == "sent"
    assert ledger_data["contact_doc_id"] == "contact-1"
    assert ledger_data["queue_id"] == "q1"

    analysis = db.collection("analysis").document("contact-1").get().to_dict()
    assert analysis["intro_sent_at"] == START + timedelta(minutes=1)
    assert analysis["name"] == "Jane"  # the merge did not clobber a pre-existing field


def test_settle_for_an_intro_whose_analysis_document_does_not_exist_creates_no_analysis_document():
    db = FakeFirestore()
    claimed_item(db, queue_id="q1", kind="intro")

    queue.settle(db, "q1", queue.SENT, now=START)

    assert db.collection("analysis").document("contact-1").get().exists is False


def test_settle_unknown_for_an_intro_sets_intro_sent_at():
    db = FakeFirestore()
    db.collection("analysis").document("contact-1").set({})
    claimed_item(db, queue_id="q1", kind="intro")

    queue.settle(db, "q1", queue.UNKNOWN, now=START, error="ServerError")

    analysis = db.collection("analysis").document("contact-1").get().to_dict()
    assert analysis["intro_sent_at"] == START


def test_settle_failed_for_an_intro_does_not_set_intro_sent_at():
    db = FakeFirestore()
    db.collection("analysis").document("contact-1").set({})
    claimed_item(db, queue_id="q1", kind="intro")

    queue.settle(db, "q1", queue.FAILED, now=START, error="NotFound")

    analysis = db.collection("analysis").document("contact-1").get().to_dict()
    assert "intro_sent_at" not in analysis


def test_settle_only_sets_sent_at_for_the_sent_status():
    db = FakeFirestore()
    claimed_item(db, queue_id="q1")

    queue.settle(db, "q1", queue.FAILED, now=START, error="boom")

    assert queue.get(db, "q1").get("sent_at") is None


def test_settle_stores_chat_id_when_given():
    db = FakeFirestore()
    claimed_item(db, queue_id="q1")

    queue.settle(db, "q1", queue.SENT, now=START, chat_id="chat-xyz")

    assert queue.get(db, "q1")["chat_id"] == "chat-xyz"


def test_settle_without_a_chat_id_leaves_an_existing_chat_id_untouched():
    db = FakeFirestore()
    claimed_item(db, queue_id="q1", chat_id="chat-abc")

    queue.settle(db, "q1", queue.FAILED, now=START, error="boom")

    assert queue.get(db, "q1")["chat_id"] == "chat-abc"


# --- mark_skipped() -------------------------------------------------------


def test_mark_skipped_transitions_approved_to_skipped():
    db = FakeFirestore()
    approved_item(db, queue_id="q1")

    marked = queue.mark_skipped(db, "q1", "guard refused", START)

    assert marked is True
    item = queue.get(db, "q1")
    assert item["status"] == queue.SKIPPED
    assert item["skip_reason"] == "guard refused"
    assert item["settled_at"] == START


def test_mark_skipped_refuses_a_non_approved_item():
    db = FakeFirestore()
    pending_item(db, queue_id="q1")

    assert queue.mark_skipped(db, "q1", "guard refused", START) is False


# --- mark_unknown() --------------------------------------------------------


def test_mark_unknown_transitions_sending_to_unknown_with_one_ledger_row():
    db = FakeFirestore()
    claimed_item(db, queue_id="q1", kind="follow_up")

    marked = queue.mark_unknown(db, "q1", "stale claim", START + timedelta(hours=1))

    assert marked is True
    item = queue.get(db, "q1")
    assert item["status"] == queue.UNKNOWN
    assert item["error"] == "stale claim"
    assert item["settled_at"] == START + timedelta(hours=1)

    [ledger_doc] = list(db.collection(ledger.LEDGER_COLLECTION).stream())
    ledger_data = ledger_doc.to_dict()
    assert ledger_data["result"] == "unknown"
    assert ledger_data["queue_id"] == "q1"


def test_mark_unknown_refuses_a_non_sending_item():
    db = FakeFirestore()
    approved_item(db, queue_id="q1")

    assert queue.mark_unknown(db, "q1", "stale claim", START) is False


def test_mark_unknown_for_an_intro_merges_intro_sent_at_when_analysis_document_exists():
    db = FakeFirestore()
    db.collection("analysis").document("contact-1").set({})
    claimed_item(db, queue_id="q1", kind="intro")

    queue.mark_unknown(db, "q1", "stale claim", START)

    analysis = db.collection("analysis").document("contact-1").get().to_dict()
    assert analysis["intro_sent_at"] == START


def test_mark_unknown_for_an_intro_creates_no_analysis_document_when_it_does_not_exist():
    db = FakeFirestore()
    claimed_item(db, queue_id="q1", kind="intro")

    queue.mark_unknown(db, "q1", "stale claim", START)

    assert db.collection("analysis").document("contact-1").get().exists is False


# --- claim without settle: the stale-claim story ---------------------------


def test_claim_without_settle_next_due_no_longer_returns_the_item():
    db = FakeFirestore()
    claimed_item(db, queue_id="q1", due_at=START)

    assert queue.next_due(db, START + timedelta(hours=1)) is None


def test_claim_without_settle_stale_sending_returns_it_once_older_than_passes_sending_at():
    db = FakeFirestore()
    claimed_item(db, queue_id="q1", now=START)

    assert queue.stale_sending(db, START) == []

    stale = queue.stale_sending(db, START + timedelta(seconds=1))

    assert [item["id"] for item in stale] == ["q1"]


def test_stale_sending_treats_a_missing_sending_at_as_stale():
    db = FakeFirestore()
    db.collection(queue.QUEUE_COLLECTION).document("q1").set(
        {"status": queue.SENDING, "contact_doc_id": "contact-1", "kind": "follow_up"}
    )

    stale = queue.stale_sending(db, START)

    assert [item["id"] for item in stale] == ["q1"]


def test_claim_without_settle_mark_unknown_moves_it_to_unknown():
    db = FakeFirestore()
    claimed_item(db, queue_id="q1", now=START)

    assert queue.mark_unknown(db, "q1", "stale claim", START + timedelta(hours=1)) is True
    assert queue.get(db, "q1")["status"] == queue.UNKNOWN


# --- resolve_unknown() -----------------------------------------------------


def unknown_item(db, queue_id="q1", contact_doc_id="contact-1", kind="follow_up", now=START):
    claimed_item(db, queue_id=queue_id, contact_doc_id=contact_doc_id, kind=kind, now=now)
    queue.mark_unknown(db, queue_id, "stale claim", now)


def test_resolve_unknown_to_sent():
    db = FakeFirestore()
    unknown_item(db, queue_id="q1")

    resolved = queue.resolve_unknown(db, "q1", sent=True, now=START + timedelta(hours=1), message_id="msg-1")

    assert resolved is True
    item = queue.get(db, "q1")
    assert item["status"] == queue.SENT
    assert item["sent_at"] == START + timedelta(hours=1)
    assert item["message_id"] == "msg-1"


def test_resolve_unknown_to_failed():
    db = FakeFirestore()
    unknown_item(db, queue_id="q1")

    resolved = queue.resolve_unknown(db, "q1", sent=False, now=START + timedelta(hours=1), error="NotFound")

    assert resolved is True
    item = queue.get(db, "q1")
    assert item["status"] == queue.FAILED
    assert item["error"] == "NotFound"


def test_resolve_unknown_writes_no_additional_ledger_row():
    db = FakeFirestore()
    unknown_item(db, queue_id="q1")
    rows_before = len(list(db.collection(ledger.LEDGER_COLLECTION).stream()))

    queue.resolve_unknown(db, "q1", sent=True, now=START + timedelta(hours=1))

    rows_after = len(list(db.collection(ledger.LEDGER_COLLECTION).stream()))
    assert rows_after == rows_before


def test_resolve_unknown_refuses_a_non_unknown_item():
    db = FakeFirestore()
    approved_item(db, queue_id="q1")

    assert queue.resolve_unknown(db, "q1", sent=True, now=START) is False


# --- approve() ---------------------------------------------------------------


def test_approve_transitions_pending_to_approved():
    db = FakeFirestore()
    pending_item(db, queue_id="q1")

    approved = queue.approve(db, "q1", START)

    assert approved is True
    item = queue.get(db, "q1")
    assert item["status"] == queue.APPROVED
    assert item["approved_by"] == "human"
    assert item["approved_at"] == START


def test_approve_refuses_a_non_pending_item():
    db = FakeFirestore()
    approved_item(db, queue_id="q1")

    assert queue.approve(db, "q1", START) is False


# --- cancel() -----------------------------------------------------------------


def test_cancel_transitions_pending_to_cancelled():
    db = FakeFirestore()
    pending_item(db, queue_id="q1")

    cancelled = queue.cancel(db, "q1", "changed my mind", START)

    assert cancelled is True
    item = queue.get(db, "q1")
    assert item["status"] == queue.CANCELLED
    assert item["cancel_reason"] == "changed my mind"
    assert item["settled_at"] == START


def test_cancel_transitions_approved_to_cancelled():
    db = FakeFirestore()
    approved_item(db, queue_id="q1")

    assert queue.cancel(db, "q1", "changed my mind", START) is True
    assert queue.get(db, "q1")["status"] == queue.CANCELLED


def test_cancel_refuses_a_terminal_item():
    db = FakeFirestore()
    claimed_item(db, queue_id="q1")
    queue.settle(db, "q1", queue.SENT, now=START)

    assert queue.cancel(db, "q1", "too late", START) is False


def test_cancel_refuses_a_created_by_mismatch():
    db = FakeFirestore()
    pending_item(db, queue_id="q1")  # created_by defaults to "agent"

    cancelled = queue.cancel(db, "q1", "not yours", START, created_by="human")

    assert cancelled is False
    assert queue.get(db, "q1")["status"] == queue.PENDING


def test_cancel_succeeds_when_created_by_matches():
    db = FakeFirestore()
    pending_item(db, queue_id="q1")

    assert queue.cancel(db, "q1", "yours", START, created_by="agent") is True


# --- cancel_for_contact() --------------------------------------------------


def test_cancel_for_contact_cancels_open_items_and_returns_the_count():
    db = FakeFirestore()
    approved_item(db, queue_id="a1", contact_doc_id="contact-A")
    pending_item(db, queue_id="a2", contact_doc_id="contact-A")
    claimed_item(db, queue_id="a3", contact_doc_id="contact-A")  # sending: NOT pending/approved
    approved_item(db, queue_id="b1", contact_doc_id="contact-B")

    count = queue.cancel_for_contact(db, "contact-A", "contact opted out", START)

    assert count == 2
    assert queue.get(db, "a1")["status"] == queue.CANCELLED
    assert queue.get(db, "a2")["status"] == queue.CANCELLED
    assert queue.get(db, "a3")["status"] == queue.SENDING  # untouched: not pending/approved
    assert queue.get(db, "b1")["status"] == queue.APPROVED  # a different contact, untouched


# --- items_for_contact() ---------------------------------------------------


def test_items_for_contact_returns_only_that_contacts_items_newest_first():
    db = FakeFirestore()
    approved_item(db, queue_id="a1", contact_doc_id="contact-A", now=START)
    approved_item(db, queue_id="a2", contact_doc_id="contact-A", now=START + timedelta(minutes=1))
    approved_item(db, queue_id="b1", contact_doc_id="contact-B", now=START)

    items = queue.items_for_contact(db, "contact-A")

    assert [item["id"] for item in items] == ["a2", "a1"]


# --- open_contact_ids() -----------------------------------------------------


def test_open_contact_ids_includes_every_open_status():
    db = FakeFirestore()
    pending_item(db, queue_id="p", contact_doc_id="contact-pending")
    approved_item(db, queue_id="a", contact_doc_id="contact-approved")
    claimed_item(db, queue_id="s", contact_doc_id="contact-sending")
    unknown_item(db, queue_id="u", contact_doc_id="contact-unknown")
    claimed_item(db, queue_id="t", contact_doc_id="contact-terminal")
    queue.settle(db, "t", queue.SENT, now=START)  # terminal: must NOT appear

    ids = queue.open_contact_ids(db)

    assert ids == {"contact-pending", "contact-approved", "contact-sending", "contact-unknown"}


# --- list_items() -------------------------------------------------------------


def test_list_items_orders_newest_created_at_first():
    db = FakeFirestore()
    approved_item(db, queue_id="q1", now=START)
    approved_item(db, queue_id="q2", now=START + timedelta(minutes=1))
    approved_item(db, queue_id="q3", now=START + timedelta(minutes=2))

    items = queue.list_items(db)

    assert [item["id"] for item in items] == ["q3", "q2", "q1"]


def test_list_items_filters_by_status():
    db = FakeFirestore()
    approved_item(db, queue_id="q1")
    pending_item(db, queue_id="q2")

    items = queue.list_items(db, status=queue.PENDING)

    assert [item["id"] for item in items] == ["q2"]


def test_list_items_limit_clamps_to_a_minimum_of_1():
    db = FakeFirestore()
    approved_item(db, queue_id="q1", now=START)
    approved_item(db, queue_id="q2", now=START + timedelta(minutes=1))

    items = queue.list_items(db, limit=0)

    assert len(items) == 1


def test_list_items_limit_clamps_to_a_maximum_of_100():
    db = FakeFirestore()
    for i in range(105):
        db.collection(queue.QUEUE_COLLECTION).document(f"raw-{i}").set(
            {
                "status": queue.APPROVED,
                "created_at": START + timedelta(seconds=i),
                "contact_doc_id": "contact-1",
                "kind": "follow_up",
                "text": "t",
            }
        )

    items = queue.list_items(db, limit=1000)

    assert len(items) == 100


# --- stale_sending() ---------------------------------------------------------


def test_stale_sending_ignores_non_sending_items():
    db = FakeFirestore()
    approved_item(db, queue_id="q1")

    assert queue.stale_sending(db, START + timedelta(days=1)) == []


# --- counts() -----------------------------------------------------------------


def test_counts_reports_zero_for_every_open_status_with_no_items():
    db = FakeFirestore()

    assert queue.counts(db) == {
        queue.PENDING: 0,
        queue.APPROVED: 0,
        queue.SENDING: 0,
        queue.UNKNOWN: 0,
    }


def test_counts_reports_the_right_number_per_status_and_excludes_terminal_items():
    db = FakeFirestore()
    pending_item(db, queue_id="p1")
    approved_item(db, queue_id="a1")
    approved_item(db, queue_id="a2")
    claimed_item(db, queue_id="s1")  # sending
    approved_item(db, queue_id="c1")
    queue.cancel(db, "c1", "test noise", START)  # cancelled: terminal, must not be counted

    counts = queue.counts(db)

    assert counts == {
        queue.PENDING: 1,
        queue.APPROVED: 2,
        queue.SENDING: 1,
        queue.UNKNOWN: 0,
    }


# --- start_manual() ---------------------------------------------------------


def test_start_manual_creates_a_sending_item_that_settles_like_a_claimed_one():
    db = FakeFirestore()
    item, created = queue.start_manual(
        db, "manual:contact-1:t1", {"contact_doc_id": "contact-1", "text": "hi", "chat_id": "chat-1", "tags": ["manual"]},
        "webapp", START,
    )
    assert created and item["status"] == queue.SENDING and item["kind"] == queue.MANUAL
    assert queue.next_due(db, START + timedelta(hours=1)) is None  # never approved, so no step picks it up

    queue.settle(db, "manual:contact-1:t1", queue.SENT, now=START, message_id="msg-1")
    stored = queue.get(db, "manual:contact-1:t1")
    assert (stored["status"], stored["message_id"], stored["tags"], stored["created_by"]) == (queue.SENT, "msg-1", ["manual"], "webapp")
    [row] = [doc.to_dict() for doc in db.collection(ledger.LEDGER_COLLECTION).stream()]
    assert (row["kind"], row["result"], row["queue_id"]) == ("message", queue.SENT, "manual:contact-1:t1")


def test_start_manual_twice_writes_nothing_the_second_time():
    db = FakeFirestore()
    queue.start_manual(db, "m1", {"contact_doc_id": "contact-1", "text": "first"}, "webapp", START)
    item, created = queue.start_manual(db, "m1", {"contact_doc_id": "contact-1", "text": "second"}, "webapp", START)
    assert not created and item["text"] == "first"
    with pytest.raises(ValueError):
        queue.enqueue(db, "m2", {"contact_doc_id": "contact-1", "kind": queue.MANUAL, "text": "x"}, require_approval=False, now=START)
