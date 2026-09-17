"""LinkedIn's starred conversations, marked on the contacts in `analysis`.

LinkedIn stars a conversation, not a message, and Unipile lists it as the
chat's `pinned` field (1 for starred). `GET /api/v1/chats` has no filter for
it, no webhook event reports a change, and starring leaves the chat's
`timestamp` alone, so every call reads the whole chat list: 14 requests at
250 chats a page, 3 to 4 seconds for 3,350 chats (2026-09-17).

A starred chat's attendee is matched to a contact through the stored
`messages` (`contact_provider_id` to `contact_doc_id`). A chat with no
attendee id, or whose messages the sync has not stored, has no match; it is
counted in `unmatched` and nothing is written for it.

The mark is `analysis.linkedin_starred`: `True` on a contact with a starred
chat, `False` on a contact marked before whose chat is no longer starred
(unstarred in LinkedIn). Only the differences are written, merge-only, on
documents that exist. A failed chat read raises before any write, so a
partial list never removes a mark.
"""

import time

from google.cloud.firestore_v1.base_query import FieldFilter

from messages_sync import _IN_LIMIT, _chunks

ANALYSIS_COLLECTION = "analysis"
MESSAGES_COLLECTION = "messages"

#: The `analysis` field holding the mark.
FIELD = "linkedin_starred"

#: Chats a page. Unipile accepts 250 on `GET /api/v1/chats`.
PAGE_SIZE = 250

#: Writes a batch; Firestore takes 500.
_BATCH = 400


def _contacts_of(db, provider_ids: set[str]) -> dict[str, str]:
    """`contact_doc_id` by `contact_provider_id`, from the stored messages."""
    doc_ids: dict[str, str] = {}
    messages = db.collection(MESSAGES_COLLECTION)
    for chunk in _chunks(sorted(provider_ids), _IN_LIMIT):
        query = messages.where(filter=FieldFilter("contact_provider_id", "in", chunk)).select(
            ["contact_provider_id", "contact_doc_id"]
        )
        for snapshot in query.stream():
            message = snapshot.to_dict() or {}
            if message.get("contact_doc_id"):
                doc_ids.setdefault(message["contact_provider_id"], message["contact_doc_id"])
    return doc_ids


def get_stars(db, client) -> dict:
    """Read which chats are starred now and bring `analysis.linkedin_starred`
    in line. Returns `starred` (starred chats), `contacts` (contacts marked
    after this call), `unmatched` (starred chats with no contact in
    `analysis`), `added` and `removed` (the doc ids written `True` and
    `False`), and `seconds`."""
    started = time.monotonic()
    starred = [chat for chat in client.messaging.iter_chats(page_size=PAGE_SIZE) if chat.pinned == 1]
    by_provider = _contacts_of(db, {chat.attendee_provider_id for chat in starred if chat.attendee_provider_id})

    analysis = db.collection(ANALYSIS_COLLECTION)
    candidates = sorted(set(by_provider.values()))
    now_starred = (
        {snapshot.id for snapshot in db.get_all([analysis.document(d) for d in candidates], field_paths=[FIELD]) if snapshot.exists}
        if candidates else set()  # nothing starred: no batch get with no documents
    )
    marked = {
        snapshot.id for snapshot in analysis.where(filter=FieldFilter(FIELD, "==", True)).select([FIELD]).stream()
    }

    added = sorted(now_starred - marked)
    removed = sorted(marked - now_starred)
    writes = [(doc_id, True) for doc_id in added] + [(doc_id, False) for doc_id in removed]
    for chunk in _chunks(writes, _BATCH):
        batch = db.batch()
        for doc_id, value in chunk:
            batch.set(analysis.document(doc_id), {FIELD: value}, merge=True)
        batch.commit()

    unmatched = sum(1 for chat in starred if by_provider.get(chat.attendee_provider_id) not in now_starred)
    return {
        "starred": len(starred), "contacts": len(now_starred), "unmatched": unmatched,
        "added": added, "removed": removed, "seconds": round(time.monotonic() - started, 1),
    }
