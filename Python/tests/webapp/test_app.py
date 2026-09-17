"""`webapp.app`: the factory, the open health route, the guarded screens,
the Home counts and the contacts table. `TestClient` in a `with` block
runs the lifespan."""

from datetime import UTC

from fastapi.testclient import TestClient
from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from linkedinmcp import clients
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_unipile import FakeUnipile, chat, provider_id_of, seed_message
from tests.webapp.conftest import AUDIENCE, ME, outreach_settings, webapp_settings
from webapp import app as webapp_app, projection


def _db():
    db = FakeFirestore()
    db.collection("analysis").document("ann").set({
        "firstName": "Ann", "lastName": "Lee", "industry": "Pathology", "pipeline_stage": "lead",
        "handling": "manual", "last_reply_date": DatetimeWithNanoseconds(2026, 9, 3, 12, 0, tzinfo=UTC),
    })
    db.collection("analysis").document("bob").set({"industry": "RCM"})
    db.collection("extracted").document("bob").set({"fullName": "Bob Ray", "occupation": "Billing Manager"})
    db.collection("fetch_queue").document("ann").set({"connected_at": DatetimeWithNanoseconds(2026, 8, 20, 15, 0, tzinfo=UTC)})
    return db


def _contacts():
    db = _db()
    return projection.Contacts(lambda: db)


def _app(contacts=None, **auth):
    if contacts is None:
        contacts = _contacts()
        contacts.rebuild()
    return webapp_app.create_app(webapp_settings(**auth), outreach=outreach_settings(), contacts=contacts)


def test_health_is_open_and_screens_are_not():
    with TestClient(_app(dev_user=None, audience=AUDIENCE)) as client:
        assert client.get("/health").json() == {"ok": True}
        assert client.get("/").status_code == 403
        assert client.get("/contacts").status_code == 403


def test_startup_builds_the_frame_when_none_was_given():
    contacts = _contacts()
    with TestClient(_app(contacts)) as client:
        assert contacts.frame is not None and contacts.frame.height == 2
        assert client.get("/health").status_code == 200


def test_home_shows_the_total_and_the_counts():
    with TestClient(_app()) as client:
        page = client.get("/")
    assert page.status_code == 200
    assert "2 contacts" in page.text
    assert "Pathology" in page.text and "RCM" in page.text
    assert "lead" in page.text and "manual" in page.text and "none" in page.text
    assert ME in page.text  # the signed-in user, in the header
    # Each count opens the Contacts list with that filter applied.
    assert 'href="/contacts?stage=lead"' in page.text and 'href="/contacts?stage=none"' in page.text
    assert 'href="/contacts?industry=RCM"' in page.text and 'href="/contacts?handling=manual"' in page.text


def test_contacts_screen_lists_sorts_and_searches():
    with TestClient(_app()) as client:
        page = client.get("/contacts")
        assert page.status_code == 200
        assert "Ann Lee" in page.text and "Bob Ray" in page.text and "Billing Manager" in page.text
        assert "2026-09-03 07:00" in page.text  # last reply, shown in America/Chicago
        assert "2026-08-20 10:00" in page.text  # connected
        head = page.text.split("<thead>")[1].split("</thead>")[0]
        assert "Headline" in head.split("<th")[-1]  # the last column
        assert page.text.index("Ann Lee") < page.text.index("Bob Ray")  # newest activity first
        page = client.get("/contacts", params={"sort": "industry", "dir": "desc"})
        assert page.text.index("Bob Ray") < page.text.index("Ann Lee")
        page = client.get("/contacts", params={"q": "billing"})
        assert "Bob Ray" in page.text and "Ann Lee" not in page.text
        assert "1 contact," in page.text


def test_contacts_screen_filters_and_its_links_keep_the_filters():
    with TestClient(_app()) as client:
        page = client.get("/contacts", params={"industry": "RCM"})
        assert "Bob Ray" in page.text and "Ann Lee" not in page.text
        assert 'name="industry" value="RCM" checked' in page.text
        assert "/contacts?industry=RCM&amp;sort=name&amp;dir=desc" in page.text  # a sort heading keeps it
        page = client.get("/contacts", params=[("industry", "RCM"), ("industry", "Pathology")])
        assert "Bob Ray" in page.text and "Ann Lee" in page.text
        assert '<span class="picked">RCM, Pathology</span>' in page.text
        assert "/contacts?industry=RCM&amp;industry=Pathology&amp;sort=name" in page.text  # both kept
        page = client.get("/contacts", params={"industry": "Nope"})  # no contact holds it: ignored
        assert "Ann Lee" in page.text and "Bob Ray" in page.text
        page = client.get("/contacts", params={"handling": "manual", "sent": "no"})
        assert "Ann Lee" in page.text and "Bob Ray" not in page.text
        assert '<option value="no" selected>No</option>' in page.text
        page = client.get("/contacts")
        assert '<option value="" selected>Any</option>' in page.text  # Message Sent and Received start at Any


def test_the_need_my_answer_button_lists_the_contacts_who_wrote_last():
    db = _db()
    db.collection("messages").document("m1").set({
        "contact_doc_id": "bob", "chat_id": "chat-bob", "is_sender": 0, "text": "Are you there?",
        "timestamp": DatetimeWithNanoseconds(2026, 9, 5, 12, 0, tzinfo=UTC),
    })
    contacts = projection.Contacts(lambda: db)
    contacts.rebuild()
    with TestClient(_app(contacts)) as client:
        home = client.get("/")
        assert 'href="/contacts?view=needs_answer&amp;sort=last_received_at&amp;dir=desc"' in home.text
        assert 'Need my answer <span class="count">1</span>' in home.text
        page = client.get("/contacts", params={"view": "needs_answer", "sort": "last_received_at", "dir": "desc"})
        assert "Bob Ray" in page.text and "Ann Lee" not in page.text
        assert "view=needs_answer" in page.text.split('class="list"')[1]  # sort headings keep the view
        assert '<input type="hidden" name="view" value="needs_answer">' in page.text


def test_my_stars_marks_the_starred_contacts_and_lists_them(monkeypatch):
    db = _db()
    db.collection("analysis").document("bob").set({"linkedin_starred": True}, merge=True)  # unstarred since
    for doc_id in ("ann", "bob"):
        seed_message(db, f"m-{doc_id}", doc_id, is_sender=1, timestamp=DatetimeWithNanoseconds(2026, 9, 1, tzinfo=UTC))
    linkedin = FakeUnipile(chats=[chat("chat-ann", provider_id_of("ann"), pinned=1), chat("chat-bob", provider_id_of("bob"))])
    monkeypatch.setattr(clients, "firestore_client", lambda: db)
    monkeypatch.setattr(clients, "unipile_client", lambda: linkedin)
    contacts = projection.Contacts(lambda: db)
    contacts.rebuild()
    with TestClient(_app(contacts)) as client:
        assert 'My Stars <span class="count">1</span>' in client.get("/").text
        response = client.post("/stars", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/contacts?view=stars&starred=1&added=1&removed=1&unmatched=0&at=")
        page = client.get(response.headers["location"])
    assert linkedin.closed
    assert db.collection("analysis").document("ann").get().to_dict()["linkedin_starred"] is True
    assert db.collection("analysis").document("bob").get().to_dict()["linkedin_starred"] is False
    assert "Ann Lee" in page.text and "Bob Ray" not in page.text  # the rows were patched, no rebuild
    assert "1 starred conversation in LinkedIn at" in page.text and "1 marked, 1 cleared." in page.text
    assert 'My Stars <span class="count">1</span>' in page.text


def test_refresh_rebuilds_and_returns_to_the_same_screen():
    app = _app()
    before = app.state.contacts.built_at
    with TestClient(app) as client:
        response = client.post(
            "/refresh", headers={"referer": "http://testserver/contacts?q=ann"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/contacts?q=ann"  # a path, never another origin
        assert app.state.contacts.built_at > before
        response = client.post("/refresh", follow_redirects=False)
        assert response.headers["location"] == "/"


def test_no_route_ends_in_z():
    """Cloud Run's front end answers some paths ending in `z` itself."""
    for route in _app().routes:
        assert not getattr(route, "path", "").rstrip("/").endswith("z")
