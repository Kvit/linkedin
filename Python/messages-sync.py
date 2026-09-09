"""Sync LinkedIn messages into the Firestore `messages` collection.

Fetches only what Firestore does not already hold, and resolves each message to
a contact in `extracted` / `analysis` so the two can be analysed together.

    uv run python messages-sync.py                  # incremental, since 2024-01-01
    uv run python messages-sync.py --dry-run        # read everything, write nothing
    uv run python messages-sync.py --since 2023-01-01   # widen the history
    uv run python messages-sync.py --verify         # check for gaps

The invariant
-------------
`messages` always holds exactly the contiguous range [min_ts, max_ts], where
min_ts >= the floor set by --since. The forward pass extends the top, the
backward pass extends the bottom, and no write may create an interior hole.

Both watermarks are *derived* from the collection on every run rather than
stored alongside it, so there is no sync state to corrupt, lose or reset -- and
an interruption is repaired simply by running the script again.

The ordering rule that makes that true is in `plan_forward_writes`: the API
answers newest-first, and writing in that order is the one way this can lose
data permanently. See its docstring.

Known limits
------------
A message edited, deleted or marked seen *after* it was stored is never re-read,
so its stored copy keeps the state it had at sync time. Deleted messages remain
in the API stream flagged `deleted: 1` rather than vanishing, so one deleted
before we first saw it is captured correctly. `--rescan-days N` re-walks the
last N days to refresh those flags; the default of 0 is pure incremental.

An out-of-band write into `messages` -- a document added by hand or imported by
another tool -- can stretch [min_ts, max_ts] across an interior that was never
filled, and neither pass will go back for it. `--verify` is the check for that.

Attachments are stored as metadata only. Documents carrying any are marked
`attachments_fetched: False`, so a later pass can find its own backlog without
rescanning. Such a pass would need a GCS bucket and key convention, would have
to reissue each URL through GET /api/v1/messages/{id}/attachments/{id} because
the stored ones expire, and should skip `linkedin_post` entries -- those are
references to posts, not files.
"""

import argparse
import itertools
import logging
import os
import re
import sys
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from functions import (
    boundary_window,
    index_external_ids,
    member_id_from_urn,
    needs_backfill,
    plan_forward_writes,
)
from lib.unipile import RateLimited, UnipileClient, UnipileError

#: Firestore commits at most 500 operations per batch; the API pages 250 at a
#: time, so one page is one commit with room to spare.
PAGE_SIZE = 250

#: Above this many unresolved chats, listing every conversation costs fewer
#: requests than fetching them one at a time.
CHAT_FETCH_THRESHOLD = 25

#: A forward delta larger than this means a long quiet period or a bad
#: watermark. Worth saying out loud before a large write lands.
LARGE_DELTA_WARNING = 5000

_RESERVED_DOC_ID = re.compile(r"^__.*__$")


# --- Firestore plumbing -------------------------------------------------------


def _check_document_id(message_id: str) -> str:
    """Reject an id Firestore would mangle rather than store.

    `document(None)` mints a random id, which once caused the same contact to be
    stored afresh on every ingestion. Failing loudly is the only safe response.
    """
    if (
        not message_id
        or "/" in message_id
        or message_id in (".", "..")
        or _RESERVED_DOC_ID.match(message_id)
    ):
        raise ValueError(f"message id is not usable as a Firestore key: {message_id!r}")
    return message_id


def _watermarks(messages_ref) -> tuple[datetime | None, datetime | None]:
    """The oldest and newest timestamps currently stored.

    Two ordered queries, one document read each. Firestore indexes single fields
    automatically, so this needs no composite index.
    """
    newest = next(
        messages_ref.order_by("timestamp", direction=firestore.Query.DESCENDING)
        .limit(1)
        .stream(),
        None,
    )
    oldest = next(
        messages_ref.order_by("timestamp", direction=firestore.Query.ASCENDING)
        .limit(1)
        .stream(),
        None,
    )
    def stamp(doc):
        return (doc.to_dict() or {}).get("timestamp") if doc else None

    return stamp(oldest), stamp(newest)


def _ids_at(messages_ref, moment: datetime | None) -> set[str]:
    """Which documents sit exactly on a watermark.

    The one-millisecond overlap the bounds are widened by means these come back
    on every run. Excluding them by id is what lets an unchanged mailbox report
    an empty delta instead of re-resolving contacts for a message it already has.
    """
    if moment is None:
        return set()
    query = messages_ref.where(filter=FieldFilter("timestamp", "==", moment))
    return {doc.id for doc in query.select([]).stream()}


def _chunks[T](items: Iterable[T], size: int) -> Iterator[list[T]]:
    """Consume an iterator in fixed-size lists, lazily."""
    iterator = iter(items)
    while chunk := list(itertools.islice(iterator, size)):
        yield chunk


# --- contact resolution -------------------------------------------------------


class ContactResolver:
    """Maps a message's chat to a document in `extracted` / `analysis`.

    Three tables, each built only when something actually needs it:

    1. `chat_id -> attendee_provider_id`, because 80% of messages are outbound
       and their `sender_id` is us, not the contact.
    2. `provider_id -> member_id`, from the account-wide attendee list.
    3. `(key_type, external_id) -> document id`, from `extracted`.

    A steady-state run in which no new conversation appeared answers entirely
    from resolutions already stored on earlier messages, and touches none of them.
    """

    def __init__(self, client: UnipileClient, messages_ref, extracted_ref, *, join: bool):
        self._client = client
        self._messages_ref = messages_ref
        self._extracted_ref = extracted_ref
        self._join = join
        self._by_chat: dict[str, str] = {}
        self._member_by_provider: dict[str, str] = {}
        self._index: dict[tuple[str, str], str] | None = None
        self._chats_listed = False
        self._attendees_listed = False
        self.stats = {"reused": 0, "resolved": 0, "unjoined": 0}

    # -- table construction ----------------------------------------------------

    def prime_from_stored(self, chat_ids: set[str]) -> set[str]:
        """Reuse the contact already recorded against an earlier message.

        A conversation's contact never changes, so one small query per chat
        avoids rebuilding the 28k-document index for a delta that only touched
        conversations we have seen before. Returns the chats still unresolved.
        """
        unresolved: set[str] = set()
        for chat_id in chat_ids - self._by_chat.keys():
            # Equality on one field only: pairing it with an inequality on
            # `contact_provider_id` would demand a composite index, for a lookup
            # that is a convenience rather than a requirement.
            query = (
                self._messages_ref.where(filter=FieldFilter("chat_id", "==", chat_id))
                .select(["contact_provider_id", "contact_member_id"])
                .limit(5)
            )
            provider_id = member_id = None
            for stored in query.stream():
                body = stored.to_dict() or {}
                if body.get("contact_provider_id"):
                    provider_id = body["contact_provider_id"]
                    member_id = body.get("contact_member_id")
                    break

            if provider_id is None:
                unresolved.add(chat_id)
                continue

            self._by_chat[chat_id] = provider_id
            if member_id:
                self._member_by_provider[provider_id] = member_id
            self.stats["reused"] += 1
        return unresolved

    def load_chats(self, chat_ids: set[str]) -> None:
        """Fill `chat_id -> attendee_provider_id` for the chats given."""
        if not chat_ids or self._chats_listed:
            return

        if len(chat_ids) <= CHAT_FETCH_THRESHOLD:
            for chat_id in chat_ids:
                chat = self._client.messaging.get_chat(chat_id)
                if chat.attendee_provider_id:
                    self._by_chat[chat_id] = chat.attendee_provider_id
            return

        print(f"  listing all chats to resolve {len(chat_ids)} conversations...")
        for chat in self._client.messaging.iter_chats(page_size=PAGE_SIZE):
            if chat.attendee_provider_id:
                self._by_chat.setdefault(chat.id, chat.attendee_provider_id)
        self._chats_listed = True

    def load_attendees(self) -> None:
        if self._attendees_listed:
            return
        print("  loading attendees...")
        for attendee in self._client.messaging.iter_all_attendees(page_size=PAGE_SIZE):
            member_id = member_id_from_urn(attendee.member_urn)
            if attendee.provider_id and member_id:
                self._member_by_provider.setdefault(attendee.provider_id, member_id)
        self._attendees_listed = True

    def load_index(self) -> None:
        """Stream `extracted` and index both identity types.

        Built in Python rather than queried: see `index_external_ids`. Roughly
        28k document reads and ten seconds, which is why nothing calls this
        until a genuinely new conversation turns up.
        """
        if self._index is not None:
            return
        print("  indexing contacts from 'extracted'...")
        started = time.monotonic()
        self._index = index_external_ids(
            self._extracted_ref.select(["externalIds"]).stream()
        )
        print(f"    {len(self._index):,} keys in {time.monotonic() - started:.0f}s")

    def prepare(self, chat_ids: set[str]) -> None:
        """Build whatever the given chats need, and nothing more."""
        unresolved = self.prime_from_stored(chat_ids)
        if not unresolved:
            return
        self.load_chats(unresolved)
        if self._join:
            self.load_attendees()
            self.load_index()

    # -- lookup ----------------------------------------------------------------

    def resolve(self, message) -> tuple[str | None, str | None, str | None]:
        """`(provider_id, member_id, doc_id)` for the contact in this message.

        Falls back to `sender_id` only for a message we received: on one we sent,
        the sender is the account itself, and storing that as the contact would
        quietly corrupt every join downstream.
        """
        provider_id = self._by_chat.get(message.chat_id)
        if provider_id is None and message.is_sender == 0:
            provider_id = message.sender_id
        if provider_id is None:
            return None, None, None

        member_id = self._member_by_provider.get(provider_id)

        doc_id = None
        if self._index is not None:
            doc_id = self._index.get(("li-hash-id", provider_id))
            if doc_id is None and member_id:
                doc_id = self._index.get(("member-id", member_id))

        if doc_id:
            self.stats["resolved"] += 1
        else:
            self.stats["unjoined"] += 1
        return provider_id, member_id, doc_id


# --- writing ------------------------------------------------------------------


def _document_body(message, provider_id, member_id, doc_id) -> dict:
    """The Firestore document for one message.

    `model_dump()` and never `mode="json"`: JSON mode stringifies the timestamp,
    and Firestore sorts every string above every timestamp, so a single string
    value would become max_ts permanently and silently stop the forward pass.

    A null timestamp is omitted rather than written, for the mirror-image reason
    -- Firestore sorts nulls first, so an explicit one would become min_ts.
    """
    body = message.model_dump()
    if body.get("timestamp") is None:
        body.pop("timestamp", None)

    body["contact_provider_id"] = provider_id
    body["contact_member_id"] = member_id
    body["contact_doc_id"] = doc_id
    if message.attachments:
        body["attachments_fetched"] = False
    body["synced_at"] = firestore.SERVER_TIMESTAMP
    return body


def _commit(db, messages_ref, messages, resolver, *, dry_run: bool) -> int:
    """Write one chunk atomically.

    A batch rather than a bulk writer: `BulkWriter` parallelises and gives no
    ordering guarantee across batches, and ordered commits are the whole reason
    an interrupted run leaves a contiguous range rather than a hole.
    """
    if not messages:
        return 0

    batch = db.batch()
    for message in messages:
        provider_id, member_id, doc_id = resolver.resolve(message)
        batch.set(
            messages_ref.document(_check_document_id(message.id)),
            _document_body(message, provider_id, member_id, doc_id),
        )
    if not dry_run:
        batch.commit()
    return len(messages)


# --- the two passes -----------------------------------------------------------


def forward_pass(client, db, messages_ref, resolver, max_ts, skip_ids, *, dry_run):
    """Fetch messages newer than anything stored, and write them oldest-first.

    The whole delta is buffered before the first write, because it has to be
    reversed: the API answers newest-first, and writing in that order raises the
    high-water mark past messages that were never written.

    That is also why this pass takes no page cap, deliberately. Truncating a
    descending stream keeps the *newest* N, and writing those advances the high
    water mark straight over the remainder -- an interior hole neither pass would
    ever revisit. `--max-pages` therefore bounds the backfill only, where
    stopping early is safe because the walk moves the low water mark downward.
    """
    if max_ts is None:
        return 0

    stream = client.messaging.iter_all_messages(
        after=boundary_window(max_ts, "forward"), page_size=PAGE_SIZE
    )
    delta = [m for m in stream if m.id not in skip_ids]
    if not delta:
        return 0

    if len(delta) > LARGE_DELTA_WARNING:
        print(f"  [!] {len(delta):,} new messages -- unusually large for one run")

    resolver.prepare({m.chat_id for m in delta if m.chat_id})

    written = 0
    for chunk in _chunks(plan_forward_writes(delta), PAGE_SIZE):
        written += _commit(db, messages_ref, chunk, resolver, dry_run=dry_run)
    return written


def backward_pass(client, db, messages_ref, resolver, min_ts, floor, skip_ids, *,
                  dry_run, max_pages):
    """Extend the stored history downward, as far as the floor.

    Streamed rather than buffered. Walking descending moves min_ts down
    monotonically, so every intermediate state is already contiguous and an
    interruption costs at most one chunk.

    Bounding the window on both sides is what makes this terminate: once the
    backfill has reached the floor there is nothing between the bounds, so the
    pass costs a single request instead of paging to exhaustion.
    """
    if not needs_backfill(min_ts, floor):
        return 0

    stream = client.messaging.iter_all_messages(
        before=boundary_window(min_ts, "backward"),
        after=boundary_window(floor, "forward"),
        page_size=PAGE_SIZE,
    )

    written = 0
    for index, chunk in enumerate(_chunks(stream, PAGE_SIZE)):
        if max_pages and index >= max_pages:
            print()
            print(f"  stopped at --max-pages {max_pages}; re-run to continue")
            break
        fresh = [m for m in chunk if m.id not in skip_ids]
        if not fresh:
            continue
        resolver.prepare({m.chat_id for m in fresh if m.chat_id})
        written += _commit(db, messages_ref, fresh, resolver, dry_run=dry_run)
        print(f"  backfilled {written:,}...", end="\r")
    if written:
        print()
    return written


def rescan(client, db, messages_ref, resolver, days, *, dry_run):
    """Re-read a recent window so edits, deletions and read receipts refresh."""
    if not days:
        return 0
    since = datetime.now(UTC) - timedelta(days=days)
    stream = client.messaging.iter_all_messages(
        after=boundary_window(since, "forward"), page_size=PAGE_SIZE
    )
    refreshed = 0
    for chunk in _chunks(stream, PAGE_SIZE):
        resolver.prepare({m.chat_id for m in chunk if m.chat_id})
        # Ordered like the forward pass: a message arriving between the two
        # passes is new here, and writing it before an older sibling would leave
        # the same hole.
        refreshed += _commit(db, messages_ref, plan_forward_writes(chunk), resolver,
                             dry_run=dry_run)
    return refreshed


def verify(client, messages_ref, min_ts, max_ts) -> bool:
    """Compare what is stored against what the API holds in the same range.

    The one failure this design cannot prevent by construction is an out-of-band
    write stretching the range across an interior nobody filled. This finds it.
    """
    if min_ts is None or max_ts is None:
        print("verify: collection is empty; nothing to check.")
        return True

    stored = sum(1 for _ in messages_ref.select([]).stream())
    live = sum(
        1
        for _ in client.messaging.iter_all_messages(
            before=boundary_window(max_ts, "backward"),
            after=boundary_window(min_ts, "forward"),
            page_size=PAGE_SIZE,
        )
    )
    print(f"verify: stored {stored:,}, provider holds {live:,} in the same range")
    if stored == live:
        print("verify: OK, no gaps.")
        return True
    print(f"verify: [!] {live - stored:,} message(s) missing from the stored range.")
    return False


# --- entry point --------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Sync LinkedIn messages into the Firestore 'messages' collection.",
    )
    parser.add_argument("--since", default="2024-01-01",
                        help="oldest message to fetch, YYYY-MM-DD (default: 2024-01-01)")
    parser.add_argument("--dry-run", action="store_true",
                        help="read everything, write nothing")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="stop the backfill after this many pages; re-run to continue. "
                             "Does not apply to new messages, which are always fetched whole")
    parser.add_argument("--rescan-days", type=int, default=0,
                        help="also re-read the last N days to refresh edits and deletions")
    parser.add_argument("--no-contact-join", action="store_true",
                        help="skip resolving contacts; store the raw ids only")
    parser.add_argument("--verify", action="store_true",
                        help="check the stored range against the provider and exit")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    floor = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=UTC)

    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("lib.unipile").setLevel(logging.INFO)

    if os.path.isfile("vk-linkedin-master-service-account.json"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "vk-linkedin-master-service-account.json"

    db = firestore.Client(project="vk-linkedin", database="linkedin")
    messages_ref = db.collection("messages")
    extracted_ref = db.collection("extracted")

    client = UnipileClient.from_env()
    started = time.monotonic()

    try:
        min_ts, max_ts = _watermarks(messages_ref)

        if args.verify:
            return 0 if verify(client, messages_ref, min_ts, max_ts) else 1

        print("messages-sync")
        print(f"  floor:         {floor:%Y-%m-%d}")
        if max_ts is None:
            print("  stored before: nothing; this is a first run")
        else:
            print(f"  stored before: {min_ts:%Y-%m-%d %H:%M} .. {max_ts:%Y-%m-%d %H:%M}")
        if args.dry_run:
            print("  [!] dry run: nothing will be written")

        resolver = ContactResolver(
            client, messages_ref, extracted_ref, join=not args.no_contact_join
        )
        skip_ids = _ids_at(messages_ref, max_ts) | _ids_at(messages_ref, min_ts)

        new = forward_pass(client, db, messages_ref, resolver, max_ts, skip_ids,
                           dry_run=args.dry_run)
        print(f"  forward pass:  {new:,} new")

        old = backward_pass(client, db, messages_ref, resolver, min_ts, floor, skip_ids,
                            dry_run=args.dry_run, max_pages=args.max_pages)
        print(f"  backward pass: {old:,} older")

        if args.rescan_days:
            refreshed = rescan(client, db, messages_ref, resolver, args.rescan_days,
                               dry_run=args.dry_run)
            print(f"  rescan:        {refreshed:,} refreshed over {args.rescan_days}d")

        if new or old:
            stats = resolver.stats
            print(f"  contacts:      {stats['resolved']:,} joined, "
                  f"{stats['unjoined']:,} unmatched, {stats['reused']:,} reused")
        else:
            print("  up to date; no contact tables built")

        if not args.dry_run:
            min_ts, max_ts = _watermarks(messages_ref)
            if max_ts is not None:
                print(f"  stored after:  {min_ts:%Y-%m-%d %H:%M} .. {max_ts:%Y-%m-%d %H:%M}")
        print(f"  elapsed: {time.monotonic() - started:.1f}s")
        return 0

    except RateLimited as error:
        print(f"\n[!] rate limited by the provider: {error.title}")
        print("    Everything committed so far stands. Wait, then re-run.")
        return 1
    except UnipileError as error:
        print(f"\n[!] {error.type}: {error.title}")
        print("    Everything committed so far stands; re-run to continue.")
        return 1
    except KeyboardInterrupt:
        print("\n[!] interrupted. Committed pages stand; re-run to continue.")
        return 130
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
