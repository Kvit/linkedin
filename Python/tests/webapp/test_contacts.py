"""`webapp.contacts`: the Contact screen, read-only, built on
`linkedinmcp.contacts.get_contact` and `get_conversation`. Firestore is
reached through `clients.firestore_client`, monkeypatched the way
`tests/linkedinmcp/test_app.py`'s `backend` fixture does it."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from linkedinmcp import clients
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_unipile import seed_contact, seed_item, seed_message
from tests.webapp.conftest import outreach_settings, webapp_settings
from webapp import app as webapp_app, projection

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(monkeypatch):
    db = FakeFirestore()
    seed_contact(
        db, "ann", firstName="Ann", lastName="Lee", industry="Pathology", pipeline_stage="lead",
        pipeline_reason="asked for pricing", summary="Ann runs a pathology lab.", email1="ann@example.com",
    )
    seed_message(db, "m1", "ann", is_sender=1, timestamp=datetime(2026, 9, 1, 12, 0, tzinfo=UTC), text="Thanks for connecting")
    seed_message(db, "m2", "ann", is_sender=0, timestamp=datetime(2026, 9, 2, 12, 0, tzinfo=UTC), text="What does it cost?")
    seed_item(db, "agent:ann:20260910", "ann", now=NOW, kind="reply", text="Happy to share pricing.")
    db.collection("fetch_queue").document("ann").set({"connected_at": datetime(2026, 8, 20, 15, 0, tzinfo=UTC)})
    db.collection("extracted").document("ann").set({"fullName": "Ann Lee", "miniProfile": {"headline": "Owner, Lee Pathology"}})
    monkeypatch.setattr(clients, "firestore_client", lambda: db)
    return db


@pytest.fixture
def client(db):
    contacts = projection.Contacts(lambda: db)
    contacts.rebuild()
    with TestClient(webapp_app.create_app(webapp_settings(), outreach=outreach_settings(), contacts=contacts)) as client:
        yield client


def test_contact_screen_shows_header_conversation_queue_and_summary(client):
    page = client.get("/contacts/ann")
    assert page.status_code == 200
    assert "Ann Lee" in page.text and "Pathology" in page.text and "asked for pricing" in page.text
    assert "Owner, Lee Pathology" in page.text  # the LinkedIn Helper headline the list shows
    assert "2026-08-20 10:00" in page.text  # connected, in America/Chicago
    assert "Me: Thanks for connecting" in page.text and "Them: What does it cost?" in page.text
    assert "Happy to share pricing." in page.text and "pending" in page.text
    assert "Ann runs a pathology lab." in page.text
    assert "ann@example.com" not in page.text


def test_a_contact_with_no_messages_says_so(client, db):
    seed_contact(db, "bob", firstName="Bob")
    page = client.get("/contacts/bob")
    assert page.status_code == 200
    assert "No messages." in page.text and "Nothing queued." in page.text


def test_an_unknown_contact_is_404(client):
    assert client.get("/contacts/nobody").status_code == 404


def test_the_list_links_each_name_to_its_contact_screen(client):
    assert 'href="/contacts/ann"' in client.get("/contacts").text
