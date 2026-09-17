"""`linkedinmcp.stars.get_stars`: starred chats marked on `analysis`, a
removed star unmarked, nothing created."""

from datetime import UTC, datetime

import pytest

from lib.unipile import errors as unipile_errors
from linkedinmcp import stars
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_unipile import FakeUnipile, chat, provider_id_of, seed_contact, seed_message

WHEN = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _db():
    db = FakeFirestore()
    seed_contact(db, "ann", firstName="Ann")
    seed_contact(db, "bob", firstName="Bob", linkedin_starred=True)  # unstarred in LinkedIn since
    seed_contact(db, "cat", firstName="Cat", linkedin_starred=True)  # still starred
    for doc_id in ("ann", "bob", "cat", "gone"):  # "gone" has messages and no `analysis` document
        seed_message(db, f"m-{doc_id}", doc_id, is_sender=1, timestamp=WHEN, chat_id=f"chat-{doc_id}")
    return db


def test_marks_starred_contacts_and_unmarks_a_removed_star():
    db = _db()
    client = FakeUnipile(chats=[
        chat("chat-ann", provider_id_of("ann"), pinned=1),
        chat("chat-bob", provider_id_of("bob")),
        chat("chat-cat", provider_id_of("cat"), pinned=1),
        chat("chat-gone", provider_id_of("gone"), pinned=1),
        chat("chat-stranger", "ACoStranger", pinned=1),
    ])

    found = stars.get_stars(db, client)

    assert found["starred"] == 4 and found["contacts"] == 2 and found["unmatched"] == 2
    assert found["added"] == ["ann"] and found["removed"] == ["bob"]
    analysis = db.collection("analysis")
    assert analysis.document("ann").get().to_dict() == {"firstName": "Ann", "linkedin_starred": True}
    assert analysis.document("bob").get().to_dict()["linkedin_starred"] is False
    assert analysis.document("cat").get().to_dict()["linkedin_starred"] is True
    assert not analysis.document("gone").get().exists  # never created

    again = stars.get_stars(db, client)
    assert again["added"] == [] and again["removed"] == []


def test_unstarring_the_last_conversation_clears_every_mark():
    db = _db()
    client = FakeUnipile(chats=[chat("chat-bob", provider_id_of("bob")), chat("chat-cat", provider_id_of("cat"))])

    found = stars.get_stars(db, client)

    assert found["starred"] == 0 and found["contacts"] == 0
    assert found["added"] == [] and found["removed"] == ["bob", "cat"]
    assert db.collection("analysis").document("cat").get().to_dict()["linkedin_starred"] is False


def test_a_failed_chat_read_writes_nothing():
    db = _db()
    client = FakeUnipile()
    client.messaging.read_errors["iter_chats"] = unipile_errors.NotFound(status=404, title="gone")
    with pytest.raises(unipile_errors.NotFound):
        stars.get_stars(db, client)
    assert db.collection("analysis").document("bob").get().to_dict()["linkedin_starred"] is True
