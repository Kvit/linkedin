"""`webapp.routine`: the Home buttons. The outreach service is a recorder in
place of `routine.call_tool`, answering each job id from `jobs`."""

import pytest
from fastapi.testclient import TestClient

from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.webapp.conftest import outreach_settings, webapp_settings
from webapp import app as webapp_app, projection, routine


class FakeOutreach:
    def __init__(self):
        self.calls = []
        self.jobs = {}
        self.starts = {}

    async def __call__(self, url, api_key, name, arguments):
        self.calls.append((name, arguments))
        if name == "get_job":
            job_id = arguments["job_id"]
            return self.jobs.get(job_id, {"ok": True, "job_id": job_id, "status": "queued", "lost": False, "result": None})
        return self.starts.get(name, {"ok": True, "job_id": f"{name}:1", "status": "queued"})

    def job(self, job_id, status, result=None, **extra):
        self.jobs[job_id] = {"ok": True, "job_id": job_id, "status": status, "lost": False, "result": result, **extra}

    def started(self):
        return [(name, arguments) for name, arguments in self.calls if name != "get_job"]


@pytest.fixture
def outreach(monkeypatch):
    fake = FakeOutreach()
    monkeypatch.setattr(routine, "call_tool", fake)
    return fake


@pytest.fixture
def client():
    db = FakeFirestore()
    db.collection("analysis").document("ann").set({"firstName": "Ann", "industry": "RCM"})
    contacts = projection.Contacts(lambda: db)
    contacts.rebuild()
    with TestClient(webapp_app.create_app(webapp_settings(), outreach=outreach_settings(), contacts=contacts)) as client:
        yield client


def test_sync_messages_runs_for_real_and_shows_its_counts(client, outreach):
    page = client.get("/")
    assert "Sync Messages" in page.text and "http-equiv" not in page.text

    response = client.post("/routine/sync-messages", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert outreach.started() == [("sync_messages", {"classify": True, "dry_run": False})]

    outreach.job("sync_messages:1", "running", progress={"done": 0, "total": 0, "note": "refreshing contact stats"})
    page = client.get("/")
    assert 'http-equiv="refresh"' in page.text and "disabled" in page.text
    assert "refreshing contact stats" in page.text
    client.post("/routine/new-contacts")
    assert len(outreach.started()) == 1  # one run at a time

    outreach.job("sync_messages:1", "succeeded", {"written": 3, "replied_contacts": 1, "cancelled": 0, "new_leads": 1})
    page = client.get("/")
    assert "messages stored</td><td>3" in page.text and "new leads</td><td>1" in page.text
    assert "http-equiv" not in page.text and "disabled" not in page.text and "Press Refresh" in page.text


def test_get_new_contacts_classifies_exactly_the_profiles_it_stored(client, outreach):
    client.post("/routine/new-contacts")
    assert outreach.started() == [("get_contacts", {"max_profiles": 10, "dry_run": False})]

    outreach.job("get_contacts:1", "succeeded", {"new_connections": 2, "stored_slugs": ["ann", "bob"], "connections": [{"doc_id": "zed"}]})
    client.get("/")
    assert outreach.started()[1] == ("classify_contacts", {"doc_ids": ["ann", "bob"], "max": 2, "dry_run": False})

    rows = [{"doc_id": "ann", "name": "Ann Lee", "outcome": "classified", "industry": "RCM", "target": True}]
    outreach.job("classify_contacts:1", "succeeded", {"classified": 1, "contacts": rows})
    page = client.get("/")
    assert '<a href="/contacts/ann">ann</a>' in page.text and "Ann Lee" in page.text and "<td>yes</td>" in page.text
    assert "zed" not in page.text  # the connection list is left out
    assert "Press Refresh" in page.text


def test_nothing_stored_starts_no_classification(client, outreach):
    client.post("/routine/new-contacts")
    outreach.job("get_contacts:1", "succeeded", {"new_connections": 0, "stored_slugs": []})
    page = client.get("/")
    assert [name for name, _ in outreach.started()] == ["get_contacts"]
    assert "nothing to classify" in page.text and "http-equiv" not in page.text


def test_a_refused_start_or_a_failed_job_is_shown_and_ends_the_run(client, outreach):
    outreach.starts["sync_messages"] = {"ok": False, "reason": "not_started", "job_id": "x", "error": "RetryError"}
    client.post("/routine/sync-messages")
    page = client.get("/")
    assert "was not started: not_started RetryError" in page.text and "disabled" not in page.text

    outreach.starts["sync_messages"] = {"ok": False, "reason": "already_running", "job_id": "sync_messages:0", "status": "running"}
    client.post("/routine/sync-messages")
    outreach.job("sync_messages:0", "failed", error="ResourceExhausted")
    page = client.get("/")
    assert "failed: ResourceExhausted" in page.text and "http-equiv" not in page.text
