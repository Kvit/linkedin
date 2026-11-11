"""`decisions/{id}` -- the asynchronous inbox through which the agent asks a
human a question in one session and reads the answer in a later one, and
through which the service itself raises an alert a human has to acknowledge.

Two ways in: `ask` (an agent question -- auto id, since the same question can
legitimately be asked more than once) and `raise_alert` (a service alert --
the deterministic id `alert:{kind}:{key}`, create-only, so a condition that
keeps being true on every tick raises ONE alert, not one per tick). Two
transitions out: `answer` (`pending` -> `answered`) and `mark_applied`
(`answered` -> `applied`), both transactions -- the real
`google.cloud.firestore.transactional` decorator, read with
`ref.get(transaction=transaction)`, write with `transaction.set(...,
merge=True)`, exactly as `state.py` and `queue.py` use it -- that re-read the
current status and refuse an illegal move by returning `None`/`False` rather
than raising.

`PENDING`/`ANSWERED`/`APPLIED` are private-helper constants beyond what the
contract's module signature names explicitly (it lists only
`DECISIONS_COLLECTION`); they exist so this module never repeats the literal
strings `"pending"`/`"answered"`/`"applied"`, the same reasoning `queue.py`'s
status constants follow.
"""

from datetime import datetime

DECISIONS_COLLECTION = "decisions"

PENDING = "pending"
ANSWERED = "answered"
APPLIED = "applied"

_CONTEXT_VALUE_TYPES = (str, int, float, bool)


def ask(
    db,
    question: str,
    options: list[str],
    context: dict,
    now: datetime,
    *,
    asked_by: str = "agent",
    session_id: str | None = None,
) -> str:
    """Store a new agent question under an auto id and return that id.

    Raises `ValueError` for a blank `question`, an `options` that is not a
    list of strings, or a `context` that is not a dict whose values are all
    `str`/`int`/`float`/`bool`/`None` -- a flat dict, so it can be shown to a
    human (or another LLM) without recursing into it.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("ask: question must not be blank")
    if not isinstance(options, list) or not all(isinstance(option, str) for option in options):
        raise ValueError("ask: options must be a list of strings")
    if not isinstance(context, dict) or not all(
        value is None or isinstance(value, _CONTEXT_VALUE_TYPES) for value in context.values()
    ):
        raise ValueError("ask: context must be a flat dict of str/int/float/bool/None values")

    data = {
        "question": question,
        "options": options,
        "context": context,
        "status": PENDING,
        "answer": None,
        "asked_by": asked_by,
        "asked_at": now,
        "answered_at": None,
        "applied_at": None,
        "session_id": session_id,
    }
    ref = db.collection(DECISIONS_COLLECTION).document()
    ref.set(data)
    return ref.id


def raise_alert(db, kind: str, key: str, question: str, context: dict, now: datetime) -> bool:
    """Create-only alert at the deterministic id `alert:{kind}:{key}`, with
    `options = ["acknowledged"]` and `asked_by = "service"`. Returns whether
    it was created -- a repeating condition (the same `kind`/`key` raised
    again while the first alert is still unresolved) writes nothing and
    returns `False`, so a human sees ONE alert, not one per tick.

    Raises `ValueError` when `kind` or `key` contains `"/"` -- both are
    concatenated straight into the document id.
    """
    if "/" in kind or "/" in key:
        raise ValueError("raise_alert: kind and key must not contain '/'")

    data = {
        "question": question,
        "options": ["acknowledged"],
        "context": context,
        "status": PENDING,
        "answer": None,
        "asked_by": "service",
        "asked_at": now,
        "answered_at": None,
        "applied_at": None,
        "session_id": None,
    }
    ref = db.collection(DECISIONS_COLLECTION).document(f"alert:{kind}:{key}")

    from google.api_core import exceptions as api_exceptions

    try:
        ref.create(data)
    except api_exceptions.Conflict:
        return False
    return True


def get(db, decision_id: str) -> dict | None:
    """The decision's fields plus `"id"`, or `None` when it does not exist."""
    snapshot = db.collection(DECISIONS_COLLECTION).document(decision_id).get()
    if not snapshot.exists:
        return None
    return {**snapshot.to_dict(), "id": decision_id}


def answer(db, decision_id: str, answer: str, now: datetime) -> dict | None:
    """`pending` -> `answered`: store `answer` (non-blank; any text -- it
    need not be one of `options`) and `answered_at`. Returns the updated
    decision, or `None` when the id is missing, the decision is not
    `pending`, or `answer` is blank.

    The blank check runs before the transaction opens -- it needs no read of
    stored state, so there is nothing to gain from doing it inside one.
    """
    if not answer or not answer.strip():
        return None

    from google.cloud import firestore

    ref = db.collection(DECISIONS_COLLECTION).document(decision_id)

    @firestore.transactional
    def _answer(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        if data.get("status") != PENDING:
            return None
        changes = {"status": ANSWERED, "answer": answer, "answered_at": now}
        transaction.set(ref, changes, merge=True)
        return {**data, **changes, "id": decision_id}

    return _answer(db.transaction())


def mark_applied(db, decision_id: str, now: datetime) -> bool:
    """`answered` -> `applied`: the agent has acted on the answer. Returns
    whether it transitioned.
    """
    from google.cloud import firestore

    ref = db.collection(DECISIONS_COLLECTION).document(decision_id)

    @firestore.transactional
    def _mark(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists or snapshot.to_dict().get("status") != ANSWERED:
            return False
        transaction.set(ref, {"status": APPLIED, "applied_at": now}, merge=True)
        return True

    return _mark(db.transaction())


def list_decisions(db, status: str | None = None, limit: int = 25) -> list[dict]:
    """Newest `asked_at` first, capped at `limit` (clamped to 1..100) -- the
    same query rule as `queue.list_items`: an equality `where` sorted in
    Python when `status` is given, otherwise one single-field `order_by` plus
    `limit`.
    """
    limit = max(1, min(100, limit))
    if status is not None:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = db.collection(DECISIONS_COLLECTION).where(filter=FieldFilter("status", "==", status))
        items = [{**snapshot.to_dict(), "id": snapshot.id} for snapshot in query.stream()]
        items.sort(key=lambda item: item.get("asked_at"), reverse=True)
        return items[:limit]

    query = db.collection(DECISIONS_COLLECTION).order_by("asked_at", direction="DESCENDING").limit(limit)
    return [{**snapshot.to_dict(), "id": snapshot.id} for snapshot in query.stream()]


def counts(db) -> dict[str, int]:
    """`{"pending": n, "answered": n}`, zeros included -- `applied` is
    deliberately excluded, since an applied decision needs no more human or
    agent attention. One equality query per status (`.select(["status"])`,
    a one-field projection -- see `queue.counts` on what an empty
    `select([])` does), counted in Python.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    result = {}
    for status in (PENDING, ANSWERED):
        query = (
            db.collection(DECISIONS_COLLECTION).where(filter=FieldFilter("status", "==", status)).select(["status"])
        )
        result[status] = sum(1 for _ in query.stream())
    return result
