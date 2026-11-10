"""`linkedinmcp.documents`: the bucket listing, the link records and which
requests count as opens. Firestore is `FakeFirestore`, the bucket `FakeStorage`."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from linkedinmcp import documents
from linkedinmcp.settings import OutreachSettings
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_storage import FakeStorage

NOW = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
SETTINGS = OutreachSettings(api_key="test-key-not-a-real-secret", tz="America/Chicago", _env_file=None)


@pytest.fixture
def storage():
    fake = FakeStorage()
    fake.add("marketing/")  # the folder placeholder
    fake.add("marketing/Recovr_One_Pager.pdf", updated=datetime(2026, 9, 8, tzinfo=UTC))
    fake.add("marketing/CaseStudy.html", b"<html>", "text/html", updated=datetime(2026, 10, 14, tzinfo=UTC))
    fake.add("marketing/sub/deep.pdf")
    fake.add("other/x.pdf")
    return fake


def test_list_documents_lists_direct_children_newest_first(storage):
    rows = documents.list_documents(storage, SETTINGS)

    assert [row["name"] for row in rows] == ["CaseStudy.html", "Recovr_One_Pager.pdf"]
    assert rows[1] == {
        "name": "Recovr_One_Pager.pdf", "object": "marketing/Recovr_One_Pager.pdf", "type": "PDF",
        "size": 8, "updated_at": datetime(2026, 9, 8, tzinfo=UTC),
    }


def test_read_document_returns_content_type_and_size_and_downloads_in_one_bucket_call(storage, monkeypatch):
    assert documents.read_document(storage, SETTINGS, "marketing/CaseStudy.html", body=False) == (b"", "text/html", 6)

    monkeypatch.setattr(storage, "get_blob", lambda name: pytest.fail("a second bucket call"))
    assert documents.read_document(storage, SETTINGS, "marketing/CaseStudy.html") == (b"<html>", "text/html", 6)
    assert documents.read_document(storage, SETTINGS, "marketing/gone.pdf") is None


@pytest.mark.parametrize("token, usable", [
    ("recovr-one-pager.pdf", True), ("Ab3_-xyz0123456789012", True),
    ("", False), ("..", False), ("a/b", False), ("__x__", False), ("a b", False), ("a" * 201, False),
])
def test_usable_token(token, usable):
    assert documents.usable_token(token) is usable


def test_permanent_links_are_created_once_and_a_colliding_name_gets_a_suffix():
    db = FakeFirestore()
    names = ["Recovr_One_Pager.pdf", "A_b.pdf", "a-b.pdf"]

    first = documents.ensure_permanent_links(db, SETTINGS, names, NOW)

    assert first == {"Recovr_One_Pager.pdf": "recovr-one-pager.pdf", "A_b.pdf": "a-b.pdf", "a-b.pdf": "a-b-2.pdf"}
    assert documents.ensure_permanent_links(db, SETTINGS, names, NOW) == first
    assert len(list(db.collection("document_links").stream())) == 3
    assert documents.get_link(db, SETTINGS, "recovr-one-pager.pdf") == {
        "id": "recovr-one-pager.pdf", "name": "Recovr_One_Pager.pdf",
        "url": "https://info.paypredict.net/d/recovr-one-pager.pdf",
        "document": "marketing/Recovr_One_Pager.pdf", "kind": "permanent", "doc_id": None, "label": None,
        "created_by": "auto", "created_at": NOW, "opens": 0, "bot_requests": 0,
        "first_open_at": None, "last_open_at": None,
    }


def test_create_link_stores_a_custom_link_and_links_filters_by_name_and_contact():
    db = FakeFirestore()

    link = documents.create_link(db, SETTINGS, "Recovr_One_Pager.pdf", doc_id="ann", created_by="me@example.com", now=NOW)
    documents.create_link(
        db, SETTINGS, "CaseStudy.html", label="email signature", created_by="mcp", now=NOW + timedelta(minutes=1)
    )

    assert re.fullmatch(r"recovr-one-pager-[a-z0-9]{5}\.pdf", link["id"])  # the main link's id, a token before the extension
    assert link["url"] == f"https://info.paypredict.net/d/{link['id']}"
    assert (link["kind"], link["doc_id"], link["label"], link["created_by"]) == ("custom", "ann", None, "me@example.com")
    assert [row["id"] for row in documents.links(db, SETTINGS, doc_id="ann")] == [link["id"]]
    assert [row["label"] for row in documents.links(db, SETTINGS, name="CaseStudy.html")] == ["email signature", None]
    assert len(documents.links(db, SETTINGS)) == 4  # two sublinks, and the main link each one made sure of


def test_a_sublink_token_that_is_taken_is_drawn_again(monkeypatch):
    db = FakeFirestore()
    tokens = iter(["aaaaa", "aaaaa", "bbbbb"])
    monkeypatch.setattr(documents, "_token", lambda: next(tokens))

    first = documents.create_link(db, SETTINGS, "Recovr_One_Pager.pdf", label="one", created_by="mcp", now=NOW)
    second = documents.create_link(db, SETTINGS, "Recovr_One_Pager.pdf", label="two", created_by="mcp", now=NOW)

    assert (first["id"], second["id"]) == ("recovr-one-pager-aaaaa.pdf", "recovr-one-pager-bbbbb.pdf")
    assert documents.get_link(db, SETTINGS, first["id"])["label"] == "one"


def test_a_sublink_is_built_from_the_main_link_also_a_suffixed_or_a_cut_one():
    db = FakeFirestore()
    documents.ensure_permanent_links(db, SETTINGS, ["A_b.pdf", "a-b.pdf"], NOW)  # a-b.pdf gets the id a-b-2.pdf

    sublink = documents.create_link(db, SETTINGS, "a-b.pdf", label="x", created_by="mcp", now=NOW)
    assert re.fullmatch(r"a-b-2-[a-z0-9]{5}\.pdf", sublink["id"])

    long_name = "x" * 205 + ".pdf"
    assert documents.ensure_permanent_links(db, SETTINGS, [long_name], NOW) == {long_name: "x" * 176 + ".pdf"}
    sublink = documents.create_link(db, SETTINGS, long_name, label="y", created_by="mcp", now=NOW)
    assert re.fullmatch(r"x{176}-[a-z0-9]{5}\.pdf", sublink["id"]) and documents.usable_token(sublink["id"])


# --- which requests count -------------------------------------------------------

BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"


@pytest.mark.parametrize("method, user_agent, accept_language, kind", [
    ("GET", BROWSER, "en-US,en;q=0.9", "person"),
    ("HEAD", BROWSER, "en-US", "bot"),
    ("GET", "LinkedInBot/1.0 (compatible; Mozilla/5.0)", "en-US", "bot"),
    ("GET", "python-requests/2.32", "en", "bot"),
    ("GET", BROWSER, None, "bot"),
])
def test_classify_request(method, user_agent, accept_language, kind):
    assert documents.classify_request(method, user_agent, accept_language, SETTINGS.document_bot_agents) == kind


def test_record_request_counts_a_person_once_per_60_seconds_and_never_a_bot():
    db = FakeFirestore()
    link = documents.create_link(db, SETTINGS, "Recovr_One_Pager.pdf", label="x", created_by="mcp", now=NOW)

    def hit(seconds, **changes):
        request = {"method": "GET", "user_agent": BROWSER, "ip": "203.0.113.7", "accept_language": "en-US", **changes}
        return documents.record_request(
            db, SETTINGS, documents.get_link(db, SETTINGS, link["id"]), now=NOW + timedelta(seconds=seconds), **request
        )

    assert hit(0)["counted"] is True
    assert hit(30)["counted"] is False  # a reload
    assert hit(45, user_agent="LinkedInBot/1.0")["kind"] == "bot"
    assert hit(100)["counted"] is True
    stored = documents.get_link(db, SETTINGS, link["id"])
    assert (stored["opens"], stored["bot_requests"]) == (2, 1)
    assert (stored["first_open_at"], stored["last_open_at"]) == (NOW, NOW + timedelta(seconds=100))
    newest = documents.requests_for(db, SETTINGS, "Recovr_One_Pager.pdf")[link["id"]]
    assert [row["at"] for row in newest] == [NOW + timedelta(seconds=s) for s in (100, 45, 30, 0)]
    assert documents.totals(documents.links(db, SETTINGS)) == {
        "Recovr_One_Pager.pdf": {"opens": 2, "links": 1, "last_open_at": NOW + timedelta(seconds=100)}
    }


# --- the page a recipient sees for a link that does not work -------------------


@pytest.mark.parametrize("kind, title", [
    ("invalid", "This link is not valid"), ("gone", "This document is no longer available"),
])
def test_not_found_page_is_self_contained_and_in_the_documents_look(kind, title):
    page = documents.not_found_page(kind, 2026)

    assert f"<title>{title}</title>" in page and "$" not in page  # no placeholder left
    assert 'src="data:image/jpeg;base64,' in page and 'alt="Pinnacle Services"' in page
    assert "#E85C33" in page and "Montserrat" in page
    assert "&copy; 2026" in page and 'href="https://www.pinnacleservice.co"' in page
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    assert "storage.googleapis.com" not in page


def test_record_request_counts_another_reader_inside_the_window_and_cuts_what_it_stores():
    db = FakeFirestore()
    link = documents.create_link(db, SETTINGS, "Recovr_One_Pager.pdf", label="x", created_by="mcp", now=NOW)

    def hit(seconds, ip, accept_language="en-US"):
        return documents.record_request(
            db, SETTINGS, documents.get_link(db, SETTINGS, link["id"]), method="GET", user_agent=BROWSER,
            ip=ip, accept_language=accept_language, now=NOW + timedelta(seconds=seconds),
        )

    assert hit(0, "198.51.100.10")["counted"] is True
    assert hit(30, "203.0.113.77")["counted"] is True  # someone else, not a reload
    assert hit(40, "203.0.113.77")["counted"] is False  # the same reader again
    assert documents.get_link(db, SETTINGS, link["id"])["opens"] == 2

    row = hit(500, "x" * 4000, accept_language="y" * 12000)
    assert (len(row["ip"]), len(row["accept_language"])) == (64, 100)


def test_a_main_link_id_held_by_another_file_is_passed_over():
    db = FakeFirestore()
    other = documents._record("marketing/Other.pdf", documents.CUSTOM, doc_id=None, label="x", created_by="mcp", now=NOW)
    db.collection("document_links").document("a-b.pdf").set(other)  # not a main link, so only create() reports it

    assert documents.ensure_permanent_links(db, SETTINGS, ["A_b.pdf"], NOW) == {"A_b.pdf": "a-b-2.pdf"}
    assert documents.get_link(db, SETTINGS, "a-b.pdf")["document"] == "marketing/Other.pdf"
