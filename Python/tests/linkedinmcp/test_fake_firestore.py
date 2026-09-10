"""Tests for the in-memory Firestore double the outreach service's queue,
lease, ledger and decision-inbox tests all run against.

Every test here pins one real `google.cloud.firestore` behaviour -- verified
against the installed library while `fake_firestore.py` was written -- and
says what would go wrong if the fake got it wrong instead. The four riskiest
behaviours get the most attention: `SERVER_TIMESTAMP` resolving to a real
`datetime` on write, a transaction rolling back cleanly, a contended
transaction retrying through the REAL `@firestore.transactional` decorator,
`get_all`'s shuffled order, and a document missing a field being excluded
from a query rather than treated as null or zero.
"""

from datetime import UTC, datetime

import pytest
from google.cloud import firestore
from google.cloud.exceptions import NotFound
from google.cloud.firestore_v1.base_query import FieldFilter

from tests.linkedinmcp.fake_firestore import FakeFirestore

# --- db.collection() / .document() handles ----------------------------------


def test_collection_is_a_stateless_handle_onto_the_shared_store():
    """Two `db.collection("x")` calls must not be two stores: the service
    builds a fresh reference on every call and a test independently holds its
    own. If a fake gave each call its own state, a write made through the
    service's handle would be invisible to the test's assertions.
    """
    db = FakeFirestore()
    db.collection("c").document("d").set({"n": 1})

    assert db.collection("c").document("d").get().to_dict() == {"n": 1}


def test_document_with_no_id_invents_a_unique_one():
    db = FakeFirestore()
    col = db.collection("c")

    first = col.document()
    second = col.document()

    assert first.id != second.id
    assert isinstance(first.id, str) and first.id


# --- ref.get() / .set() / .update() / .delete() -----------------------------


def test_get_on_a_missing_document_is_not_an_error():
    db = FakeFirestore()

    snapshot = db.collection("c").document("missing").get()

    assert snapshot.exists is False
    assert snapshot.id == "missing"
    assert snapshot.to_dict() is None  # not {} -- mirrors the real DocumentSnapshot


def test_get_on_an_existing_document_returns_its_data():
    db = FakeFirestore()
    ref = db.collection("c").document("d")
    ref.set({"n": 1})

    snapshot = ref.get()

    assert snapshot.exists is True
    assert snapshot.id == "d"
    assert snapshot.to_dict() == {"n": 1}
    assert snapshot.reference.id == ref.id


def test_set_without_merge_replaces_the_whole_document():
    db = FakeFirestore()
    ref = db.collection("c").document("d")
    ref.set({"a": 1, "b": 2})

    ref.set({"a": 9})

    assert ref.get().to_dict() == {"a": 9}  # "b" is gone, not kept


def test_set_with_merge_keeps_fields_the_new_body_omits():
    db = FakeFirestore()
    ref = db.collection("c").document("d")
    ref.set({"a": 1, "b": 2})

    ref.set({"a": 9}, merge=True)

    assert ref.get().to_dict() == {"a": 9, "b": 2}


def test_update_merges_into_an_existing_document():
    db = FakeFirestore()
    ref = db.collection("c").document("d")
    ref.set({"a": 1, "b": 2})

    ref.update({"a": 9})

    assert ref.get().to_dict() == {"a": 9, "b": 2}


def test_update_on_a_missing_document_raises_not_found():
    """Pins the failure mode a queue's claim-then-send code depends on: if a
    document was deleted out from under it, `update()` must fail loudly
    rather than silently creating a half-written document.
    """
    db = FakeFirestore()
    ref = db.collection("c").document("missing")

    with pytest.raises(NotFound):
        ref.update({"a": 1})


def test_delete_removes_the_document():
    db = FakeFirestore()
    ref = db.collection("c").document("d")
    ref.set({"a": 1})

    ref.delete()

    assert ref.get().exists is False


def test_deleting_an_absent_document_is_not_an_error():
    db = FakeFirestore()
    ref = db.collection("c").document("missing")

    ref.delete()  # must not raise


# --- col.where() -------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "value", "expected_ids"),
    [
        ("==", 2, ["b"]),
        ("!=", 2, ["a", "c"]),
        (">", 2, ["c"]),
        (">=", 2, ["b", "c"]),
        ("<", 2, ["a"]),
        ("<=", 2, ["a", "b"]),
        ("in", [1, 3], ["a", "c"]),
    ],
)
def test_where_supports_every_documented_operator(op, value, expected_ids):
    """The seven operators the brief calls for. `functions.
    count_created_since` already depends on `>=` matching inclusively; the
    rest round out the set a queue or ledger query needs.
    """
    db = FakeFirestore()
    col = db.collection("c")
    col.document("a").set({"n": 1})
    col.document("b").set({"n": 2})
    col.document("c").set({"n": 3})

    results = col.where(filter=FieldFilter("n", op, value)).stream()

    assert sorted(s.id for s in results) == sorted(expected_ids)


def test_where_can_be_chained_for_an_implicit_and():
    db = FakeFirestore()
    col = db.collection("c")
    col.document("a").set({"x": 1, "y": 1})
    col.document("b").set({"x": 1, "y": 2})

    results = (
        col.where(filter=FieldFilter("x", "==", 1))
        .where(filter=FieldFilter("y", "==", 1))
        .stream()
    )

    assert [s.id for s in results] == ["a"]


def test_where_rejects_the_deprecated_positional_form():
    """The service always calls `where(filter=FieldFilter(...))`; supporting
    the older positional form too would be one more code path this fake
    would have to keep faithful for no caller that needs it.
    """
    db = FakeFirestore()
    col = db.collection("c")

    with pytest.raises(NotImplementedError):
        col.where("n", "==", 2)


# --- col.order_by() ------------------------------------------------------------


def test_order_by_defaults_to_ascending():
    db = FakeFirestore()
    col = db.collection("c")
    col.document("a").set({"n": 3})
    col.document("b").set({"n": 1})
    col.document("c").set({"n": 2})

    results = list(col.order_by("n").stream())

    assert [s.id for s in results] == ["b", "c", "a"]


def test_order_by_descending_reverses_it():
    db = FakeFirestore()
    col = db.collection("c")
    col.document("a").set({"n": 3})
    col.document("b").set({"n": 1})
    col.document("c").set({"n": 2})

    results = list(col.order_by("n", direction="DESCENDING").stream())

    assert [s.id for s in results] == ["a", "c", "b"]


def test_order_by_rejects_an_invalid_direction():
    """Guards against a typo like `direction="DESC"` silently sorting
    ascending instead of raising -- passing for the wrong reason is exactly
    the failure mode this whole file exists to prevent.
    """
    db = FakeFirestore()
    col = db.collection("c")

    with pytest.raises(ValueError):
        col.order_by("n", direction="DESC")


# --- col.limit() ---------------------------------------------------------------


def test_limit_caps_the_number_of_results():
    db = FakeFirestore()
    col = db.collection("c")
    for i in range(5):
        col.document(f"d{i}").set({"n": i})

    results = list(col.order_by("n").limit(2).stream())

    assert [s.id for s in results] == ["d0", "d1"]


# --- col.select() ----------------------------------------------------------


def test_select_projects_only_the_named_fields():
    db = FakeFirestore()
    db.collection("c").document("d").set({"a": 1, "b": 2, "c": 3})

    [snapshot] = list(db.collection("c").select(["a", "c"]).stream())

    assert snapshot.to_dict() == {"a": 1, "c": 3}
    assert snapshot.id == "d"  # projection drops fields, never identity


def test_select_with_no_fields_yields_empty_dicts_but_real_ids():
    db = FakeFirestore()
    db.collection("c").document("d").set({"a": 1})

    [snapshot] = list(db.collection("c").select([]).stream())

    assert snapshot.to_dict() == {}
    assert snapshot.id == "d"


# --- col.stream() ------------------------------------------------------------


def test_stream_iterates_every_document_when_unfiltered():
    db = FakeFirestore()
    db.collection("c").document("a").set({})
    db.collection("c").document("b").set({})

    assert sorted(s.id for s in db.collection("c").stream()) == ["a", "b"]


# --- col.count() ---------------------------------------------------------------


def test_count_returns_the_nested_list_shape_with_alias():
    """`functions.count_created_since` unwraps this as `result[0][0].value`;
    `_FakeAggregation` in test_functions.py mirrors the same shape without
    `alias`. This fake adds `alias` because `count(alias=...)` names it.
    """
    db = FakeFirestore()
    col = db.collection("c")
    col.document("a").set({})
    col.document("b").set({})

    result = col.count(alias="n").get()

    assert result[0][0].value == 2
    assert result[0][0].alias == "n"


def test_count_reflects_the_filters_applied_before_it():
    db = FakeFirestore()
    col = db.collection("c")
    col.document("a").set({"active": True})
    col.document("b").set({"active": False})

    result = col.where(filter=FieldFilter("active", "==", True)).count(alias="n").get()

    assert result[0][0].value == 1


# --- db.get_all() and behaviour 3: its arbitrary, seeded order ---------------


def test_get_all_returns_every_requested_snapshot():
    db = FakeFirestore()
    col = db.collection("c")
    refs = [col.document(doc_id) for doc_id in ("a", "b", "c")]
    for ref in refs:
        ref.set({"id": ref.id})

    snapshots = db.get_all(refs)

    assert sorted(s.id for s in snapshots) == ["a", "b", "c"]


def test_get_all_order_matches_a_seeded_shuffle_not_request_order():
    """The real API documents `get_all` order as arbitrary; code that quietly
    assumes request order works by accident locally and breaks in
    production. `shuffle_seed` makes the arbitrary order reproducible so a
    failure here can be replayed deterministically instead of flaking.

    This must be the ONLY `get_all` (direct or via a transaction's `.get()`)
    this `db` instance ever performs: the seed advances in call order, so any
    other shuffle would shift the sequence this hard-coded expectation was
    computed against.
    """
    db = FakeFirestore(shuffle_seed=1)
    col = db.collection("c")
    doc_ids = ("a", "b", "c", "d", "e")
    for doc_id in doc_ids:
        col.document(doc_id).set({})
    refs = [col.document(doc_id) for doc_id in doc_ids]

    snapshots = db.get_all(refs)

    # random.Random(1).shuffle(['a', 'b', 'c', 'd', 'e']) == ['c', 'd', 'e', 'a', 'b']
    assert [s.id for s in snapshots] == ["c", "d", "e", "a", "b"]


# --- db.batch() ----------------------------------------------------------------


def test_batch_writes_are_invisible_until_commit():
    db = FakeFirestore()
    ref = db.collection("c").document("d")

    batch = db.batch()
    batch.set(ref, {"a": 1})

    assert ref.get().exists is False  # nothing landed yet

    batch.commit()

    assert ref.get().to_dict() == {"a": 1}


def test_batch_applies_set_update_and_delete_together_in_order():
    db = FakeFirestore()
    col = db.collection("c")
    survivor = col.document("survivor")
    survivor.set({"a": 1})
    doomed = col.document("doomed")
    doomed.set({"a": 1})

    batch = db.batch()
    batch.set(col.document("new"), {"a": 1})  # brand new document
    batch.update(col.document("new"), {"b": 2})  # updated in the SAME batch
    batch.update(survivor, {"a": 9})
    batch.delete(doomed)
    batch.commit()

    assert col.document("new").get().to_dict() == {"a": 1, "b": 2}
    assert survivor.get().to_dict() == {"a": 9}
    assert doomed.get().exists is False


def test_batch_commit_is_all_or_nothing():
    """If any buffered `update` fails its precondition, nothing in the batch
    should land -- a batch is one atomic unit, not a sequence of independent
    writes that stops partway through.
    """
    db = FakeFirestore()
    ref = db.collection("c").document("d")

    batch = db.batch()
    batch.set(ref, {"a": 1})
    batch.update(db.collection("c").document("missing"), {"a": 1})

    with pytest.raises(NotFound):
        batch.commit()

    assert ref.get().exists is False  # the set() never landed either


# --- db.transaction() + @firestore.transactional (behaviour 2) --------------
#
# `_Transactional` (google.cloud.firestore_v1.transaction) is the REAL
# decorator's driver, read from the installed library while writing this
# fake. In order, it calls `transaction._clean_up()`, `transaction._begin
# (retry_id=...)`, reads `transaction._id`, runs the wrapped function, then
# calls `transaction._commit()`. A `google.api_core.exceptions.Aborted` out
# of `_commit()` (only when `transaction._read_only` is false) retries up to
# `transaction._max_attempts` times before giving up with a `ValueError`; any
# other exception -- including that final `ValueError` -- triggers
# `transaction._rollback()` and propagates. These tests exercise the REAL
# decorator against the fake's transaction object, not a reimplementation of
# the decorator's own logic.


def test_transaction_that_raises_leaves_the_store_untouched():
    """A write buffered inside a transactional function must never reach the
    store if the function itself fails -- the whole point of wrapping
    queue/lease/ledger writes in a transaction instead of plain `set()`
    calls.
    """
    db = FakeFirestore()
    ref = db.collection("c").document("d")

    @firestore.transactional
    def doomed(transaction):
        transaction.set(ref, {"n": 1})
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        doomed(db.transaction())

    assert ref.get().exists is False


def test_contended_transaction_retries_and_only_the_winning_attempt_commits():
    """`contend_once()` makes the FIRST commit attempt raise Aborted, exactly
    as a real contended write would. The real `_Transactional` catches that,
    re-runs the wrapped function from scratch, and commits again -- proving
    the retry is driven by the real decorator, not by this fake pretending to
    retry on its own. The first attempt's buffered write must not survive: if
    it did, a losing caller retrying after a race could still clobber
    whatever a winner already wrote.
    """
    db = FakeFirestore()
    ref = db.collection("leases").document("job")
    attempts = []

    @firestore.transactional
    def acquire(transaction, holder):
        attempts.append(holder)
        snapshot = next(transaction.get(ref))
        if snapshot.exists:
            return snapshot.to_dict()["holder"]
        transaction.set(ref, {"holder": holder})
        return holder

    db.contend_once()
    winner = acquire(db.transaction(), "A")

    assert winner == "A"
    assert attempts == ["A", "A"]  # ran twice: the forced abort caused a retry
    assert ref.get().to_dict() == {"holder": "A"}  # committed exactly once


def test_transaction_gives_up_after_max_attempts_instead_of_looping_forever():
    """Perpetual contention (re-armed on every attempt) must not become an
    infinite loop: the real decorator gives up after `max_attempts` and
    raises `ValueError`, and the store must show no trace of any of the
    failed attempts.
    """
    db = FakeFirestore()
    ref = db.collection("c").document("d")
    attempts = []

    @firestore.transactional
    def always_aborts(transaction):
        attempts.append(len(attempts) + 1)
        db.contend_once()
        transaction.set(ref, {"n": attempts[-1]})

    with pytest.raises(ValueError):
        always_aborts(db.transaction(max_attempts=3))

    assert len(attempts) == 3
    assert ref.get().exists is False


def test_transaction_get_on_a_document_returns_a_one_item_iterator():
    """A genuine quirk of the real API, verified against the installed
    library: `Transaction.get(a_document_ref)` does NOT return a snapshot --
    it dispatches through `Client.get_all([ref], transaction=self)`, so it
    returns a length-1 iterator. Code that writes
    `transaction.get(ref).to_dict()` fails against both the real client and
    this fake; it must be `next(transaction.get(ref)).to_dict()`. The plain,
    non-transactional `ref.get(transaction=transaction)` form is the one that
    returns a bare snapshot -- both are exercised here so the difference is
    unmissable.
    """
    db = FakeFirestore()
    ref = db.collection("c").document("d")
    ref.set({"a": 1})

    @firestore.transactional
    def read_both(transaction):
        via_transaction = list(transaction.get(ref))
        via_reference = ref.get(transaction=transaction)
        return via_transaction, via_reference

    via_transaction, via_reference = read_both(db.transaction())

    assert len(via_transaction) == 1
    assert via_transaction[0].to_dict() == {"a": 1}
    assert via_reference.to_dict() == {"a": 1}


# --- behaviour 1: SERVER_TIMESTAMP resolves on write -------------------------


def test_server_timestamp_resolves_to_a_real_datetime_from_the_injected_clock():
    """The service writes `firestore.SERVER_TIMESTAMP` and later reads back a
    `datetime`. If this fake stored the sentinel itself, every downstream
    comparison (e.g. `count_created_since`'s `>=` filter) would silently
    compare against a `Sentinel` object instead of a timestamp and pass or
    fail for the wrong reason.
    """
    fixed = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    db = FakeFirestore(clock=lambda: fixed)
    ref = db.collection("c").document("d")

    ref.set({"created_at": firestore.SERVER_TIMESTAMP, "name": "unchanged"})

    data = ref.get().to_dict()
    assert data["created_at"] == fixed
    assert isinstance(data["created_at"], datetime)
    assert data["name"] == "unchanged"


def test_server_timestamp_also_resolves_through_update():
    fixed = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    db = FakeFirestore(clock=lambda: fixed)
    ref = db.collection("c").document("d")
    ref.set({"a": 1})

    ref.update({"updated_at": firestore.SERVER_TIMESTAMP})

    assert ref.get().to_dict() == {"a": 1, "updated_at": fixed}


# --- behaviour 4: missing field vs. null field vs. a range filter -----------


def test_where_excludes_documents_missing_the_filtered_field():
    """A document written before a field existed carries no such key at all.
    Firestore drops it from the result entirely rather than treating the
    absence as a falsy or zero value -- `count_created_since` depends on
    exactly this so pre-`created_at` documents are never miscounted as if
    they were created at the Unix epoch.
    """
    db = FakeFirestore()
    col = db.collection("c")
    col.document("has-field").set({"created_at": datetime(2026, 1, 1, tzinfo=UTC)})
    col.document("no-field").set({"name": "legacy"})

    results = col.where(
        filter=FieldFilter("created_at", ">=", datetime(2020, 1, 1, tzinfo=UTC))
    ).stream()

    assert [s.id for s in results] == ["has-field"]


def test_where_excludes_an_explicit_null_from_a_range_filter():
    """Missing and null are not the same thing, and Firestore treats them the
    same way here only by coincidence: a field explicitly set to null fails a
    range comparison just as one that was never set does, because null does
    not compare greater than or less than anything.
    """
    db = FakeFirestore()
    col = db.collection("c")
    col.document("has-value").set({"created_at": datetime(2026, 1, 1, tzinfo=UTC)})
    col.document("null-value").set({"created_at": None})

    results = col.where(
        filter=FieldFilter("created_at", ">=", datetime(2020, 1, 1, tzinfo=UTC))
    ).stream()

    assert [s.id for s in results] == ["has-value"]


def test_order_by_excludes_documents_missing_the_ordered_field():
    db = FakeFirestore()
    col = db.collection("c")
    col.document("has-field").set({"rank": 2})
    col.document("no-field").set({"name": "legacy"})
    col.document("also-has").set({"rank": 1})

    results = list(col.order_by("rank").stream())

    assert [s.id for s in results] == ["also-has", "has-field"]


def test_order_by_includes_an_explicit_null_sorted_first():
    """Unlike a missing field, an explicit null IS present -- Firestore
    includes it in an ordered result, sorted as the smallest value. This fake
    follows the same rule so a queue ordered by a nullable priority field
    doesn't quietly drop rows a real query would return.
    """
    db = FakeFirestore()
    col = db.collection("c")
    col.document("has-value").set({"rank": 1})
    col.document("null-value").set({"rank": None})

    results = list(col.order_by("rank").stream())

    assert [s.id for s in results] == ["null-value", "has-value"]
