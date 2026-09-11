"""The durable, append-only record of every service-side LinkedIn action:
messages `tick` (2f) sends, and (task 3a) profile fetches task 3b attempts.

`action_log/{auto id}` is what `count_since` reads to answer "how many
messages has this service sent in the last N hours" -- the number `tick`
checks against the daily cap before it claims another queue item, and the
number a status report shows a human. It is written once per attempt and
never edited afterwards, so it survives a send whose queue item later gets
retried, corrected, or resolved from `unknown` to `sent`: the log keeps every
attempt, the queue (`queue.py`, `fetch_queue.py`) keeps only the current
status.

`entry` validates `kind`, `result` and `at` before anything is written, so a
typo -- `"snet"` instead of `"sent"` -- raises immediately rather than landing
silently in Firestore, invisible to every future `count_since` call that
filters on the real spelling. Validation is PER KIND: `RESULTS` is legal for
`kind == "message"`, `PROFILE_RESULTS` for `kind == "profile"` -- a result
valid for the other kind (e.g. `"sent"` on a `profile` row) raises just the
same as a result valid for neither, because the two kinds record different
things and their result vocabularies do not otherwise overlap (`"failed"`
is the one word both share).

`count_since` runs exactly ONE Firestore query -- `where(at >= cutoff)`, no
second `where`, no `order_by` -- and filters by `kind` and `results` in
Python instead. Ledger ruling P2-3: a second `where` or an `order_by` on top
of the range filter would need a composite index, and this project creates
none. `.select(["kind", "result"])` keeps the streamed documents small; `at`
itself is never read back, only compared server-side. `count_since` does not
itself validate `kind` against `KINDS` -- it filters on whatever string it is
given -- so it already works unchanged for `kind="profile"`.
"""

from collections.abc import Iterable
from datetime import datetime

LEDGER_COLLECTION = "action_log"
KINDS = ("message", "profile")
RESULTS = ("sent", "unknown", "failed")
PROFILE_RESULTS = ("stored", "short", "incomplete", "failed")

#: Which result vocabulary applies to which kind (see the module docstring).
_RESULTS_BY_KIND = {"message": RESULTS, "profile": PROFILE_RESULTS}


def entry(kind: str, contact_doc_id: str, result: str, at: datetime, queue_id: str | None = None) -> dict:
    """The document `record` writes: exactly `kind`, `contact_doc_id`,
    `result`, `queue_id`, `at`.

    Raises `ValueError` for a `kind` outside `KINDS`, a `result` outside the
    vocabulary `kind` allows (`RESULTS` for `message`, `PROFILE_RESULTS` for
    `profile` -- so a result valid only for the OTHER kind is rejected too),
    or a naive `at` -- the same "never guess a timezone" rule as
    `clock.local_date`.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown ledger kind: {kind!r}")
    if result not in _RESULTS_BY_KIND[kind]:
        raise ValueError(f"unknown ledger result for kind {kind!r}: {result!r}")
    if at.tzinfo is None:
        raise ValueError("entry: `at` must be timezone-aware")
    return {
        "kind": kind,
        "contact_doc_id": contact_doc_id,
        "result": result,
        "queue_id": queue_id,
        "at": at,
    }


def record(db, kind, contact_doc_id, result, at, queue_id=None, *, batch=None) -> None:
    """Write one `entry(...)` to a new auto-id document in `LEDGER_COLLECTION`.

    With `batch`, the write is only buffered (`batch.set(ref, data)`) --
    nothing is stored until the caller commits that batch. This is how
    `queue.settle` (2c) will write the ledger row and the queue-status update
    as one atomic commit.
    """
    data = entry(kind, contact_doc_id, result, at, queue_id)
    ref = db.collection(LEDGER_COLLECTION).document()
    if batch is not None:
        batch.set(ref, data)
    else:
        ref.set(data)


def count_since(db, kind: str, cutoff: datetime, results: Iterable[str] | None = None) -> int:
    """How many `kind` actions landed at or after `cutoff`, optionally
    limited to `results`.

    ONE query -- see the module docstring for why `kind` and `result` are
    checked in Python rather than added as a second server-side filter.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = (
        db.collection(LEDGER_COLLECTION)
        .where(filter=FieldFilter("at", ">=", cutoff))
        .select(["kind", "result"])
    )
    allowed_results = set(results) if results is not None else None

    count = 0
    for snapshot in query.stream():
        data = snapshot.to_dict()
        if data.get("kind") != kind:
            continue
        if allowed_results is not None and data.get("result") not in allowed_results:
            continue
        count += 1
    return count
