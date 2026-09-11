"""`outreach_queue/{id}` -- one document per outbound LinkedIn message, taken
through a two-phase claim so a message is never sent twice and never quietly
lost: `claim` (`approved` -> `sending`) reserves the item before any network
call is attempted, and `settle` (`sending` -> `sent`/`failed`/`unknown`)
records the outcome afterwards. Ids are deterministic (`intro_id`, `agent_id`)
so `enqueue` is create-only -- at most one intro and one agent/human item per
contact per local day, ever, no matter how many times a job or tool tries to
queue one.

The status graph is exact -- see the contract's transition table -- and every
transition function here except `settle` is a transaction (the real
`google.cloud.firestore.transactional` decorator, exactly as `state.py` uses
it: read with `ref.get(transaction=transaction)`, write with
`transaction.set(ref, ..., merge=True)`) that RE-READS the current status
inside the transaction and refuses an illegal move by returning `False`/
`None` -- never by raising. Two ticks racing to claim the same item, or a
human clicking "approve" twice, are both routine, not exceptional.

`settle` is the one function that does raise (`RuntimeError`), because it is
only ever called by the caller that just won its own `claim` -- there is no
other party to race against, so an item that is not `sending` there is a
programming error, not a lost race. It is also the one write that touches
three collections at once (the queue item, an `action_log` row via
`ledger.record`, and -- for a sent or unknown intro -- an `analysis` merge),
so it reads everything it needs FIRST and then commits all of it as ONE
`db.batch()`: partial settlement (the item marked `sent` but no ledger row)
would let the same message be sent again by a retry that finds no record of
it.

`analysis` holds the only copy of 14,158 contacts' names and emails and is
NEVER created here, only merged into when it already exists -- checked with
`snapshot.exists` before every merge, so a queue item for a contact whose
`analysis` document does not exist (or was deleted) cannot mint a new,
partial one.

Every query here is single-field -- one equality, one `in`, or one range
`where`, or one single-field `order_by` -- so it is served by Firestore's
automatic indexes; everything else (the second sort key, the status filter
when an `order_by` is already spent on `created_at`) is done in Python.
`google.cloud.firestore` and `FieldFilter` are imported inside each function
that needs them, matching `state.py` and `ledger.py`, so importing this
module opens nothing.
"""

import re
from datetime import date, datetime

from linkedinmcp import ledger

QUEUE_COLLECTION = "outreach_queue"

PENDING = "pending"
APPROVED = "approved"
SENDING = "sending"
SENT = "sent"
UNKNOWN = "unknown"
FAILED = "failed"
CANCELLED = "cancelled"
SKIPPED = "skipped"

OPEN = frozenset({PENDING, APPROVED, SENDING, UNKNOWN})
TERMINAL = frozenset({SENT, FAILED, CANCELLED, SKIPPED})
KINDS = ("intro", "follow_up", "drip_step", "reply")

# Iteration order for `counts()`: a tuple, not `OPEN` itself, so the returned
# dict's key order is deterministic across runs rather than following
# frozenset's arbitrary (hash-seed-dependent) iteration order.
_OPEN_ORDERED = (PENDING, APPROVED, SENDING, UNKNOWN)

_ENQUEUE_KEYS = frozenset(
    {
        "contact_doc_id",
        "kind",
        "text",
        "chat_id",
        "provider_id",
        "name",
        "profile_url",
        "campaign",
        "template_id",
        "due_at",
        "created_by",
        "tags",
    }
)

_ANALYSIS_COLLECTION = "analysis"

#: Campaign tags (`clean_tags`): at most `MAX_TAGS` per item, each a
#: lowercase slug of at most 40 characters -- so `Recovr` and `recovr` are
#: one tag, and a tag can go in a query unescaped.
MAX_TAGS = 10
_TAG_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")


def clean_tags(tags) -> list[str]:
    """`tags` stripped, lowercased and de-duplicated, in order; `[]` for
    `None`. Raises `ValueError`, saying why, for anything else than a list of
    at most `MAX_TAGS` strings that are each a lowercase slug -- letters,
    digits, `-`, `_` and `.`, starting with a letter or digit, at most 40
    characters."""
    if tags is None:
        return []
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("tags must be a list of strings.")
    cleaned = list(dict.fromkeys(tag.strip().lower() for tag in tags))
    bad = [tag for tag in cleaned if not _TAG_RE.fullmatch(tag)]
    if bad:
        raise ValueError(
            f"Each tag is 1 to 40 of a-z, 0-9, '-', '_' and '.', starting with a letter or digit: {bad[:3]}."
        )
    if len(cleaned) > MAX_TAGS:
        raise ValueError(f"At most {MAX_TAGS} tags.")
    return cleaned


def intro_id(doc_id: str) -> str:
    """The deterministic id of a contact's (at most one, ever) intro item."""
    return f"intro:{doc_id}"


def agent_id(doc_id: str, day: date) -> str:
    """The deterministic id of a contact's agent/human item for `day` -- at
    most one per contact per local day.
    """
    return f"agent:{doc_id}:{day:%Y%m%d}"


def enqueue(db, queue_id: str, item: dict, *, require_approval: bool, now: datetime) -> tuple[dict, bool]:
    """Create `queue_id` from `item`, or -- when it already exists -- change
    nothing and return the item already stored there.

    Raises `ValueError` for an `item` carrying a key outside the documented
    twelve, a `kind` outside `KINDS`, an empty `contact_doc_id`, a blank
    `text`, a `due_at` that is given and is not a timezone-aware `datetime`,
    or `tags` that are not a list of strings -- checked before any Firestore
    call, so a malformed `item` never reaches `.create()` even when
    `queue_id` happens to be free.

    Every item stores `tags`, `[]` when none were given: a campaign's
    messages are found by them (`list_items(tag=...)`, `tagged_sends`), and
    `messages_sync` copies them onto the message once it is sent. The
    caller cleans them first (`clean_tags`).

    Status is `pending` when `require_approval` is true or `kind == "reply"`
    (a reply is text a stranger has not seen yet; it always needs a human's
    yes), otherwise `approved` with `approved_by = "auto"`.

    Create-only via `ref.create(...)`: a second `enqueue` of the same
    `queue_id` -- the daily job re-running, a retried tool call -- writes
    nothing and returns `(existing_item, False)`, `existing_item` being
    whatever was stored by the call that actually won.
    """
    unknown_keys = set(item) - _ENQUEUE_KEYS
    if unknown_keys:
        raise ValueError(f"enqueue: unknown item key(s): {sorted(unknown_keys)}")
    if item.get("kind") not in KINDS:
        raise ValueError(f"enqueue: kind must be one of {KINDS}, got {item.get('kind')!r}")
    if not item.get("contact_doc_id"):
        raise ValueError("enqueue: contact_doc_id must not be empty")
    text = item.get("text")
    if not text or not text.strip():
        raise ValueError("enqueue: text must not be blank")
    due_at = item.get("due_at")
    if due_at is not None and (not isinstance(due_at, datetime) or due_at.tzinfo is None):
        raise ValueError("enqueue: `due_at` must be a timezone-aware datetime")
    tags = item.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("enqueue: `tags` must be a list of strings")

    status = PENDING if (require_approval or item["kind"] == "reply") else APPROVED
    data = dict(item)
    data["tags"] = list(tags)
    data["due_at"] = due_at or now
    data["created_by"] = item.get("created_by", "agent")
    data["status"] = status
    data["created_at"] = now
    if status == APPROVED:
        data["approved_by"] = "auto"
        data["approved_at"] = now

    from google.api_core import exceptions as api_exceptions

    ref = db.collection(QUEUE_COLLECTION).document(queue_id)
    try:
        ref.create(data)
    except api_exceptions.Conflict:
        existing = ref.get().to_dict()
        return {**existing, "id": queue_id}, False
    return {**data, "id": queue_id}, True


def get(db, queue_id: str) -> dict | None:
    """The item's fields plus `"id"`, or `None` when it does not exist."""
    snapshot = db.collection(QUEUE_COLLECTION).document(queue_id).get()
    if not snapshot.exists:
        return None
    return {**snapshot.to_dict(), "id": queue_id}


def next_due(db, now: datetime) -> dict | None:
    """The `approved` item with the earliest `due_at` at or before `now`,
    ties broken by `created_at` then id; `None` when nothing is due.

    ONE `where(status == approved)` query -- `due_at <= now` and the tie
    break are both applied in Python, since a second `where` clause would
    need a composite index this project does not create.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = db.collection(QUEUE_COLLECTION).where(filter=FieldFilter("status", "==", APPROVED))
    due = []
    for snapshot in query.stream():
        data = snapshot.to_dict()
        due_at = data.get("due_at")
        if due_at is not None and due_at <= now:
            due.append((due_at, data.get("created_at"), snapshot.id, data))
    if not due:
        return None
    due.sort(key=lambda row: (row[0], row[1], row[2]))
    due_at, _created_at, doc_id, data = due[0]
    return {**data, "id": doc_id}


def claim(db, queue_id: str, owner: str, now: datetime) -> dict | None:
    """`approved` -> `sending`: reserve the item for `owner` before any send
    is attempted. Returns the updated item, or `None` when it is missing or
    not `approved`.
    """
    from google.cloud import firestore

    ref = db.collection(QUEUE_COLLECTION).document(queue_id)

    @firestore.transactional
    def _claim(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        if data.get("status") != APPROVED:
            return None
        changes = {"status": SENDING, "sending_at": now, "lease_owner": owner}
        transaction.set(ref, changes, merge=True)
        return {**data, **changes, "id": queue_id}

    return _claim(db.transaction())


def release(db, queue_id: str, owner: str, now: datetime, reason: str) -> bool:
    """`sending` -> `approved`, only when `lease_owner == owner`: the send
    was provably not attempted or not accepted, so the item goes back to the
    front of the line. Clears `sending_at` and `lease_owner`, stores `error`.
    Returns whether it released.
    """
    from google.cloud import firestore

    ref = db.collection(QUEUE_COLLECTION).document(queue_id)

    @firestore.transactional
    def _release(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return False
        data = snapshot.to_dict()
        if data.get("status") != SENDING or data.get("lease_owner") != owner:
            return False
        transaction.set(
            ref, {"status": APPROVED, "sending_at": None, "lease_owner": None, "error": reason}, merge=True
        )
        return True

    return _release(db.transaction())


def settle(
    db,
    queue_id: str,
    status: str,
    *,
    now: datetime,
    message_id: str | None = None,
    chat_id: str | None = None,
    error: str | None = None,
) -> None:
    """`sending` -> `sent`/`failed`/`unknown`: record the outcome of a claimed
    send. ONE `db.batch()` commits the queue update, a `ledger.record(...,
    batch=batch)` row, and -- for a `sent`/`unknown` intro whose `analysis`
    document exists -- an `{"intro_sent_at": now}` merge, atomically.

    Raises `ValueError` for a `status` outside `sent`/`failed`/`unknown`.
    Raises `RuntimeError`, writing nothing, when `queue_id` does not exist or
    is not currently `sending` -- `settle` is only ever called right after
    the caller's own `claim`, so a wrong status here is a bug, not a race to
    recover from.
    """
    if status not in (SENT, FAILED, UNKNOWN):
        raise ValueError(f"settle: status must be sent, failed or unknown, got {status!r}")

    ref = db.collection(QUEUE_COLLECTION).document(queue_id)
    snapshot = ref.get()
    data = snapshot.to_dict()
    if data is None or data.get("status") != SENDING:
        raise RuntimeError(f"settle: {queue_id} is not a sending item")

    merge_intro = data.get("kind") == "intro" and status in (SENT, UNKNOWN)
    analysis_ref = db.collection(_ANALYSIS_COLLECTION).document(data["contact_doc_id"])
    analysis_exists = merge_intro and analysis_ref.get().exists

    changes = {"status": status, "settled_at": now, "message_id": message_id, "error": error}
    if status == SENT:
        changes["sent_at"] = now
    if chat_id is not None:
        changes["chat_id"] = chat_id

    batch = db.batch()
    batch.set(ref, changes, merge=True)
    ledger.record(db, "message", data["contact_doc_id"], status, now, queue_id, batch=batch)
    if analysis_exists:
        batch.set(analysis_ref, {"intro_sent_at": now}, merge=True)
    batch.commit()


def mark_skipped(db, queue_id: str, reason: str, now: datetime) -> bool:
    """`approved` -> `skipped`: a guard refused the item at send time, before
    a `claim` was even attempted. Returns whether it transitioned.
    """
    from google.cloud import firestore

    ref = db.collection(QUEUE_COLLECTION).document(queue_id)

    @firestore.transactional
    def _mark(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists or snapshot.to_dict().get("status") != APPROVED:
            return False
        transaction.set(ref, {"status": SKIPPED, "skip_reason": reason, "settled_at": now}, merge=True)
        return True

    return _mark(db.transaction())


def mark_unknown(db, queue_id: str, reason: str, now: datetime) -> bool:
    """`sending` -> `unknown`: the stale-claim sweep. The claim was never
    settled, so whether the send actually happened is genuinely unknown; the
    safe assumption is that it might have.

    In the SAME transaction as the status change: a ledger row
    (`result = "unknown"`, built with `ledger.entry(...)` since a transaction
    -- unlike a batch -- has no `ledger.record(..., transaction=...)` form),
    and, for an intro whose `analysis` document exists, an
    `{"intro_sent_at": now}` merge -- the send may have happened, and marking
    it sent-adjacent is the safe direction (never twice). Both reads (the
    queue item and, conditionally, `analysis`) happen before either write:
    the real client raises if a transactional `ref.get()` follows a write on
    the same transaction.
    """
    from google.cloud import firestore

    ref = db.collection(QUEUE_COLLECTION).document(queue_id)

    @firestore.transactional
    def _mark(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return False
        data = snapshot.to_dict()
        if data.get("status") != SENDING:
            return False

        analysis_ref = None
        analysis_exists = False
        if data.get("kind") == "intro":
            analysis_ref = db.collection(_ANALYSIS_COLLECTION).document(data["contact_doc_id"])
            analysis_exists = analysis_ref.get(transaction=transaction).exists

        transaction.set(ref, {"status": UNKNOWN, "error": reason, "settled_at": now}, merge=True)
        ledger_ref = db.collection(ledger.LEDGER_COLLECTION).document()
        transaction.set(ledger_ref, ledger.entry("message", data["contact_doc_id"], UNKNOWN, now, queue_id))
        if analysis_exists:
            transaction.set(analysis_ref, {"intro_sent_at": now}, merge=True)
        return True

    return _mark(db.transaction())


def resolve_unknown(
    db, queue_id: str, *, sent: bool, now: datetime, message_id: str | None = None, error: str | None = None
) -> bool:
    """`unknown` -> `sent` (`sent_at`, `message_id`) or `failed` (`error`):
    a sync later found the message (or did not). No ledger row -- the
    `mark_unknown` that put the item here already wrote one; this only
    resolves what it meant.
    """
    from google.cloud import firestore

    ref = db.collection(QUEUE_COLLECTION).document(queue_id)

    @firestore.transactional
    def _resolve(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists or snapshot.to_dict().get("status") != UNKNOWN:
            return False
        if sent:
            transaction.set(ref, {"status": SENT, "sent_at": now, "message_id": message_id}, merge=True)
        else:
            transaction.set(ref, {"status": FAILED, "error": error}, merge=True)
        return True

    return _resolve(db.transaction())


def approve(db, queue_id: str, now: datetime) -> bool:
    """`pending` -> `approved` with `approved_by = "human"`. Returns whether
    it transitioned.
    """
    from google.cloud import firestore

    ref = db.collection(QUEUE_COLLECTION).document(queue_id)

    @firestore.transactional
    def _approve(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists or snapshot.to_dict().get("status") != PENDING:
            return False
        transaction.set(ref, {"status": APPROVED, "approved_by": "human", "approved_at": now}, merge=True)
        return True

    return _approve(db.transaction())


def cancel(db, queue_id: str, reason: str, now: datetime, *, created_by: str | None = None) -> bool:
    """`pending`/`approved` -> `cancelled`. When `created_by` is given, only
    an item whose stored `created_by` equals it is cancelled -- so an agent
    cannot cancel an item a human queued, and vice versa. Returns whether it
    transitioned.
    """
    from google.cloud import firestore

    ref = db.collection(QUEUE_COLLECTION).document(queue_id)

    @firestore.transactional
    def _cancel(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return False
        data = snapshot.to_dict()
        if data.get("status") not in (PENDING, APPROVED):
            return False
        if created_by is not None and data.get("created_by") != created_by:
            return False
        transaction.set(ref, {"status": CANCELLED, "cancel_reason": reason, "settled_at": now}, merge=True)
        return True

    return _cancel(db.transaction())


def cancel_for_contact(db, contact_doc_id: str, reason: str, now: datetime) -> int:
    """Cancel every `pending`/`approved` item belonging to `contact_doc_id`
    (a contact opting out, an intro superseded) and return how many actually
    transitioned. Built on `items_for_contact` (one query) and `cancel` (one
    transaction per item) rather than its own transaction, so it is NOT
    atomic as a whole -- only each individual item's cancellation is.
    """
    count = 0
    for item in items_for_contact(db, contact_doc_id):
        if item["status"] in (PENDING, APPROVED) and cancel(db, item["id"], reason, now):
            count += 1
    return count


def tagged_sends(db, tags: list[str]) -> dict[str, datetime]:
    """Every contact sent a message carrying all of `tags` (already cleaned,
    at least one), with when the newest such message went: `sent` items
    only, by their `sent_at`. ONE `array_contains` on the first tag; the
    rest, and the status, are checked in Python.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    wanted = set(tags)
    query = db.collection(QUEUE_COLLECTION).where(filter=FieldFilter("tags", "array_contains", tags[0]))
    newest: dict[str, datetime] = {}
    for snapshot in query.stream():
        item = snapshot.to_dict() or {}
        sent_at, contact = item.get("sent_at"), item.get("contact_doc_id")
        if item.get("status") != SENT or sent_at is None or not contact or not wanted <= set(item.get("tags") or []):
            continue
        if contact not in newest or sent_at > newest[contact]:
            newest[contact] = sent_at
    return newest


def items_for_contact(db, contact_doc_id: str) -> list[dict]:
    """Every queue item belonging to `contact_doc_id`, newest `created_at`
    first. ONE equality `where`; the sort is done in Python.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = db.collection(QUEUE_COLLECTION).where(filter=FieldFilter("contact_doc_id", "==", contact_doc_id))
    items = [{**snapshot.to_dict(), "id": snapshot.id} for snapshot in query.stream()]
    items.sort(key=lambda item: item.get("created_at"), reverse=True)
    return items


def open_contact_ids(db) -> set[str]:
    """The `contact_doc_id` of every item currently in `OPEN` -- the set a
    caller must not queue a new item against. ONE `in` query over `OPEN`,
    projected to `contact_doc_id` with `select`.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = (
        db.collection(QUEUE_COLLECTION)
        .where(filter=FieldFilter("status", "in", list(OPEN)))
        .select(["contact_doc_id"])
    )
    return {snapshot.to_dict()["contact_doc_id"] for snapshot in query.stream()}


def list_items(db, status: str | None = None, limit: int = 25, *, tag: str | None = None) -> list[dict]:
    """Newest `created_at` first, capped at `limit` (clamped to 1..100).

    With `tag`: ONE `array_contains` on `tags`, `status` checked in Python.
    With `status` alone: ONE equality `where`, sorted in Python. Without
    either: ONE `order_by("created_at", direction="DESCENDING")` plus
    `limit`. All three are single-field, so Firestore's automatic indexes
    serve them.
    """
    limit = max(1, min(100, limit))
    if tag is not None or status is not None:
        from google.cloud.firestore_v1.base_query import FieldFilter

        if tag is not None:
            query = db.collection(QUEUE_COLLECTION).where(filter=FieldFilter("tags", "array_contains", tag))
        else:
            query = db.collection(QUEUE_COLLECTION).where(filter=FieldFilter("status", "==", status))
        items = [{**snapshot.to_dict(), "id": snapshot.id} for snapshot in query.stream()]
        if status is not None:
            items = [item for item in items if item.get("status") == status]
        items.sort(key=lambda item: item.get("created_at"), reverse=True)
        return items[:limit]

    query = db.collection(QUEUE_COLLECTION).order_by("created_at", direction="DESCENDING").limit(limit)
    return [{**snapshot.to_dict(), "id": snapshot.id} for snapshot in query.stream()]


def stale_sending(db, older_than: datetime) -> list[dict]:
    """Every `sending` item whose `sending_at` is before `older_than` -- a
    missing `sending_at` counts as stale too, so a document written outside
    the normal `claim` path (a manual fix, a bug elsewhere) is still swept
    rather than lingering forever. ONE equality `where(status == sending)`;
    the `sending_at` comparison is done in Python since Firestore serves only
    one `where` per query.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = db.collection(QUEUE_COLLECTION).where(filter=FieldFilter("status", "==", SENDING))
    stale = []
    for snapshot in query.stream():
        data = snapshot.to_dict()
        sending_at = data.get("sending_at")
        if sending_at is None or sending_at < older_than:
            stale.append({**data, "id": snapshot.id})
    return stale


def counts(db) -> dict[str, int]:
    """`{status: n}` for every status in `OPEN`, zeros included -- one
    equality query per status (`.select(["status"])`, matching
    `ledger.count_since`'s minimal projection), counted in Python.

    An empty `select([])` would be at least as cheap: the installed client
    rewrites an empty projection to the document name alone
    (`BaseQuery._normalize_projection` sends `__name__` as its one field),
    so the documents come back with no fields at all -- which is how
    `mcp_server._firestore_probe` uses it. Either keeps the payload to a
    few bytes a document; this uses the one-field projection.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    result = {}
    for status in _OPEN_ORDERED:
        query = db.collection(QUEUE_COLLECTION).where(filter=FieldFilter("status", "==", status)).select(["status"])
        result[status] = sum(1 for _ in query.stream())
    return result
