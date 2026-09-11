"""Read helpers behind the read-only MCP tools: contacts (`analysis` +
`extracted`), their conversations (`messages`), and the touch-due list --
shared here so `list_contacts`, `get_contact` and `mcp_server.py`'s tool
wrappers never build a contact's shape three slightly different ways.

Every returned dict is built as an explicit whitelist, never `{**data,
...}`: `analysis` carries `email1` and friends, `extracted` carries `email`
and `phoneNumbers`, and this module's whole job is to make sure neither ever
reaches a caller. `summary` -- up to 35 KB on `analysis` -- is truncated by
`get_contact` unless `full=True`, and `list_contacts` never asks Firestore
for it at all (its `.select([...])` never names it).

`db` is always an explicit parameter and `now` is always passed in rather
than read from the wall clock (contract §0). Every query is single-field --
one equality/range `where`, or one single-field `order_by` -- with a second
filter, the final sort, and the limit clamp all done in Python, the same
discipline `queue.py` and `decisions.py` follow.

`pipeline` (`get_conversation`'s `load_messages`/`build_transcripts`) is
imported inside the function that needs it, not at module level -- it alone
costs ~0.8 s to import (`google.genai`), the same reasoning `clients.py`'s
docstring gives for `gemini_client`. `functions.HANDLING_HOLDS` and
`google.cloud.firestore_v1.base_query.FieldFilter` are imported inside
`list_contacts` for the same "importing this module opens nothing" reason,
even though neither is actually heavy on its own.
"""

import itertools
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from linkedinmcp import queue

ANALYSIS_COLLECTION = "analysis"
EXTRACTED_COLLECTION = "extracted"

#: `list_contacts(tags=...)` reads the tagged contacts by id, this many per
#: `get_all` -- as `pipeline.load_contacts` pages `analysis`.
_TAGGED_PAGE = 250

#: What `list_contacts` ever asks Firestore for -- exactly the row's source
#: fields, and every field every Python-side filter (`industry`, `since`,
#: `needs_touch`) reads. Never `summary`, never `email*`/`phone*`. The same
#: list is used for every query branch, so every branch reads the identical
#: shape regardless of which filter went to the server.
_ROW_FIELDS = (
    "firstName", "lastName", "industry", "function", "seniority",
    "pipeline_stage", "pipeline_reason", "sent_total", "replied_total",
    "last_sent_date", "last_reply_date", "handling", "profileUrl",
)

_EXTRACTED_ROW_FIELDS = ("fullName", "occupation")


def _iso_local(value: datetime | None, tz: str) -> str | None:
    """`value` converted into `tz` and formatted as ISO 8601, or `None`.

    Every contact-facing datetime this module returns uses this -- the
    service's stated convention (`get_status`'s own docstring) that every
    date it reports is in `settings.tz`. `get_conversation`'s message dates
    are the one documented exception; see `_iso_utc`.
    """
    if value is None:
        return None
    return value.astimezone(ZoneInfo(tz)).isoformat()


def _iso_utc(value: datetime | None) -> str | None:
    """`value` as ISO 8601, unconverted. Stored datetimes are already
    timezone-aware UTC (contract §0), so this is just `.isoformat()` --
    used only by `get_conversation`, whose dates the brief states are UTC,
    unlike every contact-facing date `_iso_local` handles.
    """
    if value is None:
        return None
    return value.isoformat()


def _name(analysis: dict, extracted: dict) -> str | None:
    """`firstName` + `lastName` from `analysis`, joined and trimmed,
    whenever either is non-blank (so a document holding only one of the two
    still yields a usable name); otherwise `fullName` from `extracted`;
    otherwise `None`.
    """
    first = (analysis.get("firstName") or "").strip()
    last = (analysis.get("lastName") or "").strip()
    joined = " ".join(part for part in (first, last) if part)
    if joined:
        return joined
    return extracted.get("fullName") or None


def _row(doc_id: str, analysis: dict, extracted: dict, tz: str) -> dict:
    """The shape `list_contacts` returns per contact, and the base every
    `get_contact` row extends with a few more keys.
    """
    return {
        "doc_id": doc_id,
        "name": _name(analysis, extracted),
        "headline": extracted.get("occupation") or None,
        "industry": analysis.get("industry"),
        "function": analysis.get("function"),
        "seniority": analysis.get("seniority"),
        "stage": analysis.get("pipeline_stage"),
        "stage_reason": analysis.get("pipeline_reason"),
        "sent_total": analysis.get("sent_total"),
        "replied_total": analysis.get("replied_total"),
        "last_sent_date": _iso_local(analysis.get("last_sent_date"), tz),
        "last_reply_date": _iso_local(analysis.get("last_reply_date"), tz),
        "handling": analysis.get("handling"),
        "profile_url": analysis.get("profileUrl") or f"https://www.linkedin.com/in/{doc_id}",
    }


def _local_midnight(value, tz: str) -> datetime:
    """Local midnight of `value` -- a `date`, a `datetime` (its time
    component is dropped), or an ISO date string -- in the IANA zone `tz`.

    Raises `ValueError` for a string that is not a valid ISO date. Left to
    propagate: `list_contacts` is a plain helper, not an MCP tool, so
    contract §4's never-raise rule does not bind it -- the tool wrapper in
    `mcp_server.py` is what turns this into `{"ok": false, ...}`.
    """
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return datetime(value.year, value.month, value.day, tzinfo=ZoneInfo(tz))


def _activity_key(analysis: dict):
    """Sort key for "most recent activity": the later of `last_reply_date`
    and `last_sent_date`, newest first, a contact with neither last.

    `(has_date, date_or_None)`, the same null-safe tiebreak shape
    `fake_firestore.py`'s own `order_by` re-sort uses -- two contacts that
    both lack any date compare as equal tuples without ever needing to
    order `None` against `None`.
    """
    candidates = [d for d in (analysis.get("last_reply_date"), analysis.get("last_sent_date")) if d is not None]
    latest = max(candidates) if candidates else None
    return (latest is not None, latest)


def _needs_touch(doc_id: str, analysis: dict, *, now: datetime, settings, open_ids: set, holds) -> bool:
    """The full `needs_touch` predicate -- every one of the brief's six
    conditions, applied together whether or not `needs_touch` was the
    server-side filter (it only ever applies `pipeline_stage == "prospect"`,
    a subset of this).

    Every date comparison guards its `None` side first: a contact missing
    `last_sent_date` or `last_reply_date` must be excluded (or pass, for a
    missing `last_reply_date`), never raise `TypeError` from comparing
    `None`.
    """
    if analysis.get("pipeline_stage") != "prospect":
        return False
    last_sent = analysis.get("last_sent_date")
    if last_sent is None or last_sent > now - timedelta(days=settings.min_days_between_touches):
        return False
    last_reply = analysis.get("last_reply_date")
    if last_reply is not None and last_reply >= last_sent:
        return False
    if (analysis.get("sent_total") or 0) >= settings.max_touches:
        return False
    if str(analysis.get("handling") or "").strip().lower() in holds:
        return False
    if doc_id in open_ids:
        return False
    return True


def _replied_after(analysis: dict, sent_at: datetime) -> bool:
    """Whether the contact's newest reply came after `sent_at`."""
    last_reply = analysis.get("last_reply_date")
    return last_reply is not None and last_reply > sent_at


def list_contacts(
    db, settings, now, *, stage=None, industry=None, since=None, needs_touch=False, limit=25,
    tags=None, replied=None,
) -> list[dict]:
    """Contact rows, newest activity first, at most `limit` (clamped 1..100).

    ONE Firestore query, chosen by priority -- `stage`, then `industry`,
    then `needs_touch`, then `since` -- or, with none of them, two: the
    newest `limit` by `last_reply_date` and the newest `limit` by
    `last_sent_date`, merged, which between them hold the `limit` most
    recently active (the later of the two dates). Every other requested
    filter (and the full `needs_touch` predicate, even when it WAS the
    server-side filter) is applied in Python, and the page re-sorted by
    "most recent activity" before the limit is taken. See the module
    docstring for why every branch `.select()`s the identical field list.

    With `tags` (already cleaned) the contacts come from the queue instead:
    those sent a message carrying all of them (`queue.tagged_sends`), read
    by id in pages of 250, every other filter -- `stage` included -- applied
    in Python. Each row then adds `tagged`: when the newest such message
    went, and whether they replied after it; `replied` keeps those who did
    (`True`) or did not (`False`).

    `since` accepts a `date`, a `datetime` (time-of-day ignored) or an ISO
    date string, and means local midnight of that date in `settings.tz`. A
    contact with no `last_reply_date` never matches an explicit `since`.

    The page's `extracted` documents (for `name`/`headline`) are fetched
    with ONE `db.get_all(refs, field_paths=[...])` after filtering, sorting
    and limiting -- never one read per candidate, and never for a row that
    was going to be filtered or paged out anyway.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    clamped_limit = max(1, min(100, limit))
    since_bound = _local_midnight(since, settings.tz) if since is not None else None
    collection = db.collection(ANALYSIS_COLLECTION)
    fields = list(_ROW_FIELDS)
    tagged = queue.tagged_sends(db, tags) if tags else None

    if tagged is not None:
        queries = []
        primary = "tags"
    elif stage is not None:
        queries = [collection.where(filter=FieldFilter("pipeline_stage", "==", stage))]
        primary = "stage"
    elif industry is not None:
        queries = [collection.where(filter=FieldFilter("industry", "==", industry))]
        primary = "industry"
    elif needs_touch:
        queries = [collection.where(filter=FieldFilter("pipeline_stage", "==", "prospect"))]
        primary = "needs_touch"
    elif since_bound is not None:
        queries = [collection.where(filter=FieldFilter("last_reply_date", ">=", since_bound))]
        primary = "since"
    else:
        queries = [
            collection.order_by(field, direction="DESCENDING").limit(clamped_limit)
            for field in ("last_reply_date", "last_sent_date")
        ]
        primary = None

    if tagged is not None:
        rows = []
        for chunk in itertools.batched(sorted(tagged), _TAGGED_PAGE):
            snapshots = db.get_all([collection.document(doc_id) for doc_id in chunk], field_paths=fields)
            rows.extend((snapshot.id, snapshot.to_dict() or {}) for snapshot in snapshots if snapshot.exists)
        if replied is not None:
            rows = [(doc_id, data) for doc_id, data in rows if _replied_after(data, tagged[doc_id]) == replied]
    else:
        found = {snapshot.id: snapshot.to_dict() or {} for query in queries for snapshot in query.select(fields).stream()}
        rows = list(found.items())

    if stage is not None and primary != "stage":
        rows = [(doc_id, data) for doc_id, data in rows if data.get("pipeline_stage") == stage]
    if industry is not None and primary != "industry":
        rows = [(doc_id, data) for doc_id, data in rows if data.get("industry") == industry]
    if since_bound is not None and primary != "since":
        rows = [
            (doc_id, data)
            for doc_id, data in rows
            if data.get("last_reply_date") is not None and data["last_reply_date"] >= since_bound
        ]
    if needs_touch:
        import functions

        open_ids = queue.open_contact_ids(db)
        holds = functions.HANDLING_HOLDS
        rows = [
            (doc_id, data)
            for doc_id, data in rows
            if _needs_touch(doc_id, data, now=now, settings=settings, open_ids=open_ids, holds=holds)
        ]

    rows.sort(key=lambda pair: _activity_key(pair[1]), reverse=True)
    page = rows[:clamped_limit]

    extracted_by_id: dict[str, dict] = {}
    if page:
        refs = [db.collection(EXTRACTED_COLLECTION).document(doc_id) for doc_id, _data in page]
        for snapshot in db.get_all(refs, field_paths=list(_EXTRACTED_ROW_FIELDS)):
            if snapshot.exists:
                extracted_by_id[snapshot.id] = snapshot.to_dict() or {}

    result = [_row(doc_id, data, extracted_by_id.get(doc_id, {}), settings.tz) for doc_id, data in page]
    if tagged is not None:
        for row, (doc_id, data) in zip(result, page, strict=True):
            row["tagged"] = {
                "sent_at": _iso_local(tagged[doc_id], settings.tz),
                "replied": _replied_after(data, tagged[doc_id]),
            }
    return result


def get_contact(db, settings, doc_id: str, *, full: bool = False) -> dict | None:
    """One contact's full detail, or `None` when `analysis/{doc_id}` does
    not exist.

    The row shape (see `_row`) plus `pipeline_classified_at`,
    `intro_sent_at` (ISO strings in `settings.tz`), `summary` (truncated to
    4,000 characters unless `full=True`), `summary_truncated`, and `queue`
    -- the contact's queue items via `queue.items_for_contact` (already
    newest-`created_at`-first), narrowed to the documented seven keys and
    capped at 10.

    Reads the FULL `analysis` and `extracted` documents (no `.select()`),
    since `summary` and the two extra dates are not in `_ROW_FIELDS` -- so,
    unlike `list_contacts`, this function must build its result as an
    explicit whitelist rather than ever spreading the raw document: both
    documents carry `email*`/`phone*` fields this must never return.
    """
    snapshot = db.collection(ANALYSIS_COLLECTION).document(doc_id).get()
    if not snapshot.exists:
        return None
    analysis = snapshot.to_dict() or {}

    extracted_snapshot = db.collection(EXTRACTED_COLLECTION).document(doc_id).get()
    extracted = extracted_snapshot.to_dict() or {} if extracted_snapshot.exists else {}

    row = _row(doc_id, analysis, extracted, settings.tz)

    summary = analysis.get("summary") or ""
    truncated = not full and len(summary) > 4000
    if truncated:
        summary = summary[:4000]

    queue_rows = [
        {
            "id": item["id"],
            "kind": item.get("kind"),
            "status": item.get("status"),
            "due_at": _iso_local(item.get("due_at"), settings.tz),
            "created_by": item.get("created_by"),
            "text": item.get("text"),
            "tags": item.get("tags") or [],
        }
        for item in queue.items_for_contact(db, doc_id)[:10]
    ]

    row.update(
        {
            "pipeline_classified_at": _iso_local(analysis.get("pipeline_classified_at"), settings.tz),
            "intro_sent_at": _iso_local(analysis.get("intro_sent_at"), settings.tz),
            "summary": summary,
            "summary_truncated": truncated,
            "queue": queue_rows,
        }
    )
    return row


def get_conversation(db, doc_id: str, *, max_chars: int = 20000) -> dict | None:
    """The contact's whole message history as one transcript, or `None`
    when they have no readable message (`pipeline.build_transcripts`
    excludes undated messages, ones with a bad `is_sender`, events,
    deletions and blank text -- see its own docstring).

    `transcript` is `build_transcripts`' text, cut to its LAST `max_chars`
    characters (oldest-first text, so the tail is the newest messages) with
    `truncated = True` when that cut anything. `message_count` is the total
    stored `messages` documents for this contact -- the same set
    `pipeline.load_messages` loaded, not narrowed to only the ones that made
    it into the transcript, matching how `chat_ids` (the distinct, sorted
    `chat_id`s) is also read from that raw set rather than re-derived from
    the transcript text. `newest_inbound_date` is ISO UTC, unconverted --
    the one date in this module NOT shown in `settings.tz` (this function
    takes no `settings` at all).
    """
    import pipeline

    messages_ref = db.collection(pipeline.MESSAGES_COLLECTION)
    documents = pipeline.load_messages(messages_ref, [doc_id])
    transcripts = pipeline.build_transcripts(documents)
    entry = transcripts.get(doc_id)
    if entry is None:
        return None

    transcript = entry["transcript"]
    truncated = len(transcript) > max_chars
    if truncated:
        transcript = transcript[-max_chars:]

    bodies = [document.to_dict() or {} for document in documents]
    chat_ids = sorted({body["chat_id"] for body in bodies if body.get("chat_id")})

    return {
        "doc_id": doc_id,
        "transcript": transcript,
        "truncated": truncated,
        "message_count": len(documents),
        "inbound_total": entry["inbound_total"],
        "newest_inbound_date": _iso_utc(entry["newest_inbound_date"]),
        "chat_ids": chat_ids,
    }
