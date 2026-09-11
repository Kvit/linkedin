"""`fetch_queue/{slug}` -- one document per LinkedIn slug the daily job found
among recent connections whose profile is not stored yet (ledger ruling
P3-1). Task 3b's tick reads `next_queued` to pick ONE `queued` document per
tick that sends nothing, fetches and classifies that profile, and records
the outcome here with `mark` (a clean `stored`/`short`/`failed` outcome),
`note_attempt` (one more failed try, giving up as `failed` at
`MAX_ATTEMPTS`) or `requeue_incomplete` (LinkedIn withheld the profile's
sections: back of the queue, giving up as `failed` at `MAX_INCOMPLETE`,
ruling P3-3). Nothing in this module fetches or classifies anything -- it
only stores the queue task 3b drives.

Same conventions as `queue.py` and `state.py`: `enqueue` is create-only
(`ref.create`, catching `google.api_core.exceptions.Conflict`), so the daily
job can call it for the same slug every morning without disturbing whatever
progress this queue has already made on it. `mark`, `note_attempt` and
`requeue_incomplete` are the REAL `google.cloud.firestore.transactional`
decorator, read with `ref.get(transaction=transaction)` and written with
`transaction.set(..., merge=True)`, exactly as `state.py` and `queue.py` use
it: each re-reads the current status inside the transaction and refuses an
illegal move by returning `False`/`None` rather than raising.
`google.cloud.firestore`, `FieldFilter` and `google.api_core.exceptions` are
imported inside the functions that need them, matching every other storage
module here, so importing this module opens nothing.

Every query is single-field: `next_queued` is one equality
`where(status == queued)`, sorted in Python; `counts` is one `in` query over
all four statuses, projected with `select(["status"])` and counted in Python;
`settled_ids` one `in` query over the three settled ones, ids only.
`counts` runs ONE query rather than one per status (contrast `queue.counts`
and `decisions.counts`) because it counts every status this collection has,
not just an OPEN subset -- an `in` over all of them is one query instead of
four.
"""

import re
from datetime import datetime

FETCH_COLLECTION = "fetch_queue"

QUEUED, STORED, SHORT, FAILED = "queued", "stored", "short", "failed"
MAX_ATTEMPTS = 3

#: Ruling P3-3: how many times LinkedIn may withhold a profile's sections
#: before the slug is given up as `failed` -- it then waits for the notebook.
MAX_INCOMPLETE = 3
INCOMPLETE_LIMIT_ERROR = f"LinkedIn withheld sections {MAX_INCOMPLETE} times"

#: Every status this collection uses, in a fixed order so `counts()`'s
#: returned dict has a deterministic key order -- the same reasoning
#: `queue._OPEN_ORDERED` documents for its own `counts()`.
_STATUSES = (QUEUED, STORED, SHORT, FAILED)

#: `mark`'s three legal targets: `queued` is where every document starts, not
#: something `mark` transitions TO.
_MARK_TARGETS = (STORED, SHORT, FAILED)

#: A Firestore document id may not be empty, contain "/", be "." or "..",
#: or match this reserved-id pattern, and may be at most 1,500 bytes of
#: UTF-8 -- the same rules `messages_sync._check_document_id`,
#: `jobs._usable_document_id` and `mcp_server._usable_id` apply to their
#: own ids.
_RESERVED_SLUG = re.compile(r"^__.*__$")
_MAX_SLUG_BYTES = 1500


def usable_slug(slug: str) -> bool:
    """Whether `slug` could be stored as a `fetch_queue` document id at
    all: not empty, containing no `/`, not `.` or `..`, not matching the
    reserved `^__.*__$` pattern, and at most 1,500 bytes once encoded as
    UTF-8 (ruling P5-4). A string UTF-8 cannot encode -- a lone surrogate
    -- is not usable either, answered `False` rather than raised.

    `enqueue` calls this itself and raises `ValueError` when it is `False`,
    checked before any Firestore call. A caller that must not reach
    Firestore with a bad slug at all -- `jobs._enumerate_new_connections`'s
    dry-run branch, which never calls `enqueue` and so would otherwise never
    see that `ValueError` -- calls this directly instead, so both branches
    reject the same slugs.
    """
    if not slug or "/" in slug or slug in (".", "..") or _RESERVED_SLUG.match(slug):
        return False
    try:
        return len(slug.encode("utf-8")) <= _MAX_SLUG_BYTES
    except UnicodeEncodeError:
        return False


def enqueue(db, slug, *, provider_id, name, connected_at, now) -> bool:
    """Create `fetch_queue/{slug}` as `queued`, or -- when it already exists
    -- change nothing. Returns whether it was created.

    Stores exactly `slug`, `provider_id`, `name`, `connected_at`, `status =
    "queued"`, `queued_at = now`, `attempts = 0`, `incomplete_count = 0`,
    `last_error = None`, `updated_at = now`, `classified = None`.

    Raises `ValueError` for a `slug` `usable_slug` refuses -- empty,
    containing `/`, `.` or `..`, matching `^__.*__$` (a reserved Firestore
    id), or over 1,500 bytes of UTF-8 -- checked before any Firestore
    call, so a malformed slug never reaches `.create()`. Callers (the daily
    job) skip a slug this rejects rather than let the whole run fail on one
    bad connection.
    """
    if not usable_slug(slug):
        raise ValueError(f"enqueue: slug is not usable as a Firestore document id: {slug!r}")

    data = {
        "slug": slug,
        "provider_id": provider_id,
        "name": name,
        "connected_at": connected_at,
        "status": QUEUED,
        "queued_at": now,
        "attempts": 0,
        "incomplete_count": 0,
        "last_error": None,
        "updated_at": now,
        "classified": None,
    }

    from google.api_core import exceptions as api_exceptions

    ref = db.collection(FETCH_COLLECTION).document(slug)
    try:
        ref.create(data)
    except api_exceptions.Conflict:
        return False
    return True


def get(db, slug) -> dict | None:
    """The document's fields plus `"id"`, or `None` when it does not exist."""
    snapshot = db.collection(FETCH_COLLECTION).document(slug).get()
    if not snapshot.exists:
        return None
    return {**snapshot.to_dict(), "id": slug}


def _fetch_order(item: dict) -> tuple:
    """The newest connection first, whenever it was queued -- a backlog
    never holds back a connection made this week -- one with no
    `connected_at` after every dated one, ties broken by id. A profile
    LinkedIn withheld (`incomplete_count`, ruling P3-3) waits behind every
    fresh one, the earliest requeued first."""
    if item.get("incomplete_count"):
        return (1, item["queued_at"].timestamp(), item["id"])
    connected = item.get("connected_at")
    return (0, -connected.timestamp() if connected is not None else float("inf"), item["id"])


def next_queued(db, now: datetime, *, slugs: set[str] | None = None) -> dict | None:
    """The `queued` document to fetch next, in `_fetch_order`: the newest
    connection first, a withheld profile behind every fresh one; `None`
    when nothing is queued. With `slugs`, only one of those -- the
    `get_contacts` step fetches the connections it listed, never the rest
    of the queue.

    `now` is accepted for signature symmetry with `queue.next_due` (task 3b
    calls both from the same tick) but is not read: a queued profile has no
    due time of its own -- it is fetchable from the moment it is queued.

    ONE `where(status == queued)` query; the filter and sort are done in
    Python.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = db.collection(FETCH_COLLECTION).where(filter=FieldFilter("status", "==", QUEUED))
    items = [{**snapshot.to_dict(), "id": snapshot.id} for snapshot in query.stream()]
    if slugs is not None:
        items = [item for item in items if item["id"] in slugs]
    if not items:
        return None
    items.sort(key=_fetch_order)
    return items[0]


def settled_ids(db) -> set[str]:
    """The slugs this queue is done with -- `stored`, `short` or `failed` --
    which `next_queued` never returns again. ONE `in` query over the three,
    ids only."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = db.collection(FETCH_COLLECTION).where(filter=FieldFilter("status", "in", list(_MARK_TARGETS)))
    return {snapshot.id for snapshot in query.select([]).stream()}


def mark(db, slug, status, now, *, error=None, classified=None) -> bool:
    """`queued` -> `stored`/`short`/`failed`: task 3b's outcome of fetching
    and classifying one profile. Stores `updated_at` and `last_error =
    error` always; `classified` is written only for `status == "stored"` --
    a `short` or `failed` outcome leaves whatever `classified` already holds
    untouched. Returns whether it transitioned.

    Raises `ValueError` for a `status` outside `stored`/`short`/`failed`.
    Returns `False`, writing nothing, when the document is missing or is not
    currently `queued`.
    """
    if status not in _MARK_TARGETS:
        raise ValueError(f"mark: status must be one of {_MARK_TARGETS}, got {status!r}")

    from google.cloud import firestore

    ref = db.collection(FETCH_COLLECTION).document(slug)

    @firestore.transactional
    def _mark(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists or snapshot.to_dict().get("status") != QUEUED:
            return False
        changes = {"status": status, "last_error": error, "updated_at": now}
        if status == STORED:
            changes["classified"] = classified
        transaction.set(ref, changes, merge=True)
        return True

    return _mark(db.transaction())


def note_attempt(db, slug, now, error) -> dict | None:
    """Record one failed attempt on a `queued` document: increments
    `attempts`, stores `last_error` and `updated_at`. When `attempts` reaches
    `MAX_ATTEMPTS` in the same transaction, the status becomes `failed`.
    Returns the updated document (with `"id"`), or `None` when the document
    is missing or not currently `queued`.
    """
    from google.cloud import firestore

    ref = db.collection(FETCH_COLLECTION).document(slug)

    @firestore.transactional
    def _note(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        if data.get("status") != QUEUED:
            return None
        attempts = (data.get("attempts") or 0) + 1
        changes = {"attempts": attempts, "last_error": error, "updated_at": now}
        if attempts >= MAX_ATTEMPTS:
            changes["status"] = FAILED
        transaction.set(ref, changes, merge=True)
        return {**data, **changes, "id": slug}

    return _note(db.transaction())


def requeue_incomplete(db, slug, now, error) -> dict | None:
    """LinkedIn withheld the sections of a `queued` document's profile
    (ruling P3-3): move it to the back of the queue and count it. One
    transaction sets `queued_at = now`, increments `incomplete_count` (a
    document stored before that field existed counts from 0), and stores
    `last_error = error` and `updated_at = now` -- `next_queued` fetches
    every fresh slug before one with an `incomplete_count`, and those in
    `queued_at` order. When `incomplete_count` reaches `MAX_INCOMPLETE` the status
    becomes `failed` with `last_error = INCOMPLETE_LIMIT_ERROR`. `attempts`
    is not touched: a withheld profile is LinkedIn throttling, not a failed
    try.

    Returns the updated document (with `"id"`), or `None`, writing nothing,
    when the document is missing or not currently `queued`.
    """
    from google.cloud import firestore

    ref = db.collection(FETCH_COLLECTION).document(slug)

    @firestore.transactional
    def _requeue(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        if data.get("status") != QUEUED:
            return None
        count = (data.get("incomplete_count") or 0) + 1
        changes = {"queued_at": now, "incomplete_count": count, "last_error": error, "updated_at": now}
        if count >= MAX_INCOMPLETE:
            changes.update({"status": FAILED, "last_error": INCOMPLETE_LIMIT_ERROR})
        transaction.set(ref, changes, merge=True)
        return {**data, **changes, "id": slug}

    return _requeue(db.transaction())


def counts(db) -> dict[str, int]:
    """`{status: n}` for all four statuses, zeros included. ONE `in` query
    over `_STATUSES`, projected with `select(["status"])`, counted in
    Python.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    result = dict.fromkeys(_STATUSES, 0)
    query = (
        db.collection(FETCH_COLLECTION)
        .where(filter=FieldFilter("status", "in", list(_STATUSES)))
        .select(["status"])
    )
    for snapshot in query.stream():
        status = snapshot.to_dict().get("status")
        if status in result:
            result[status] += 1
    return result
