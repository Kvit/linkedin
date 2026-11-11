"""Tests for `linkedinmcp.fetch_queue`: one `fetch_queue/{slug}` document per
LinkedIn slug the daily job found among recent connections whose profile is
not stored yet. Task 3b's tick takes one `queued` document at a time
(`next_queued`), fetches and classifies it, and records the outcome with
`mark`, `note_attempt` or `requeue_incomplete` (ruling P3-3) -- the fetching
and classifying are `test_fetching.py`'s; these tests only exercise the
storage.

`enqueue` is create-only, like `queue.enqueue` and `decisions.raise_alert`:
a second `enqueue` of the same slug (the daily job re-running) changes
nothing. `mark`, `note_attempt` and `requeue_incomplete` are the REAL
`google.cloud.firestore.transactional` decorator, exactly as `state.py` and
`queue.py` use it, re-reading the current status and refusing an illegal
move by returning `False`/`None` rather than raising.
"""

from datetime import UTC, datetime, timedelta

import pytest

from linkedinmcp import fetch_queue
from tests.linkedinmcp.fake_firestore import FakeFirestore

START = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def make_queued(db, slug="alice", *, provider_id="ACoAAAlice", name="Alice Adams",
                 connected_at=None, now=START) -> dict:
    """Enqueue one `queued` entry through the real `enqueue` and return it
    via `fetch_queue.get`, so every field is exactly what the real function
    stored."""
    connected_at = connected_at if connected_at is not None else now
    fetch_queue.enqueue(db, slug, provider_id=provider_id, name=name, connected_at=connected_at, now=now)
    return fetch_queue.get(db, slug)


# --- enqueue() --------------------------------------------------------------


def test_enqueue_creates_the_documented_fields_and_returns_true():
    db = FakeFirestore()
    connected_at = START - timedelta(days=2)

    created = fetch_queue.enqueue(
        db, "alice", provider_id="ACoAAAlice", name="Alice Adams", connected_at=connected_at, now=START,
    )

    assert created is True
    item = fetch_queue.get(db, "alice")
    assert item == {
        "id": "alice",
        "slug": "alice",
        "provider_id": "ACoAAAlice",
        "name": "Alice Adams",
        "connected_at": connected_at,
        "status": fetch_queue.QUEUED,
        "queued_at": START,
        "attempts": 0,
        "incomplete_count": 0,
        "last_error": None,
        "updated_at": START,
        "classified": None,
    }


def test_enqueue_of_an_existing_slug_returns_false_and_changes_nothing():
    db = FakeFirestore()
    fetch_queue.enqueue(db, "alice", provider_id="ACoAAAlice", name="Alice Adams", connected_at=START, now=START)

    created = fetch_queue.enqueue(
        db, "alice", provider_id="ACoAADifferent", name="Someone Else",
        connected_at=START + timedelta(days=1), now=START + timedelta(hours=1),
    )

    assert created is False
    assert fetch_queue.get(db, "alice")["provider_id"] == "ACoAAAlice"
    assert fetch_queue.get(db, "alice")["name"] == "Alice Adams"


@pytest.mark.parametrize(
    "slug",
    ["", "a/b", "__reserved__"],
    ids=["empty", "slash", "reserved"],
)
def test_enqueue_rejects_an_unusable_slug_and_writes_nothing(slug):
    db = FakeFirestore()

    with pytest.raises(ValueError):
        fetch_queue.enqueue(db, slug, provider_id="ACoAAX", name="X", connected_at=START, now=START)

    assert list(db.collection(fetch_queue.FETCH_COLLECTION).stream()) == []


def test_enqueue_accepts_a_normal_slug_with_a_hyphen():
    db = FakeFirestore()

    created = fetch_queue.enqueue(
        db, "jane-doe", provider_id="ACoAAJane", name="Jane Doe", connected_at=START, now=START,
    )

    assert created is True


# --- usable_slug() -------------------------------------------------------------


@pytest.mark.parametrize(
    "slug, expected",
    [("alice", True), ("jane-doe", True), ("", False), ("a/b", False), ("__reserved__", False)],
    ids=["normal", "hyphen", "empty", "slash", "reserved"],
)
def test_usable_slug_matches_what_enqueue_accepts_or_rejects(slug, expected):
    """The public predicate `enqueue` itself consults before raising:
    usable for an ordinary slug or one with a hyphen, not usable for an
    empty slug, one containing `/`, or one matching `^__.*__$` -- the same
    three cases `enqueue`'s own `ValueError` tests exercise indirectly.
    """
    assert fetch_queue.usable_slug(slug) is expected


@pytest.mark.parametrize(
    "slug, expected",
    [
        (".", False),
        ("..", False),
        ("x" * 1500, True),
        ("é" * 750, True),
        ("x" * 1501, False),
        ("é" * 751, False),
        ("\ud800", False),
    ],
    ids=["dot", "dotdot", "1500-ascii", "1500-bytes-utf8", "1501-ascii", "1502-bytes-utf8", "lone-surrogate"],
)
def test_usable_slug_follows_the_same_document_id_rules_as_jobs_and_mcp_server(slug, expected):
    """Ruling P5-4: `.` and `..` are no document id, and one is at most
    1,500 bytes of UTF-8 -- so 750 two-byte characters fit and 751 do not;
    a string UTF-8 cannot encode (a lone surrogate) is unusable, answered
    rather than raised. `enqueue` refuses what this refuses, writing
    nothing."""
    assert fetch_queue.usable_slug(slug) is expected
    if not expected:
        db = FakeFirestore()
        with pytest.raises(ValueError):
            fetch_queue.enqueue(db, slug, provider_id="ACoAAX", name="X", connected_at=START, now=START)
        assert list(db.collection(fetch_queue.FETCH_COLLECTION).stream()) == []


# --- get() -------------------------------------------------------------------


def test_get_returns_none_for_a_missing_slug():
    db = FakeFirestore()

    assert fetch_queue.get(db, "nope") is None


def test_get_returns_the_document_with_its_id():
    db = FakeFirestore()
    make_queued(db, "alice")

    item = fetch_queue.get(db, "alice")

    assert item["id"] == "alice"
    assert item["status"] == fetch_queue.QUEUED


# --- next_queued() -------------------------------------------------------------


def test_next_queued_returns_none_when_nothing_is_queued():
    db = FakeFirestore()

    assert fetch_queue.next_queued(db, START) is None


def test_next_queued_takes_the_newest_connection_first_whenever_it_was_queued():
    """A connection made this week is fetched before a backlog queued
    earlier: `get_contacts` exists to load the new contacts."""
    db = FakeFirestore()
    make_queued(db, "backlog", connected_at=START - timedelta(days=365), now=START)
    make_queued(db, "new-this-week", connected_at=START + timedelta(days=2), now=START + timedelta(days=3))

    item = fetch_queue.next_queued(db, START + timedelta(days=3))

    assert item["id"] == "new-this-week"


def test_next_queued_breaks_a_connection_date_tie_by_id():
    db = FakeFirestore()
    make_queued(db, "zzz-later-id", now=START)
    make_queued(db, "aaa-earlier-id", now=START)

    item = fetch_queue.next_queued(db, START)

    assert item["id"] == "aaa-earlier-id"


def test_next_queued_takes_a_connection_with_no_date_last():
    """One run enqueues all its connections at the same `queued_at`: the
    newest goes first, one with no connection date after every dated one."""
    db = FakeFirestore()
    make_queued(db, "aaa-connected-long-ago", connected_at=START - timedelta(days=365), now=START)
    make_queued(db, "zzz-connected-yesterday", connected_at=START - timedelta(days=1), now=START)
    fetch_queue.enqueue(db, "bbb-no-date", provider_id="ACoAANoDate", name="No Date", connected_at=None, now=START)

    order = []
    while (item := fetch_queue.next_queued(db, START)) is not None:
        order.append(item["id"])
        fetch_queue.mark(db, item["id"], fetch_queue.STORED, START)

    assert order == ["zzz-connected-yesterday", "aaa-connected-long-ago", "bbb-no-date"]


def test_next_queued_excludes_non_queued_documents():
    db = FakeFirestore()
    make_queued(db, "alice", now=START)
    fetch_queue.mark(db, "alice", fetch_queue.STORED, START)
    make_queued(db, "bob", now=START + timedelta(minutes=1))

    item = fetch_queue.next_queued(db, START + timedelta(hours=1))

    assert item["id"] == "bob"


# --- mark() --------------------------------------------------------------------


def test_mark_stored_stores_classified_and_updated_at():
    db = FakeFirestore()
    make_queued(db, "alice", now=START)

    ok = fetch_queue.mark(db, "alice", fetch_queue.STORED, START + timedelta(minutes=5), classified="prospect")

    assert ok is True
    item = fetch_queue.get(db, "alice")
    assert item["status"] == fetch_queue.STORED
    assert item["classified"] == "prospect"
    assert item["last_error"] is None
    assert item["updated_at"] == START + timedelta(minutes=5)


def test_mark_short_stores_the_error_and_leaves_classified_untouched():
    db = FakeFirestore()
    make_queued(db, "alice", now=START)

    ok = fetch_queue.mark(db, "alice", fetch_queue.SHORT, START + timedelta(minutes=5), error="withheld sections")

    assert ok is True
    item = fetch_queue.get(db, "alice")
    assert item["status"] == fetch_queue.SHORT
    assert item["last_error"] == "withheld sections"
    assert item["classified"] is None  # never touched for a non-stored mark


def test_mark_failed_stores_the_error():
    db = FakeFirestore()
    make_queued(db, "alice", now=START)

    ok = fetch_queue.mark(db, "alice", fetch_queue.FAILED, START, error="ProfileNotFound")

    assert ok is True
    assert fetch_queue.get(db, "alice")["status"] == fetch_queue.FAILED
    assert fetch_queue.get(db, "alice")["last_error"] == "ProfileNotFound"


def test_mark_rejects_a_status_outside_stored_short_failed():
    db = FakeFirestore()
    make_queued(db, "alice", now=START)

    with pytest.raises(ValueError):
        fetch_queue.mark(db, "alice", fetch_queue.QUEUED, START)

    assert fetch_queue.get(db, "alice")["status"] == fetch_queue.QUEUED


def test_mark_of_a_missing_document_returns_false():
    db = FakeFirestore()

    assert fetch_queue.mark(db, "nope", fetch_queue.STORED, START) is False


def test_mark_of_a_non_queued_document_returns_false_and_changes_nothing():
    db = FakeFirestore()
    make_queued(db, "alice", now=START)
    fetch_queue.mark(db, "alice", fetch_queue.STORED, START, classified="prospect")

    ok = fetch_queue.mark(db, "alice", fetch_queue.FAILED, START + timedelta(minutes=1), error="too late")

    assert ok is False
    item = fetch_queue.get(db, "alice")
    assert item["status"] == fetch_queue.STORED
    assert item["classified"] == "prospect"


# --- note_attempt() --------------------------------------------------------------


def test_note_attempt_increments_attempts_and_stays_queued_below_max_attempts():
    db = FakeFirestore()
    make_queued(db, "alice", now=START)

    updated = fetch_queue.note_attempt(db, "alice", START + timedelta(minutes=1), "TimeoutError")

    assert updated["attempts"] == 1
    assert updated["status"] == fetch_queue.QUEUED
    assert updated["last_error"] == "TimeoutError"
    assert updated["updated_at"] == START + timedelta(minutes=1)
    assert fetch_queue.get(db, "alice")["attempts"] == 1


def test_note_attempt_reaching_max_attempts_marks_failed():
    """`MAX_ATTEMPTS` is 3: the third failed attempt is the one that flips
    the status, in the SAME transaction as the increment."""
    db = FakeFirestore()
    make_queued(db, "alice", now=START)

    fetch_queue.note_attempt(db, "alice", START, "e1")
    fetch_queue.note_attempt(db, "alice", START, "e2")
    third = fetch_queue.note_attempt(db, "alice", START, "e3")

    assert third["attempts"] == fetch_queue.MAX_ATTEMPTS == 3
    assert third["status"] == fetch_queue.FAILED
    assert fetch_queue.get(db, "alice")["status"] == fetch_queue.FAILED


def test_note_attempt_on_a_missing_document_returns_none():
    db = FakeFirestore()

    assert fetch_queue.note_attempt(db, "nope", START, "e1") is None


def test_note_attempt_on_a_non_queued_document_returns_none():
    db = FakeFirestore()
    make_queued(db, "alice", now=START)
    fetch_queue.mark(db, "alice", fetch_queue.FAILED, START, error="gone")

    assert fetch_queue.note_attempt(db, "alice", START + timedelta(minutes=1), "e-again") is None
    assert fetch_queue.get(db, "alice")["attempts"] == 0


# --- requeue_incomplete() ----------------------------------------------------------


def test_requeue_incomplete_moves_the_slug_behind_the_others_and_counts_one_incomplete():
    """Ruling P3-3: `queued_at` becomes `now` and `incomplete_count` 1, so
    `next_queued` returns the other slug first. `last_error` is the
    class name passed in, `updated_at` is `now`; the status stays `queued`
    and `attempts` is untouched. The returned document is the stored one.
    """
    db = FakeFirestore()
    make_queued(db, "alice", now=START)
    make_queued(db, "bob", now=START + timedelta(minutes=5))
    later = START + timedelta(hours=1)

    updated = fetch_queue.requeue_incomplete(db, "alice", later, "ProfileIncomplete")

    item = fetch_queue.get(db, "alice")
    assert (item["status"], item["queued_at"], item["incomplete_count"], item["attempts"]) == (
        fetch_queue.QUEUED, later, 1, 0,
    )
    assert (item["last_error"], item["updated_at"]) == ("ProfileIncomplete", later)
    assert updated == item
    assert fetch_queue.next_queued(db, later)["id"] == "bob"


def test_a_withheld_profile_waits_behind_every_fresh_one():
    """Ruling P3-3 under newest-first: LinkedIn withheld the newest profile,
    so it goes behind the fresh ones -- an older connection, and one queued
    after the requeue -- instead of straight back to the front."""
    db = FakeFirestore()
    make_queued(db, "withheld-newest", connected_at=START - timedelta(days=1), now=START)
    make_queued(db, "fresh-older", connected_at=START - timedelta(days=365), now=START)
    fetch_queue.requeue_incomplete(db, "withheld-newest", START + timedelta(hours=1), "ProfileIncomplete")
    make_queued(db, "fresh-queued-after", connected_at=START - timedelta(days=400), now=START + timedelta(hours=2))

    order = []
    while (item := fetch_queue.next_queued(db, START + timedelta(hours=3))) is not None:
        order.append(item["id"])
        fetch_queue.mark(db, item["id"], fetch_queue.STORED, START)

    assert order == ["fresh-older", "fresh-queued-after", "withheld-newest"]


def test_the_third_incomplete_marks_the_slug_failed_as_withheld_three_times():
    """`MAX_INCOMPLETE` is 3: the third call sets `status = failed` and
    `last_error = "LinkedIn withheld sections 3 times"` in the same
    transaction as the count, and nothing is queued afterwards."""
    db = FakeFirestore()
    make_queued(db, "alice", now=START)

    fetch_queue.requeue_incomplete(db, "alice", START + timedelta(hours=1), "ProfileIncomplete")
    second = fetch_queue.requeue_incomplete(db, "alice", START + timedelta(hours=2), "ThrottleLockout")
    third = fetch_queue.requeue_incomplete(db, "alice", START + timedelta(hours=3), "ProfileIncomplete")

    assert fetch_queue.MAX_INCOMPLETE == 3
    assert (second["status"], second["incomplete_count"], second["last_error"]) == (
        fetch_queue.QUEUED, 2, "ThrottleLockout",
    )
    assert (third["status"], third["incomplete_count"], third["last_error"]) == (
        fetch_queue.FAILED, 3, "LinkedIn withheld sections 3 times",
    )
    assert fetch_queue.get(db, "alice") == third
    assert fetch_queue.next_queued(db, START + timedelta(hours=4)) is None


def test_requeue_incomplete_counts_from_zero_on_an_item_stored_without_the_field():
    """An item task 3a's `enqueue` stored before `incomplete_count` existed
    has no such field: it counts as 0, so the first incomplete makes it 1."""
    db = FakeFirestore()
    make_queued(db, "alice", now=START)
    body = db.collection(fetch_queue.FETCH_COLLECTION).document("alice").get().to_dict()
    del body["incomplete_count"]
    db.collection(fetch_queue.FETCH_COLLECTION).document("alice").set(body)

    updated = fetch_queue.requeue_incomplete(db, "alice", START + timedelta(hours=1), "ProfileIncomplete")

    assert (updated["status"], updated["incomplete_count"]) == (fetch_queue.QUEUED, 1)


def test_requeue_incomplete_on_a_missing_document_returns_none():
    db = FakeFirestore()

    assert fetch_queue.requeue_incomplete(db, "nope", START, "ProfileIncomplete") is None
    assert fetch_queue.get(db, "nope") is None


def test_requeue_incomplete_on_a_non_queued_document_returns_none_and_changes_nothing():
    db = FakeFirestore()
    make_queued(db, "alice", now=START)
    fetch_queue.mark(db, "alice", fetch_queue.STORED, START, classified=True)
    before = fetch_queue.get(db, "alice")

    assert fetch_queue.requeue_incomplete(db, "alice", START + timedelta(hours=1), "ProfileIncomplete") is None
    assert fetch_queue.get(db, "alice") == before


def test_requeue_incomplete_retried_after_a_contended_commit_counts_one_incomplete():
    """`db.contend_once()` aborts the first commit and the real
    `@firestore.transactional` decorator retries: the count is 1, so the
    losing attempt's write never landed, and the arm was consumed by a real
    commit (the `test_state.py` pattern) -- the call ran in a transaction."""
    db = FakeFirestore()
    make_queued(db, "alice", now=START)
    db.contend_once()

    updated = fetch_queue.requeue_incomplete(db, "alice", START + timedelta(hours=1), "ProfileIncomplete")

    assert updated["incomplete_count"] == 1
    assert fetch_queue.get(db, "alice")["incomplete_count"] == 1
    assert db._contend_once_armed is False


# --- counts() --------------------------------------------------------------------


def test_counts_reports_zero_for_every_status_with_no_documents():
    db = FakeFirestore()

    assert fetch_queue.counts(db) == {
        fetch_queue.QUEUED: 0,
        fetch_queue.STORED: 0,
        fetch_queue.SHORT: 0,
        fetch_queue.FAILED: 0,
    }


def test_counts_reports_the_right_number_per_status():
    db = FakeFirestore()
    make_queued(db, "q1")
    make_queued(db, "q2")
    make_queued(db, "s1")
    fetch_queue.mark(db, "s1", fetch_queue.STORED, START, classified="prospect")
    make_queued(db, "h1")
    fetch_queue.mark(db, "h1", fetch_queue.SHORT, START, error="withheld")
    make_queued(db, "f1")
    fetch_queue.mark(db, "f1", fetch_queue.FAILED, START, error="gone")

    assert fetch_queue.counts(db) == {
        fetch_queue.QUEUED: 2,
        fetch_queue.STORED: 1,
        fetch_queue.SHORT: 1,
        fetch_queue.FAILED: 1,
    }
