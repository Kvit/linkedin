"""`webapp.compose`: the message box. Firestore is `FakeFirestore`, LinkedIn
is `FakeUnipile`, the outreach service a recorder in place of
`routine.call_tool`, and Gemini a fake in place of `compose.gemini`."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lib.unipile import errors as unipile_errors
from linkedinmcp import clients, clock, ledger, queue, state
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_unipile import FakeUnipile, chat, provider_id_of, seed_contact, seed_item, seed_message
from tests.webapp.conftest import outreach_settings, webapp_settings
from webapp import app as webapp_app, compose, projection, routine

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)  # 07:00 in Chicago
TOKEN = "form-token-0123456789"
TEXT = "Happy to show you how the denial recovery works."


@pytest.fixture
def db(monkeypatch):
    db = FakeFirestore()
    seed_contact(db, "ann", firstName="Ann", lastName="Lee", industry="Pathology", pipeline_stage="prospect", summary="Ann runs a lab.")
    seed_message(db, "m1", "ann", is_sender=1, timestamp=datetime(2026, 9, 1, 12, 0, tzinfo=UTC), text="Thanks for connecting")
    seed_message(db, "m2", "ann", is_sender=0, timestamp=datetime(2026, 9, 2, 12, 0, tzinfo=UTC), text="What does it cost?")
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


def _send(client, **fields):
    return client.post("/contacts/ann/send", data={"text": TEXT, "token": TOKEN, **fields}, follow_redirects=False)


def _manual_items(db):
    return [item for item in queue.items_for_contact(db, "ann") if item.get("kind") == queue.MANUAL]


def test_send_records_a_manual_message_and_starts_a_sync(client, db, linkedin, outreach):
    frame = client.app.state.contacts
    assert projection.contact_row(frame.frame, "ann")["needs_answer"] is True

    response = _send(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/contacts/ann?sent=07%3A00&sync=started"
    assert linkedin.messaging.attempts == [("send_message", "chat-1", TEXT)] and linkedin.closed
    [item] = _manual_items(db)
    assert (item["id"], item["status"], item["tags"], item["message_id"]) == (f"manual:ann:{TOKEN}", "sent", ["manual"], "sent-1")
    [row] = [doc.to_dict() for doc in db.collection(ledger.LEDGER_COLLECTION).stream()]
    assert (row["result"], row["queue_id"]) == ("sent", item["id"])
    stored = db.collection("analysis").document("ann").get().to_dict()
    assert (stored["sent_total"], stored["last_sent_date"], stored["summary"]) == (1, NOW, "Ann runs a lab.")
    assert projection.contact_row(frame.frame, "ann")["needs_answer"] is False
    assert outreach == [("sync_messages", {"classify": True, "dry_run": False})]

    page = client.get(response.headers["location"])
    assert "Sent at 07:00. Sync Messages started" in page.text
    assert "not stored by a sync yet" in page.text and TEXT in page.text

    again = _send(client)  # the same form, submitted again
    assert again.status_code == 200 and "This form was already sent" in again.text
    assert len(linkedin.messaging.attempts) == 1


@pytest.mark.parametrize(
    ("setup", "fields", "shown"),
    [
        (lambda db: seed_contact(db, "ann", firstName="Ann", handling="exclude"), {}, "Their handling is exclude"),
        (lambda db: None, {"text": "x" * 1201}, "the limit is 1200"),
        (lambda db: state.RuntimeState(db, lambda: NOW).pause_sends(NOW + timedelta(hours=1), "test"), {}, "Sends are paused until"),
        (lambda db: seed_item(db, "agent:ann:20260910", "ann", now=NOW, kind="reply"), {}, "Cancel it and send mine"),
    ],
    ids=["excluded", "too-long", "sends-paused", "open-item"],
)
def test_a_refused_send_keeps_the_text_and_writes_nothing(client, db, linkedin, outreach, setup, fields, shown):
    setup(db)
    response = _send(client, **fields)
    assert response.status_code == 200 and shown in response.text
    assert (fields.get("text") or TEXT) in response.text  # still in the box
    assert linkedin.messaging.attempts == [] and _manual_items(db) == [] and outreach == []


def test_cancel_it_and_send_mine(client, db, linkedin):
    seed_item(db, "agent:ann:20260910", "ann", now=NOW, kind="reply")
    linkedin.messaging.sent_24h = 50  # the day's budget is used: refused, and the queued message stays
    response = _send(client, cancel_item="agent:ann:20260910")
    assert response.status_code == 200 and "the daily limit is reached" in response.text
    assert queue.get(db, "agent:ann:20260910")["status"] == queue.PENDING

    linkedin.messaging.sent_24h = 0
    state.RuntimeState(db, lambda: NOW).store_budget_snapshot(0, NOW)
    response = _send(client, cancel_item="agent:ann:20260910")
    assert response.status_code == 303
    assert queue.get(db, "agent:ann:20260910")["status"] == queue.CANCELLED
    assert len(linkedin.messaging.attempts) == 1


def test_a_warning_needs_a_second_press(client, db, linkedin):
    seed_contact(db, "ann", firstName="Ann", pipeline_stage="soft_no")
    response = _send(client)
    assert response.status_code == 200 and "Their stage is soft_no." in response.text and "Send anyway" in response.text
    assert linkedin.messaging.attempts == []
    assert _send(client, confirm="1").status_code == 303
    assert len(linkedin.messaging.attempts) == 1


def test_a_send_linkedin_refuses_settles_failed_and_starts_no_sync(client, db, linkedin, outreach):
    linkedin.messaging.send_error = unipile_errors.NotFound(status=404, title="no such chat")
    response = _send(client)
    assert response.status_code == 200 and "Not sent: LinkedIn answered NotFound." in response.text
    [item] = _manual_items(db)
    assert (item["status"], item["error"]) == ("failed", "NotFound")
    assert outreach == []


def test_a_first_message_opens_a_chat_with_the_fetch_queue_provider_id(client, db, linkedin):
    seed_contact(db, "bob", firstName="Bob")
    db.collection("fetch_queue").document("bob").set({"provider_id": "ACoAABob"})
    response = client.post("/contacts/bob/send", data={"text": TEXT, "token": TOKEN}, follow_redirects=False)
    assert response.status_code == 303
    assert linkedin.messaging.attempts == [("start_chat", ("ACoAABob",), TEXT)]
    assert queue.get(db, f"manual:bob:{TOKEN}")["chat_id"] == "chat-new-1"


def test_expand_writes_the_message_from_the_note_and_the_conversation(client, monkeypatch):
    calls = []

    async def generate_content(*, model, contents, config):
        calls.append((model, contents, config))
        return SimpleNamespace(text=" Hi Ann, glad to walk you through it. ", model_version="gemini-3.1-pro-preview-01-2026",
                               candidates=[], usage_metadata=None)

    monkeypatch.setattr(compose, "gemini", lambda: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))))
    answer = client.post("/contacts/ann/expand", json={"text": "offer a demo next week"}).json()
    assert answer["ok"] and answer["text"] == "Hi Ann, glad to walk you through it."
    assert answer["model"] == "gemini-3.1-pro-preview-01-2026" and answer["problem"] is None
    [(model, contents, config)] = calls
    assert model == "gemini-pro-latest"
    assert "Ann runs a lab." in contents and "Them: What does it cost?" in contents
    assert contents.endswith("NOTE\noffer a demo next week")
    assert config.system_instruction.startswith("You write LinkedIn messages for Vitali. Recovr by Pinnacle Services")
    assert "stay in touch." in config.system_instruction and "polite" not in config.system_instruction
    assert '"jump on a call"' in config.system_instruction and "No sign-off" in config.system_instruction
    assert "one executive writing to another" in config.system_instruction and "No sales or marketing jargon" in config.system_instruction
    assert "At most 1200 characters" in config.system_instruction

    assert client.post("/contacts/ann/expand", json={"text": "  "}).json()["ok"] is False
