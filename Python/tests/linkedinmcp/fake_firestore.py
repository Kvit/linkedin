"""An in-memory stand-in for `google.cloud.firestore.Client`.

The outreach service keeps its state in Firestore: a queue of outbound
messages, a lease that stops two scheduled jobs sending at once, an action
ledger, and a decision inbox. Every one of those is about *ordering and
atomicity* -- claim before send, settle exactly once, one lease holder at a
time. `unittest.mock` cannot exercise that; this fake actually stores
documents and actually enforces a transaction.

Construct one with `FakeFirestore()` and use it exactly like a real
`Client`: `db.collection(name)`, `.document(doc_id)`, `.batch()`,
`.transaction()`. The real `google.cloud.firestore_v1.base_query.FieldFilter`
and the real `firestore.SERVER_TIMESTAMP` sentinel are used as-is rather than
imitated -- a test that passes against an imitation of the query API proves
less than one that passes against the real object. Similarly, transactions
are driven by the REAL `@firestore.transactional` decorator
(`google.cloud.firestore_v1.transaction.transactional`); this module supplies
only the transaction object that decorator's `_Transactional` expects, never
a decorator of its own.

Two test-only knobs live on the constructor:

- `clock`: a zero-argument callable returning the `datetime` that
  `firestore.SERVER_TIMESTAMP` resolves to on write. Defaults to
  `datetime.now(UTC)`, called once per write so every `SERVER_TIMESTAMP` in
  the same `set`/`update` gets the same value, matching one real write
  getting one server commit time.
- `shuffle_seed`: seeds the `random.Random` that orders `get_all()`'s
  results, so a test that depends on the real API's "arbitrary order" can
  reproduce a specific ordering instead of flaking.

Test-only control surface beyond the constructor:

- `FakeFirestore.contend_once()`: arms exactly one `Aborted` on the NEXT
  transaction commit (any transaction on this `db`), then disarms itself.
  This is how a test proves the retry loop inside the real
  `@firestore.transactional` decorator actually runs, and that a losing
  attempt's buffered writes never reach the store. Call it again from inside
  a transactional function, every attempt, to simulate perpetual contention
  (see the "gives up after max_attempts" test).

## Library surprises confirmed against the installed client

- `FieldFilter(field, "==", None)` and `FieldFilter(field, "!=", None)` do
  not stay `"=="`/`"!="` by the time a query runs: the real `FieldFilter.
  __init__` rewrites its own `op_string` at construction to the enum members
  `StructuredQuery.UnaryFilter.Operator.IS_NULL` / `.IS_NOT_NULL` (see
  `_validate_opation` in the installed `base_query.py`). No other operator
  accepts a `None` value -- every one besides `==`/`!=` raises `ValueError`
  inside `FieldFilter.__init__` itself when given `None`, before a filter
  object exists at all. This fake matches both enum members: `IS_NULL`
  matches a document whose field is present and `None`; `IS_NOT_NULL`
  matches one whose field is present and not `None`. A document missing the
  field matches neither, the same "no value, no match" rule every other
  operator follows.
- A query with NO `order_by()` at all is not insertion-ordered. Real
  Firestore always applies one more, implicit `order_by` clause -- document
  id -- as the final tiebreak (see `BaseQuery._comparator` in the installed
  `base_query.py`): ascending when the query has no `order_by()` of its own,
  and otherwise document id in the direction of the LAST `order_by()`
  clause. `limit()` is applied after this ordering, never before it.

## What this deliberately does not do

No composite-index simulation, no security rules, no subcollections, no
`not-in` / `array_contains_any`, no pagination cursors
(`start_at`/`start_after`/`end_at`/`end_before`), no listeners, no dotted or
nested field paths in `where()`/`order_by()` (top-level field names only) or
in `update()` (real `DocumentReference.update({"a.b": 1})` writes the nested
field `b` of map `a`; this fake writes a literal top-level key `"a.b"`
instead -- every document the current service writes is flat, so this has
not mattered yet), no `.get()` on a collection or query (only `.stream()` --
see below), and no real optimistic-concurrency conflict detection between two
transactions. The only way to make a transaction abort is the explicit
`contend_once()` test helper. If a later task needs any of these, that task
adds it with a test -- a fake that grows features nobody uses is a second
implementation to maintain.

`mcp_server.py`'s `_firestore_health` already calls
`db.collection("analysis").select([]).limit(1).get()` (`.get()`, not
`.stream()`), tested today via its own local fake in `test_mcp_server.py`. If
a later task ever points that code at THIS fake, `.get()` will need adding
then.

`set(merge=True)` and `SERVER_TIMESTAMP` resolution are both shallow: only
top-level keys are merged or scanned. Every document the current service
writes is a flat map, so this is not a simplification that hides anything
today -- but a nested `SERVER_TIMESTAMP` or a merge into a nested map would
silently not resolve/merge the way real Firestore's field-mask merge does.

## Firestore API surface implemented here

- `FakeFirestore`: `.collection(name)`, `.get_all(refs, field_paths=None)`,
  `.batch()`, `.transaction(max_attempts=5, read_only=False)`,
  `.contend_once()`
- `FakeCollectionReference`: `.document(doc_id=None)`, `.where(filter=...)`
  (`==`, `!=`, `>`, `>=`, `<`, `<=`, `in`, `array_contains` -- `queue.py`'s
  `tags` -- plus `== None`/`!= None` -- see
  "Library surprises" above), `.order_by(field, direction=...)` (ascending
  by default; document id is always the final tiebreak), `.limit(n)`,
  `.select(fields)`, `.count(alias=...)`, `.stream()`. A `where` or
  `order_by` on `FieldPath.document_id()` (`__name__`) works on the
  document id: a filter's value must be a document reference in the same
  collection (a bare string raises `InvalidArgument`, as real Firestore
  answers one), with `==`, `!=` and the four range operators
- `FakeDocumentReference`: `.get(transaction=None)`, `.create(data)`, `.set(data,
  merge=False)`, `.update(data)`, `.delete()`
- `FakeWriteBatch`: `.set()`, `.update()`, `.delete()`, `.commit()`
- `FakeTransaction`: the `_Transactional` protocol (`_clean_up`, `_begin`,
  `_id`, `_read_only`, `_max_attempts`, `_commit`, `_rollback`), plus
  `.get(ref_or_query)`, `.get_all(refs)`, `.set()`, `.update()`, `.delete()`
  for the wrapped function to use
"""

import copy
import random
import string
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from google.api_core import exceptions as api_exceptions
from google.cloud import firestore
from google.cloud.exceptions import NotFound
from google.cloud.firestore_v1.types import StructuredQuery

_MISSING = object()  # sentinel: "this document has no such key at all"

_AUTO_ID_ALPHABET = string.ascii_letters + string.digits


def _auto_id() -> str:
    """A 20-character alphanumeric id, the same shape as a real Firestore
    push id.

    Uses the module-level `random` functions -- deliberately NOT a
    `FakeFirestore`'s seeded `_rng` -- so inventing a document id never
    perturbs the sequence `get_all()`'s seeded shuffle depends on for
    reproducible tests.
    """
    return "".join(random.choices(_AUTO_ID_ALPHABET, k=20))


_RANGE_OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">": lambda a, v: a > v,
    ">=": lambda a, v: a >= v,
    "<": lambda a, v: a < v,
    "<=": lambda a, v: a <= v,
}
_OTHER_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda a, v: a == v,
    "!=": lambda a, v: a != v,
    "in": lambda a, v: a in v,
    # A field that is not an array never matches, as in real Firestore.
    "array_contains": lambda a, v: isinstance(a, list) and v in a,
}
# `FieldFilter(field, "==", None)` / `FieldFilter(field, "!=", None)` do not
# reach here as the strings "==" / "!=" at all: the real `FieldFilter.
# __init__` rewrites its own `op_string` at construction time (see
# `_validate_opation` in the installed `base_query.py`) to these two enum
# members. No other operator accepts a `None` value -- every one besides
# `==`/`!=` raises `ValueError` inside `FieldFilter.__init__` itself when
# given `None`, before a filter object exists -- so `_RANGE_OPS`/`_OTHER_OPS`
# never see a `None` filter value.
_UNARY_OPS: dict[Any, Callable[[Any], bool]] = {
    StructuredQuery.UnaryFilter.Operator.IS_NULL: lambda actual: actual is None,
    StructuredQuery.UnaryFilter.Operator.IS_NOT_NULL: lambda actual: actual is not None,
}
_SUPPORTED_OPS = frozenset(_RANGE_OPS) | frozenset(_OTHER_OPS) | frozenset(_UNARY_OPS)

#: `FieldPath.document_id()`: the field path that means "the document id".
_DOCUMENT_ID = "__name__"


def _passes_document_id_filter(collection_name: str, doc_id: str, field_filter) -> bool:
    """Whether the document `doc_id` of `collection_name` matches a filter on
    its id (`FieldPath.document_id()`).

    The value must be a document reference, as the real client needs it to
    be: it encodes a `DocumentReference` as a reference value, and a plain
    string as a string, which real Firestore answers with InvalidArgument
    ("__key__ filter value must be a Key") -- raised here too. Ids are
    compared as strings, which matches Firestore's order for the ids this
    service writes; a reference into another collection and an `in` list
    are not implemented.
    """
    value = field_filter.value
    if not isinstance(value, FakeDocumentReference):
        raise api_exceptions.InvalidArgument("__key__ filter value must be a Key")
    if value._collection_name != collection_name:
        raise NotImplementedError("FakeFirestore compares document ids within one collection only")
    op = field_filter.op_string
    if op in _RANGE_OPS:
        return _RANGE_OPS[op](doc_id, value.id)
    if op in ("==", "!="):
        return _OTHER_OPS[op](doc_id, value.id)
    raise NotImplementedError(f"FakeFirestore does not implement the {op!r} operator on document ids")


def _passes_filter(body: dict, field_filter) -> bool:
    """Whether `body` matches one `FieldFilter`.

    Pins several real, easy-to-miss Firestore rules:

    - a document that lacks the filtered field entirely is excluded from
      EVERY operator, including the two null-equality ones below -- there is
      no value to compare, so it never matches;
    - an explicit `None` fails a range comparison (`>`, `>=`, `<`, `<=`) even
      though the field is present, because null does not compare greater or
      less than anything. `==`/`!=`/`in` are left as plain Python, since
      `None == None` and `None != 5` are already the answers Firestore gives;
    - `FieldFilter(field, "==", None)` and `FieldFilter(field, "!=", None)`
      arrive here as the enum members `StructuredQuery.UnaryFilter.
      Operator.IS_NULL` / `.IS_NOT_NULL`, not as `"=="`/`"!="` (see
      `_UNARY_OPS` above). `IS_NULL` matches a document whose field is
      present and `None`; `IS_NOT_NULL` matches one whose field is present
      and not `None`. A document missing the field matches neither, same as
      every other operator.

    A `TypeError` from comparing incompatible types (e.g. a stored string
    against a `datetime` filter value) is treated the same way: no match,
    not a crash -- Firestore's own type ordering never matches those either.
    """
    op = field_filter.op_string
    if op not in _SUPPORTED_OPS:
        raise NotImplementedError(f"FakeFirestore does not implement the {op!r} operator")

    actual = body.get(field_filter.field_path, _MISSING)
    if actual is _MISSING:
        return False

    if op in _UNARY_OPS:
        return _UNARY_OPS[op](actual)

    if op in _RANGE_OPS:
        if actual is None:
            return False
        try:
            return _RANGE_OPS[op](actual, field_filter.value)
        except TypeError:
            return False

    return _OTHER_OPS[op](actual, field_filter.value)


_DIRECTIONS = frozenset({"ASCENDING", "DESCENDING"})


def _write_set(docs: dict, doc_id: str, data: dict, merge: bool, resolve) -> None:
    resolved = resolve(data)
    docs[doc_id] = {**docs.get(doc_id, {}), **resolved} if merge else resolved


def _write_update(docs: dict, doc_id: str, data: dict, resolve, doc_path: str) -> None:
    if doc_id not in docs:
        raise NotFound(f"No document to update: {doc_path}")
    docs[doc_id] = {**docs[doc_id], **resolve(data)}


class _FakeSnapshot:
    """Mirrors `google.cloud.firestore_v1.document.DocumentSnapshot` as far
    as the service needs: `.exists`, `.id`, `.to_dict()`, `.reference`.

    `to_dict()` returns `None` -- not `{}` -- when the document does not
    exist, pinning the same real, easy-to-miss rule as the actual
    `DocumentSnapshot.to_dict`: `if snapshot.to_dict():` and `if
    snapshot.exists:` are NOT interchangeable when the document might be
    absent.

    Data is deep-copied on construction and again on every `to_dict()` call,
    so mutating a returned dict can never corrupt the store or a snapshot
    taken earlier.
    """

    def __init__(self, reference, body: dict | None, exists: bool, select_fields=None):
        self.reference = reference
        self.id = reference.id
        self.exists = exists
        if not exists:
            self._data = None
            return
        data = body if select_fields is None else {k: v for k, v in body.items() if k in select_fields}
        self._data = copy.deepcopy(data)

    def to_dict(self) -> dict | None:
        return copy.deepcopy(self._data) if self._data is not None else None


class FakeDocumentReference:
    """Mirrors `google.cloud.firestore_v1.document.DocumentReference`."""

    def __init__(self, db: "FakeFirestore", collection_name: str, doc_id: str):
        self._db = db
        self._collection_name = collection_name
        self.id = doc_id

    @property
    def path(self) -> str:
        return f"{self._collection_name}/{self.id}"

    def _store(self) -> dict:
        return self._db._collection_store(self._collection_name)

    def get(self, transaction=None) -> _FakeSnapshot:
        """`transaction` is accepted for signature parity with the real
        `DocumentReference.get(transaction=...)`, which returns a bare
        snapshot -- unlike `Transaction.get(ref)`, which does not (see
        `FakeTransaction.get`). This fake has no real MVCC snapshotting, so a
        transactional read is simply a live read of the current store.
        """
        body = self._store().get(self.id)
        if body is None:
            return _FakeSnapshot(self, None, exists=False)
        return _FakeSnapshot(self, body, exists=True)

    def create(self, document_data: dict) -> None:
        """Mirrors real `DocumentReference.create`: fails on an existing
        document instead of overwriting it, unlike `set()`.

        Raises `google.api_core.exceptions.AlreadyExists` -- a subclass of
        `Conflict`, which is what production code is expected to catch, since
        that is the real API's own exception hierarchy for this call -- and
        leaves the stored document untouched. Otherwise stores `document_data`
        exactly as `set(document_data)` would, including `SERVER_TIMESTAMP`
        resolution.
        """
        if self.id in self._store():
            raise api_exceptions.AlreadyExists(f"Document already exists: {self.path}")
        _write_set(self._store(), self.id, document_data, False, self._db._resolve_server_timestamps)

    def set(self, document_data: dict, merge: bool = False) -> None:
        _write_set(self._store(), self.id, document_data, merge, self._db._resolve_server_timestamps)

    def update(self, field_updates: dict) -> None:
        _write_update(self._store(), self.id, field_updates, self._db._resolve_server_timestamps, self.path)

    def delete(self) -> None:
        self._store().pop(self.id, None)


class _FakeAggregationQuery:
    """Mirrors `AggregationQuery`: `.get()` returns one result row per
    aggregation requested, wrapped in an outer list -- real Firestore batches
    multiple aggregations into one server round trip. Only `count()` is
    implemented, so that outer/inner list is always length 1.

    The count is computed lazily, inside `get()`, so it reflects the store at
    the moment `.get()` is called -- not when `.count()` built this object --
    matching how a real query only executes when asked for results.
    """

    def __init__(self, query: "_FakeQuery", alias: str | None):
        self._query = query
        self._alias = alias

    def get(self):
        n = len(self._query._matching_bodies())
        return [[SimpleNamespace(alias=self._alias, value=n)]]


class _FakeQuery:
    """Mirrors `google.cloud.firestore_v1.query.Query`: an immutable builder
    that accumulates filters, an ordering, a limit and a field projection,
    and only touches the store when asked to produce results (`stream`,
    `count`). Each builder method returns a NEW `_FakeQuery`, exactly like
    the real one, so branching from a shared base query
    (`base = col.where(...); a = base.where(...); b = base.limit(5)`) does
    not let branches interfere with each other.
    """

    def __init__(self, collection: "FakeCollectionReference"):
        self._collection = collection
        self._filters: tuple = ()
        self._orders: tuple[tuple[str, str], ...] = ()
        self._limit: int | None = None
        self._select_fields: tuple[str, ...] | None = None

    def _copy(self, **changes) -> "_FakeQuery":
        clone = _FakeQuery(self._collection)
        clone._filters = changes.get("filters", self._filters)
        clone._orders = changes.get("orders", self._orders)
        clone._limit = changes.get("limit", self._limit)
        clone._select_fields = changes.get("select_fields", self._select_fields)
        return clone

    def where(self, field_path=None, op_string=None, value=None, *, filter=None) -> "_FakeQuery":
        if field_path is not None or op_string is not None:
            raise NotImplementedError(
                "FakeFirestore only supports where(filter=FieldFilter(...)); "
                "the positional form is deprecated in the real client too, "
                "and the service never calls it."
            )
        if filter is None:
            raise ValueError("where() requires filter=FieldFilter(...)")
        return self._copy(filters=self._filters + (filter,))

    def order_by(self, field_path: str, direction: str = "ASCENDING") -> "_FakeQuery":
        if direction not in _DIRECTIONS:
            raise ValueError(f"Invalid direction {direction!r}; must be 'ASCENDING' or 'DESCENDING'")
        return self._copy(orders=self._orders + ((field_path, direction),))

    def limit(self, count: int) -> "_FakeQuery":
        return self._copy(limit=count)

    def select(self, field_paths: Iterable[str]) -> "_FakeQuery":
        return self._copy(select_fields=tuple(field_paths))

    def _matching_bodies(self) -> list[tuple[str, dict]]:
        items = list(self._collection._store().items())
        collection_name = self._collection._name

        for field_filter in self._filters:
            if field_filter.field_path == _DOCUMENT_ID:
                items = [
                    (doc_id, body)
                    for doc_id, body in items
                    if _passes_document_id_filter(collection_name, doc_id, field_filter)
                ]
            else:
                items = [(doc_id, body) for doc_id, body in items if _passes_filter(body, field_filter)]

        # A document missing an order_by field is absent from the result
        # entirely (behaviour 4) -- but one holding an explicit None for that
        # field IS present, and sorts as the smallest value. Every document
        # has an id, so ordering by `FieldPath.document_id()` drops none.
        for field_path, _direction in self._orders:
            if field_path == _DOCUMENT_ID:
                continue
            items = [(doc_id, body) for doc_id, body in items if field_path in body]

        # Real Firestore always appends one more, implicit `order_by` clause
        # -- document id -- as the final tiebreak (verified against
        # `BaseQuery._comparator` in the installed `base_query.py`,
        # ~1221-1265): ascending when the query has no `order_by()` at all,
        # otherwise document id in the direction of the LAST `order_by()`
        # clause. Composed with the same least-significant-key-first stable
        # sort the loop below already uses for the caller's own `order_by()`
        # clauses: sort by id first, since it is always the least
        # significant key of all, then let each `order_by()` field re-sort
        # in turn from last (least significant) to first (most significant,
        # sorted last so it dominates).
        id_tiebreak_direction = self._orders[-1][1] if self._orders else "ASCENDING"
        items.sort(key=lambda item: item[0], reverse=(id_tiebreak_direction == "DESCENDING"))
        for field_path, direction in reversed(self._orders):
            if field_path == _DOCUMENT_ID:
                items.sort(key=lambda item: item[0], reverse=(direction == "DESCENDING"))
                continue
            items.sort(
                key=lambda item, fp=field_path: (item[1][fp] is not None, item[1][fp]),
                reverse=(direction == "DESCENDING"),
            )

        if self._limit is not None:
            items = items[: self._limit]
        return items

    def stream(self, transaction=None):
        """`transaction` is accepted, unused, for signature parity with the
        real `Query.stream(transaction=...)`."""
        for doc_id, body in self._matching_bodies():
            ref = self._collection.document(doc_id)
            yield _FakeSnapshot(ref, body, exists=True, select_fields=self._select_fields)

    def count(self, alias: str | None = None) -> _FakeAggregationQuery:
        return _FakeAggregationQuery(self, alias)


class FakeCollectionReference:
    """Mirrors `google.cloud.firestore_v1.collection.CollectionReference`.

    Every query-builder method (`where`, `order_by`, `limit`, `select`,
    `count`) builds a fresh, empty `_FakeQuery` scoped to this collection and
    delegates to it -- the same architecture the real client uses
    (`BaseCollectionReference._query()`), so a collection and a query expose
    the same builder surface without duplicating its logic.

    Two `FakeCollectionReference`s built from the same name (via
    `db.collection("x")` called twice) are independent objects sharing the
    SAME backing dict -- a stateless handle onto the store, not the store
    itself.
    """

    def __init__(self, db: "FakeFirestore", name: str):
        self._db = db
        self._name = name

    @property
    def id(self) -> str:
        return self._name

    def _store(self) -> dict:
        return self._db._collection_store(self._name)

    def document(self, document_id: str | None = None) -> FakeDocumentReference:
        if document_id is None:
            document_id = _auto_id()
        return FakeDocumentReference(self._db, self._name, document_id)

    def _query(self) -> _FakeQuery:
        return _FakeQuery(self)

    def where(self, field_path=None, op_string=None, value=None, *, filter=None) -> _FakeQuery:
        return self._query().where(field_path, op_string, value, filter=filter)

    def order_by(self, field_path: str, direction: str = "ASCENDING") -> _FakeQuery:
        return self._query().order_by(field_path, direction)

    def limit(self, count: int) -> _FakeQuery:
        return self._query().limit(count)

    def select(self, field_paths: Iterable[str]) -> _FakeQuery:
        return self._query().select(field_paths)

    def count(self, alias: str | None = None) -> _FakeAggregationQuery:
        return self._query().count(alias=alias)

    def stream(self, transaction=None):
        return self._query().stream()


def _commit_ops(db: "FakeFirestore", ops: list[tuple]) -> None:
    """Apply a batch's or transaction's buffered writes as one atomic unit.

    Each affected collection gets a working copy that already reflects
    earlier writes from the SAME commit -- so `set()` immediately followed by
    `update()` on a brand-new document is legal, exactly as it is for a real
    batch or transaction -- but that working copy is invisible to the rest of
    the store until every op has succeeded. If any `update` targets a
    document absent from its working copy, an exception propagates before
    the second loop runs, and the real store is never touched.
    """
    staged: dict[str, dict] = {}

    def working(collection_name: str) -> dict:
        if collection_name not in staged:
            staged[collection_name] = dict(db._collection_store(collection_name))
        return staged[collection_name]

    for kind, reference, data, merge in ops:
        docs = working(reference._collection_name)
        if kind == "set":
            _write_set(docs, reference.id, data, merge, db._resolve_server_timestamps)
        elif kind == "update":
            _write_update(docs, reference.id, data, db._resolve_server_timestamps, reference.path)
        elif kind == "delete":
            docs.pop(reference.id, None)
        else:  # pragma: no cover -- defensive; every caller in this module uses the three kinds above
            raise AssertionError(f"unknown buffered write kind: {kind!r}")

    for collection_name, docs in staged.items():
        live = db._collection_store(collection_name)
        live.clear()
        live.update(docs)


class FakeWriteBatch:
    """Mirrors `google.cloud.firestore_v1.batch.WriteBatch`: buffers writes
    and applies them only on `.commit()`, atomically -- see `_commit_ops`."""

    def __init__(self, db: "FakeFirestore"):
        self._db = db
        self._ops: list[tuple] = []

    def set(self, reference: FakeDocumentReference, document_data: dict, merge: bool = False) -> None:
        self._ops.append(("set", reference, document_data, merge))

    def update(self, reference: FakeDocumentReference, field_updates: dict) -> None:
        self._ops.append(("update", reference, field_updates, False))

    def delete(self, reference: FakeDocumentReference) -> None:
        self._ops.append(("delete", reference, None, False))

    def commit(self) -> list:
        _commit_ops(self._db, self._ops)
        self._ops = []
        return []


class FakeTransaction:
    """Satisfies the protocol the REAL `@firestore.transactional` decorator
    drives, so the service's lease function can carry that real decorator
    unmodified against this fake.

    The protocol is `_Transactional` in
    `google.cloud.firestore_v1.transaction`, read from the installed library
    while writing this class -- not reimplemented, only satisfied:

    - `_clean_up()`: discard buffered writes from a previous attempt.
    - `_begin(retry_id=None)`: mark the start of an attempt; sets `_id`.
    - `_id`: read right after `_begin`; any non-`None` value serves.
    - `_read_only`: whether `Aborted` from `_commit` is retried at all.
    - `_max_attempts`: how many times `_Transactional` will loop.
    - `_commit()`: apply buffered writes atomically, or raise
      `google.api_core.exceptions.Aborted` for a simulated contended commit
      (see `FakeFirestore.contend_once`).
    - `_rollback()`: called whenever any exception propagates out of the
      wrapped function or out of `_commit`; discards buffered writes.

    Reads inside the wrapped function see the live store; writes buffer here
    and are only applied -- all atomically, see `_commit_ops` -- by a
    successful `_commit()`.

    `.get(ref_or_query)` deliberately mirrors a genuine surprise in the real
    API (verified against the installed library): given a single document
    reference it does NOT return a snapshot. It dispatches through
    `Client.get_all([ref], transaction=self)`, exactly as the real
    `Transaction.get` does, so it returns a length-1 iterator. Code that
    expects a bare snapshot from `transaction.get(ref)` breaks against both
    the real client and this fake; the idiomatic single-snapshot form is the
    non-transactional `ref.get(transaction=transaction)`.
    """

    def __init__(self, db: "FakeFirestore", max_attempts: int = 5, read_only: bool = False):
        self._db = db
        self._max_attempts = max_attempts
        self._read_only = read_only
        self._id: str | None = None
        self._ops: list[tuple] = []

    # -- the _Transactional protocol ---------------------------------------

    def _clean_up(self) -> None:
        self._ops = []
        self._id = None

    def _begin(self, retry_id=None) -> None:
        self._id = uuid.uuid4().hex

    def _commit(self) -> list:
        if self._db._consume_contend_once():
            raise api_exceptions.Aborted("FakeFirestore.contend_once(): simulated contention")
        _commit_ops(self._db, self._ops)
        self._ops = []
        return []

    def _rollback(self) -> None:
        self._clean_up()

    # -- reads and buffered writes for the wrapped function ----------------

    def get(self, ref_or_query):
        if isinstance(ref_or_query, FakeDocumentReference):
            return iter(self._db.get_all([ref_or_query]))
        return ref_or_query.stream()

    def get_all(self, references: list):
        return self._db.get_all(references)

    def set(self, reference: FakeDocumentReference, document_data: dict, merge: bool = False) -> None:
        self._ops.append(("set", reference, document_data, merge))

    def update(self, reference: FakeDocumentReference, field_updates: dict) -> None:
        self._ops.append(("update", reference, field_updates, False))

    def delete(self, reference: FakeDocumentReference) -> None:
        self._ops.append(("delete", reference, None, False))


class FakeFirestore:
    """A stand-in for `google.cloud.firestore.Client`. See the module
    docstring for the full surface and the behaviours it pins."""

    def __init__(self, clock: Callable[[], datetime] | None = None, shuffle_seed: int = 0):
        self._data: dict[str, dict[str, dict]] = {}
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._rng = random.Random(shuffle_seed)
        self._contend_once_armed = False

    def _collection_store(self, name: str) -> dict:
        return self._data.setdefault(name, {})

    def _resolve_server_timestamps(self, data: dict) -> dict:
        """One `_clock()` call per write, reused for every SERVER_TIMESTAMP
        field in `data` -- matching one real write getting one server commit
        time, not one timestamp per field."""
        now = self._clock()
        return {k: (now if v is firestore.SERVER_TIMESTAMP else v) for k, v in data.items()}

    def _consume_contend_once(self) -> bool:
        armed = self._contend_once_armed
        self._contend_once_armed = False
        return armed

    def contend_once(self) -> None:
        """Test-only: make the NEXT transaction commit (on any transaction
        from this `db`) raise `Aborted` exactly once, then disarm.

        This is how a test proves the real `@firestore.transactional`
        retry loop actually drives this fake, and that a losing attempt's
        buffered writes never reach the store. Call it again from inside the
        wrapped function itself (every attempt) to simulate contention that
        never clears, e.g. to test giving up after `_max_attempts`.
        """
        self._contend_once_armed = True

    def collection(self, name: str) -> FakeCollectionReference:
        return FakeCollectionReference(self, name)

    def get_all(self, references: list, field_paths: Iterable[str] | None = None) -> list:
        """Order is not guaranteed by the real API; shuffle deliberately so
        code that accidentally depends on request order fails here instead
        of in production. Seeded (`FakeFirestore(shuffle_seed=...)`) so a
        failure reproduces instead of flaking.

        `field_paths`, when given, projects each returned (existing)
        snapshot to just those top-level fields -- mirrors the real client's
        `get_all(references, field_paths=...)` parameter. Existence is
        unaffected: a reference `get_all` cannot find is still
        `exists=False` regardless of `field_paths`. Top-level fields only,
        the same restriction `.select()` on a query already has (see the
        module docstring).
        """
        select_fields = tuple(field_paths) if field_paths is not None else None
        snapshots = []
        for ref in references:
            body = ref._store().get(ref.id)
            exists = body is not None
            snapshots.append(_FakeSnapshot(ref, body, exists=exists, select_fields=select_fields))
        self._rng.shuffle(snapshots)
        return snapshots

    def batch(self) -> FakeWriteBatch:
        return FakeWriteBatch(self)

    def transaction(self, max_attempts: int = 5, read_only: bool = False) -> FakeTransaction:
        return FakeTransaction(self, max_attempts=max_attempts, read_only=read_only)
