"""`webapp.suggested`: the Suggested screen. Firestore is `FakeFirestore`,
LinkedIn is `FakeUnipile`, the outreach service a recorder in place of
`routine.call_tool`, and Gemini a fake in place of `compose.gemini`."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from linkedinmcp import clients, clock, queue
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_unipile import FakeUnipile, chat, provider_id_of, seed_contact, seed_message
from tests.webapp.conftest import outreach_settings, webapp_settings
from webapp import app as webapp_app, compose, projection, routine

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)  # 07:00 in Chicago
POSTED = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
TOKEN = "form-token-0123456789"
DRAFT = "Hi Ann, your post on denial appeals named the payer I hear about most."


@pytest.fixture
def db(monkeypatch):
    db = FakeFirestore()
    seed_contact(db, "ann", firstName="Ann", lastName="Lee", industry="Pathology", pipeline_stage="prospect", summary="Ann runs a lab.")
    seed_message(db, "m1", "ann", is_sender=1, timestamp=datetime(2026, 9, 1, 12, 0, tzinfo=UTC), text="Thanks for connecting")
    db.collection("activity").document("ann").set({
        "doc_id": "ann", "name": "Ann Lee", "profile_url": "https://www.linkedin.com/in/ann",
        "updated_at": NOW - timedelta(hours=2), "last_activity": POSTED,
        "suggested_message": DRAFT, "suggested_message_updated_at": NOW - timedelta(hours=1),
        "posts": [{"date": POSTED, "text": "Denial appeals at our lab", "share_url": "https://www.linkedin.com/posts/ann-1",
                   "author": "Ann Lee", "is_repost": False}],
        "comments": [{"date": POSTED - timedelta(days=1), "text": "Agreed on prior auth", "post_id": "7",
                      "post": {"author": "Bob Ray", "text": "Prior auth is broken", "url": "https://www.linkedin.com/posts/bob-7"}}],
        "reactions": [{"date": None, "value": "PRAISE", "post_id": "8", "post": None}],
        "profile_changes": [], "errors": [],
    })
    db.collection("activity").document("old").set({
        "doc_id": "old", "name": "Old Draft", "last_activity": NOW - timedelta(days=40), "suggested_message": "Old one",
    })
    monkeypatch.setattr(clients, "firestore_client", lambda: db)
    monkeypatch.setattr(clock, "utcnow", lambda: NOW)
    return db


@pytest.fixture
def linkedin(monkeypatch):
    fake = FakeUnipile(chats=[chat("chat-1", provider_id_of("ann"))])
    monkeypatch.setattr(clients, "unipile_client", lambda: fake)
    return fake


@pytest.fixture
def outreach(monkeypatch):
    calls = []

    async def call_tool(url, api_key, name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "job_id": f"{name}:1", "status": "queued"}

    monkeypatch.setattr(routine, "call_tool", call_tool)
    return calls


@pytest.fixture
def client(db, linkedin, outreach):
    contacts = projection.Contacts(lambda: db)
    contacts.rebuild()
    with TestClient(webapp_app.create_app(webapp_settings(), outreach=outreach_settings(), contacts=contacts)) as client:
        yield client


def _stored(db) -> dict:
    return db.collection("activity").document("ann").get().to_dict()


def test_the_list_shows_the_drafts_of_recent_activity(client):
    page = client.get("/suggested").text
    assert 'href="/suggested/ann" target="_blank"' in page and "prospect" in page and DRAFT[:40] in page
    assert "1 draft," in page and "Old Draft" not in page
    assert "Old Draft" in client.get("/suggested?days=60").text


def test_the_page_shows_the_draft_and_what_they_did_newest_first(client):
    page = client.get("/suggested/ann").text
    assert f'data-saved="{DRAFT}"' in page and "https://www.linkedin.com/posts/ann-1" in page
    assert page.index("Denial appeals at our lab") < page.index("Agreed on prior auth") < page.index("Reacted praise")
    assert "Prior auth is broken" in page and "Bob Ray" in page and "could not be read" in page
    assert page.index('id="panel-activity"') < page.index('id="panel-conversation" aria-labelledby="tab-conversation" hidden')
    assert page.index('id="panel-conversation"') < page.index("Thanks for connecting")  # the thread, in its tab
    assert client.get("/suggested/nobody").status_code == 404


def test_save_stores_the_draft_and_clear_deletes_it(client, db):
    response = client.post("/suggested/ann/save", data={"text": "  Hi Ann, shorter.  "}, follow_redirects=False)
    assert response.headers["location"] == "/suggested/ann?saved=07%3A00"
    assert (_stored(db)["suggested_message"], _stored(db)["suggested_message_updated_at"]) == ("Hi Ann, shorter.", NOW)
    assert "Saved at 07:00." in client.get(response.headers["location"]).text

    refused = client.post("/suggested/ann/save", data={"text": "x" * 1201})
    assert refused.status_code == 200 and "Not saved: 1201 characters, the limit is 1200." in refused.text
    assert "press Clear to delete the draft" in client.post("/suggested/ann/save", data={"text": "  "}).text
    assert _stored(db)["suggested_message"] == "Hi Ann, shorter."

    response = client.post("/suggested/ann/clear", follow_redirects=False)
    assert response.headers["location"] == "/suggested?cleared=ann"
    assert _stored(db)["suggested_message"] is None and "suggested_message_sent_at" not in _stored(db)
    assert "Draft for Ann Lee cleared." in client.get(response.headers["location"]).text


def test_rework_rewrites_the_draft_with_the_activity_as_context(client, monkeypatch):
    calls = []

    async def generate_content(*, model, contents, config):
        calls.append((contents, config))
        return SimpleNamespace(text=" Hi Ann, about your appeals post. ", model_version="gemini-3.1-pro-preview-01-2026",
                               candidates=[], usage_metadata=None)

    monkeypatch.setattr(compose, "gemini", lambda: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))))
    answer = client.post("/suggested/ann/rework", json={"text": DRAFT, "instructions": "shorter"}).json()
    assert answer["ok"] and answer["text"] == "Hi Ann, about your appeals post."
    [(contents, config)] = calls
    assert "Ann runs a lab." in contents and "Me: Thanks for connecting" in contents
    assert "2026-09-08 Posted: Denial appeals at our lab" in contents
    assert "Commented: Agreed on prior auth -- on a post by Bob Ray: Prior auth is broken" in contents
    assert contents.endswith(f"DRAFT\n{DRAFT}\n\nINSTRUCTIONS\nshorter")
    assert config.system_instruction.startswith("You revise LinkedIn messages for Vitali. Recovr by Pinnacle Services")
    assert config.system_instruction.endswith(compose.STYLE) and "At most 1200 characters" in config.system_instruction
    assert client.post("/suggested/ann/rework", json={"text": " "}).json()["ok"] is False


def test_send_tags_the_message_and_records_the_send_on_the_activity_record(client, db, linkedin, outreach):
    response = client.post("/suggested/ann/send", data={"text": DRAFT, "token": TOKEN}, follow_redirects=False)
    assert response.headers["location"] == "/suggested?sent=07%3A00&to=ann&sync=started"
    assert linkedin.messaging.attempts == [("send_message", "chat-1", DRAFT)]
    [item] = [item for item in queue.items_for_contact(db, "ann") if item.get("kind") == queue.MANUAL]
    assert (item["status"], item["tags"]) == ("sent", ["manual", "activity"])
    stored = _stored(db)
    assert (stored["suggested_message"], stored["suggested_message_updated_at"], stored["suggested_message_sent_at"]) == (None, NOW, NOW)
    assert outreach == [("sync_messages", {"classify": True, "dry_run": False})]
    page = client.get(response.headers["location"]).text
    assert "Sent to Ann Lee at 07:00. Sync Messages started" in page and "0 drafts," in page


def test_a_refused_send_keeps_the_text_and_records_nothing(client, db, linkedin, outreach):
    seed_contact(db, "ann", firstName="Ann", handling="exclude")
    response = client.post("/suggested/ann/send", data={"text": "Edited text", "token": TOKEN})
    assert response.status_code == 200 and "Their handling is exclude" in response.text and "Edited text" in response.text
    assert _stored(db)["suggested_message"] == DRAFT and "suggested_message_sent_at" not in _stored(db)
    assert linkedin.messaging.attempts == [] and outreach == []
