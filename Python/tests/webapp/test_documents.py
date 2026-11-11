"""`webapp.documents`: the Documents tab. Firestore is `FakeFirestore`; the
bucket is the `storage` fixture every webapp test gets (`conftest.py`)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from linkedinmcp import clients, clock, documents
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.webapp.conftest import ME, outreach_settings, webapp_settings
from webapp import app as webapp_app, projection

NOW = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)  # 09:00 in Chicago
BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0"


@pytest.fixture
def db(monkeypatch, storage):
    db = FakeFirestore()
    storage.add("marketing/Recovr_One_Pager.pdf", b"x" * 157_239, updated=datetime(2026, 9, 8, 23, 23, tzinfo=UTC))
    storage.add("marketing/CaseStudy.html", b"<html>", "text/html", updated=datetime(2026, 7, 23, 2, 3, tzinfo=UTC))
    monkeypatch.setattr(clients, "firestore_client", lambda: db)
    monkeypatch.setattr(clock, "utcnow", lambda: NOW)
    return db


@pytest.fixture
def client(db):
    contacts = projection.Contacts(lambda: db)
    contacts.rebuild()
    with TestClient(webapp_app.create_app(webapp_settings(), outreach=outreach_settings(), contacts=contacts)) as client:
        yield client


def test_documents_lists_the_files_with_their_permanent_links(client, db):
    page = client.get("/documents")

    assert page.status_code == 200
    assert page.text.index("Recovr_One_Pager.pdf") < page.text.index("CaseStudy.html")  # newest first
    assert "154 KB" in page.text and "2026-09-08 18:23" in page.text
    assert "https://info.paypredict.net/d/recovr-one-pager.pdf" in page.text
    assert '<a href="/documents" aria-current="page">Documents</a>' in page.text
    assert len(list(db.collection("document_links").stream())) == 2


def test_a_file_added_to_or_removed_from_the_bucket_shows_on_the_next_load(client, db, storage):
    assert "ClaimRecoveryAgentOverview.pdf" not in client.get("/documents").text

    storage.add("marketing/ClaimRecoveryAgentOverview.pdf", updated=datetime(2026, 11, 9, tzinfo=UTC))
    del storage.files["marketing/CaseStudy.html"]
    page = client.get("/documents")

    assert "https://info.paypredict.net/d/claimrecoveryagentoverview.pdf" in page.text
    assert "CaseStudy.html" not in page.text
    assert client.get("/documents/ClaimRecoveryAgentOverview.pdf").status_code == 200


def test_a_label_link_is_created_and_its_opens_show_on_the_document_page(client, db):
    answer = client.post(
        "/documents/Recovr_One_Pager.pdf/links", data={"label": " email signature "}, follow_redirects=False
    )

    assert answer.status_code == 303 and answer.headers["location"] == "/documents/Recovr_One_Pager.pdf"
    (link,) = [row for row in documents.links(db, outreach_settings(), name="Recovr_One_Pager.pdf") if row["kind"] == "custom"]
    assert (link["label"], link["created_by"]) == ("email signature", ME)

    documents.record_request(
        db, outreach_settings(), link, method="GET", user_agent=BROWSER, ip="203.0.113.7", accept_language="en-US", now=NOW
    )
    page = client.get("/documents/Recovr_One_Pager.pdf")

    assert "email signature" in page.text and link["url"] in page.text
    assert "2026-11-09 09:00" in page.text and BROWSER in page.text


def test_an_unknown_document_is_404_and_an_empty_label_is_400(client):
    assert client.get("/documents/nope.pdf").status_code == 404
    assert client.post("/documents/Recovr_One_Pager.pdf/links", data={"label": "  "}).status_code == 400


def test_documents_says_so_when_the_bucket_cannot_be_listed(client, storage, monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError("no storage.objects.list")

    monkeypatch.setattr(storage, "list_blobs", fail)
    page = client.get("/documents")

    assert page.status_code == 200 and "could not be listed (PermissionError)" in page.text


def test_the_main_link_is_shown_once_with_its_counts_and_not_among_the_sublinks(client, db):
    client.get("/documents")
    link = documents.get_link(db, outreach_settings(), "recovr-one-pager.pdf")
    documents.record_request(
        db, outreach_settings(), link, method="GET", user_agent=BROWSER, ip="203.0.113.7", accept_language="en-US", now=NOW
    )

    page = client.get("/documents/Recovr_One_Pager.pdf")

    assert page.text.count("<code>https://info.paypredict.net/d/recovr-one-pager.pdf</code>") == 1
    assert "1 open," in page.text and "2026-11-09 09:00" in page.text and BROWSER in page.text
    assert "No sublinks yet." in page.text


def test_a_document_opens_in_a_new_tab_and_its_row_can_stay_marked(client):
    page = client.get("/documents").text

    assert '<a href="/documents/Recovr_One_Pager.pdf" target="_blank">Recovr_One_Pager.pdf</a>' in page
    assert '<tr data-row="Recovr_One_Pager.pdf">' in page and 'data-selected-key="documents:selected"' in page
    assert '<script src="/static/selected-row.js"></script>' in page
    assert client.get("/static/selected-row.js").status_code == 200


def test_sublinks_are_newest_first_and_sort_by_the_column_asked_for(client, db):
    settings = outreach_settings()
    documents.create_link(db, settings, "Recovr_One_Pager.pdf", label="Zenith Lab", created_by=ME, now=NOW - timedelta(days=2))
    opened = documents.create_link(
        db, settings, "Recovr_One_Pager.pdf", label="Acme Lab", created_by=ME, now=NOW - timedelta(days=3)
    )
    documents.create_link(db, settings, "Recovr_One_Pager.pdf", label="Mid Lab", created_by=ME, now=NOW - timedelta(days=1))
    documents.record_request(
        db, settings, opened, method="GET", user_agent=BROWSER, ip="203.0.113.7", accept_language="en-US", now=NOW
    )

    def order(query=""):
        page = client.get(f"/documents/Recovr_One_Pager.pdf{query}").text
        return sorted(("Acme Lab", "Mid Lab", "Zenith Lab"), key=page.index)

    assert order() == ["Mid Lab", "Zenith Lab", "Acme Lab"]  # created, newest first
    assert order("?sort=created_at&dir=asc") == ["Acme Lab", "Zenith Lab", "Mid Lab"]
    assert order("?sort=recipient&dir=asc") == ["Acme Lab", "Mid Lab", "Zenith Lab"]
    assert order("?sort=opens&dir=desc")[0] == "Acme Lab"
    assert order("?sort=last_open_at&dir=asc")[0] == "Acme Lab"  # never opened sorts last, both ways
    assert order("?sort=nonsense") == order()
    page = client.get("/documents/Recovr_One_Pager.pdf").text
    assert 'href="/documents/Recovr_One_Pager.pdf?sort=created_at&amp;dir=asc">Created ▾</a>' in page
    assert 'href="/documents/Recovr_One_Pager.pdf?sort=opens&amp;dir=desc">Opens</a>' in page


def test_the_documents_list_is_latest_on_top_and_sorts_by_the_column_asked_for(client):
    def first(query=""):
        page = client.get(f"/documents{query}").text
        return min(("CaseStudy.html", "Recovr_One_Pager.pdf"), key=lambda name: page.index(f'data-row="{name}"'))

    assert first() == "Recovr_One_Pager.pdf"  # updated 2026-09-08, after 2026-07-23
    assert first("?sort=updated_at&dir=asc") == "CaseStudy.html"
    assert first("?sort=name&dir=asc") == "CaseStudy.html"
    assert first("?sort=size&dir=desc") == "Recovr_One_Pager.pdf"
    assert first("?sort=nonsense&dir=asc") == first()
    page = client.get("/documents").text
    assert 'href="/documents?sort=updated_at&amp;dir=asc">Updated ▾</a>' in page
    assert 'href="/documents?sort=opens&amp;dir=desc">Opens</a>' in page
