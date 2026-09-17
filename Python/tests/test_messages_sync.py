"""The message-sync helpers must be importable, not just runnable.

The file was named `messages-sync.py`; a hyphen is not a valid module name, so
the outreach service could only have run it as a subprocess. These six names
are what the service imports.

The forward pass and its cursor are tested below against `FakeFirestore` and
the stub client, with no `messages_sync` function replaced.
"""

import inspect
from datetime import UTC, datetime, timedelta

import messages_sync
from lib.unipile.models import Message
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_unipile import FakeUnipile, seed_message


def test_helpers_are_importable():
    for name in (
        "_watermarks",
        "read_cursor",
        "_ids_since",
        "ContactResolver",
        "forward_pass",
        "refresh_contact_stats",
    ):
        assert hasattr(messages_sync, name), f"messages_sync.{name} is missing"


def test_main_takes_an_argv_list():
    """The shim calls `main()` with no arguments; it must still read sys.argv."""
    signature = inspect.signature(messages_sync.main)
    assert "argv" in signature.parameters
    assert signature.parameters["argv"].default is None


# =============================================================================
# the forward pass and the cursor
# =============================================================================


CURSOR = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)


def seeded(*stored: tuple[str, datetime]) -> FakeFirestore:
    """A store holding our opening message to Jane at `CURSOR`, which the
    resolver reads her identity from, plus each `(id, timestamp)` in
    `stored` as another message in her chat."""
    db = FakeFirestore()
    seed_message(
        db, "ours-1", "jane-roe", is_sender=1, timestamp=CURSOR, chat_id="chat-jane",
        contact_provider_id="ACoAAJane", contact_member_id="111",
    )
    for message_id, timestamp in stored:
        seed_message(db, message_id, "jane-roe", is_sender=0, timestamp=timestamp, chat_id="chat-jane")
    return db


def mailbox(*replies: tuple[str, datetime]) -> FakeUnipile:
    """LinkedIn's copy: the opening message at `CURSOR` and Jane's replies."""
    client = FakeUnipile()
    client.messaging.mailbox = [
        Message(id="ours-1", chat_id="chat-jane", is_sender=1, text="Thanks for connecting", timestamp=CURSOR),
        *(
            Message(id=message_id, chat_id="chat-jane", is_sender=0, sender_id="ACoAAJane", text="from LinkedIn",
                    timestamp=timestamp)
            for message_id, timestamp in replies
        ),
    ]
    return client


def forward_pass(db, client, cursor, *, dry_run=False) -> int:
    """The forward pass as its callers run it: skipping what is stored from
    the cursor on."""
    messages_ref = db.collection("messages")
    resolver = messages_sync.ContactResolver(client, messages_ref, db.collection("extracted"), join=True)
    skip_ids = messages_sync._ids_since(messages_ref, cursor)
    return messages_sync.forward_pass(client, db, messages_ref, resolver, cursor, skip_ids, dry_run=dry_run)


def test_the_forward_pass_stores_the_messages_after_the_cursor_and_moves_it_to_the_newest():
    db = seeded()
    client = mailbox(("reply-1", CURSOR + timedelta(hours=1)), ("reply-2", CURSOR + timedelta(hours=2)))

    written = forward_pass(db, client, CURSOR)

    assert written == 2
    assert db.collection("messages").document("reply-2").get().to_dict()["contact_doc_id"] == "jane-roe"
    assert messages_sync.read_cursor(db) == CURSOR + timedelta(hours=2)


def test_a_message_already_stored_after_the_cursor_is_not_rewritten_and_still_moves_it():
    """`reply-1` is stored but the cursor never reached it -- a run stopped
    between the two, or something else wrote it. It is skipped, not written
    again, and the cursor still moves past it."""
    db = seeded(("reply-1", CURSOR + timedelta(hours=1)))
    client = mailbox(("reply-1", CURSOR + timedelta(hours=1)))

    written = forward_pass(db, client, CURSOR)

    assert written == 0
    assert db.collection("messages").document("reply-1").get().to_dict()["text"] == "a stored message"
    assert messages_sync.read_cursor(db) == CURSOR + timedelta(hours=1)


def test_a_forward_pass_that_finds_nothing_new_still_records_the_cursor():
    """No cursor document yet and nothing new since the newest stored
    message: the run records the cursor anyway, rather than leaving the
    newest stored document to stand in for it until a message arrives."""
    db = seeded()

    written = forward_pass(db, mailbox(), CURSOR)

    assert written == 0
    assert messages_sync.read_cursor(db) == CURSOR


def test_a_dry_run_forward_pass_writes_no_cursor():
    db = seeded()
    client = mailbox(("reply-1", CURSOR + timedelta(hours=1)))

    written = forward_pass(db, client, CURSOR, dry_run=True)

    assert written == 1
    assert not db.collection("messages").document("reply-1").get().exists
    assert messages_sync.read_cursor(db) is None
