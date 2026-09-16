"""`webapp.contacts`: the Contact screen, read-only, built on
`linkedinmcp.contacts.get_contact` and `get_conversation`. Firestore is
reached through `clients.firestore_client`, monkeypatched the way
`tests/linkedinmcp/test_app.py`'s `backend` fixture does it."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from linkedinmcp import clients, queue
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


def _post(client, doc_id, **form):
    return client.post(f"/contacts/{doc_id}/field", data=form, follow_redirects=False)


def _stored(db, doc_id="ann"):
    return db.collection("analysis").document(doc_id).get().to_dict()


def test_the_dropdowns_start_with_handling_and_show_the_stored_values(client):
    page = client.get("/contacts/ann").text
    order = [page.index(f'name="name" value="{name}"') for name in ("handling", "industry", "function", "seniority", "pipeline_stage")]
    assert order == sorted(order)
    assert '<option value="Pathology" selected>' in page and '<option value="lead" selected>' in page
    assert '<option value="" disabled selected>none</option>' in page  # function is unset


def test_a_classification_edit_writes_the_value_and_marks_it_set_by_hand(client, db):
    response = _post(client, "ann", name="industry", value="RCM")
    assert response.status_code == 303 and response.headers["location"] == "/contacts/ann?saved=industry"
    stored = _stored(db)
    assert (stored["industry"], stored["hand_set"]) == ("RCM", ["industry"]) and stored["hand_set_at"] is not None
    assert stored["email1"] == "ann@example.com"  # merged into the document, never replacing it
    assert projection.contact_row(client.app.state.contacts.frame, "ann")["industry"] == "RCM"  # no Refresh needed
    page = client.get(response.headers["location"]).text
    assert "Industry saved." in page and "set by hand" in page


def test_a_stage_edit_keys_on_the_newest_reply(client, db):
    _post(client, "ann", name="pipeline_stage", value="soft_no")
    stored = _stored(db)
    assert (stored["pipeline_stage"], stored["pipeline_reason"], stored["pipeline_message_id"]) == ("soft_no", "set by hand", "m2")
    assert stored["hand_set"] == ["pipeline_stage"] and stored["pipeline_classified_at"] is not None


def test_handling_exclude_cancels_the_open_queue_item_and_none_clears_it(client, db):
    response = _post(client, "ann", name="handling", value="exclude")
    assert response.headers["location"] == "/contacts/ann?saved=handling&cancelled=1"
    assert _stored(db)["handling"] == "exclude" and "hand_set" not in _stored(db)
    item = db.collection(queue.QUEUE_COLLECTION).document("agent:ann:20260910").get().to_dict()
    assert item["status"] == queue.CANCELLED
    assert "Queued messages cancelled: 1." in client.get(response.headers["location"]).text
    _post(client, "ann", name="handling", value="none")
    assert _stored(db)["handling"] is None


def test_a_value_outside_the_vocabulary_or_an_unknown_contact_writes_nothing(client, db):
    before = _stored(db)
    assert _post(client, "ann", name="industry", value="Bakery").status_code == 400
    assert _post(client, "ann", name="email1", value="x@example.com").status_code == 400
    assert _stored(db) == before
    assert _post(client, "nobody", name="industry", value="RCM").status_code == 404
    assert not db.collection("analysis").document("nobody").get().exists


def test_release_takes_only_that_field_out_of_hand_set(client, db):
    _post(client, "ann", name="industry", value="RCM")
    _post(client, "ann", name="seniority", value="Owner")
    response = client.post("/contacts/ann/release", data={"name": "industry"}, follow_redirects=False)
    assert response.headers["location"] == "/contacts/ann?released=industry"
    assert (_stored(db)["hand_set"], _stored(db)["industry"]) == (["seniority"], "RCM")
