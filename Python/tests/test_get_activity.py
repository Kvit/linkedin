"""`lib.get_activity.get_contact_activity`: crawl order, what a check stores, the daily allowance, stopping."""

from datetime import UTC, datetime, timedelta

import pytest

from lib import get_activity
from lib.unipile import errors as unipile_errors
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_unipile import (
    FakeUnipile,
    comment,
    post,
    profile,
    provider_id_of,
    reaction,
    relation,
    seed_contact,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
TARGETS = ["RCM", "Pathology"]


def _setup():
    """Audience: ann, bob (never checked), cat (checked 9 days ago), dan (2 days ago)."""
    db = FakeFirestore()
    seed_contact(db, "ann", industry="RCM", pipeline_stage="lead", last_reply_date=NOW - timedelta(days=1))
    seed_contact(db, "bob", industry="Pathology", pipeline_stage="prospect", last_sent_date=NOW - timedelta(days=3))
    seed_contact(db, "cat", industry="RCM")
    seed_contact(db, "dan", industry="RCM")
    seed_contact(db, "eve", industry="Hospital")  # not a target
    seed_contact(db, "fay", industry="RCM", handling="exclude")
    seed_contact(db, "gus", industry="RCM")  # not a connection
    checked = db.collection("activity")
    checked.document("cat").set({
        "updated_at": NOW - timedelta(days=9), "comments": [{"text": "old"}], "last_activity": NOW - timedelta(days=30),
    })
    checked.document("dan").set({"updated_at": NOW - timedelta(days=2)})
    client = FakeUnipile(relations=[
        relation(slug, provider_id_of(slug), None, first=slug.title()) for slug in ("ann", "bob", "cat", "dan", "eve", "fay")
    ])
    return db, client


def _doc(db, doc_id):
    snapshot = db.collection("activity").document(doc_id).get()
    return snapshot.to_dict() if snapshot.exists else None


def test_a_crawl_checks_never_checked_contacts_first_and_stores_recent_activity():
    db, client = _setup()
    ann, bob, cat = (provider_id_of(slug) for slug in ("ann", "bob", "cat"))
    client.users.posts[ann] = [post("p-new", NOW - timedelta(days=2), text="Recent post"), post("p-old", NOW - timedelta(days=20))]
    client.users.comments[ann] = [comment("c-new", NOW - timedelta(days=1), text="Recent comment"), comment("c-old", NOW - timedelta(days=15))]
    client.users.reactions[ann] = [reaction(f"r-{n}") for n in range(7)]
    client.users.posts[cat] = [post("p-cat", NOW - timedelta(days=40))]  # outside recency, still dates the activity
    db.collection("extracted").document("ann").set(
        {"occupation": "Biller", "currentPosition": None, "extra": {"locationName": "Austin", "summary": "About Ann"}}
    )
    db.collection("extracted").document("bob").set({  # LinkedIn Helper shape
        "miniProfile": {"headline": "Pathologist"}, "currentPosition": {"position": "Pathologist", "company": "Lab Co"},
        "extra": {"locationName": "Boise", "summary": None},
    })
    client.users.profiles[ann] = profile("ann", ann, headline="RCM Director", location="Austin", summary="About Ann",
                                         work_experience=[{"position": "Director", "company": "Acme"}])
    client.users.profiles[bob] = profile("bob", bob, headline="Pathologist", location="Boise", summary="New about",
                                         work_experience=[{"position": "Pathologist", "company": "Lab Co"}])
    client.users.profiles[cat] = profile("cat", cat, headline="Coder")

    found = get_activity.get_contact_activity(db, client, industries=TARGETS, limit=3, now=NOW)

    assert (found["audience"], found["never_checked"], found["checked"], found["stopped"]) == (4, 2, 3, None)
    assert found["profile_reads"] == 3
    assert [call[1] for call in client.users.activity_calls if call[0] == "iter_posts"] == [ann, bob, cat]
    assert all(call[3] == 1 for call in client.users.activity_calls)  # one request per read
    assert client.budget.throttle_calls == 9  # posts, comments, reactions for 3 contacts
    stored = _doc(db, "ann")
    assert stored["updated_at"] == NOW and stored["name"] == "Ann Doe"
    assert [p["text"] for p in stored["posts"]] == ["Recent post"]
    assert [c["text"] for c in stored["comments"]] == ["Recent comment"]
    assert len(stored["reactions"]) == 5
    assert stored["last_activity"] == NOW - timedelta(days=1)
    assert stored["profile_changes"] == [{"field": "headline", "before": "Biller", "after": "RCM Director"}]
    assert stored["unknown_before"] == ["position"]
    assert _doc(db, "bob")["profile_changes"] == [] and _doc(db, "bob")["unknown_before"] == ["about"]
    assert _doc(db, "bob")["last_activity"] is None
    assert _doc(db, "cat")["comments"] == []  # replaced
    assert _doc(db, "cat")["last_activity"] == NOW - timedelta(days=30)  # the stored date is later than the 40-day post
    assert _doc(db, "dan") == {"updated_at": NOW - timedelta(days=2)}
    assert [row["doc_id"] for row in found["contacts"]] == ["ann"]


def test_suggested_message_is_cleared_only_when_last_activity_advances():
    db, client = _setup()
    db.collection("activity").document("cat").update({"suggested_message": "Draft for cat"})
    db.collection("activity").document("dan").update(
        {"suggested_message": "Draft for dan", "last_activity": NOW - timedelta(days=1)}
    )
    client.users.posts[provider_id_of("cat")] = [post("p-cat", NOW - timedelta(days=2))]  # newer than 30 days
    client.users.posts[provider_id_of("dan")] = [post("p-dan", NOW - timedelta(days=5))]  # older than 1 day

    get_activity.get_contact_activity(db, client, type="posts", industries=TARGETS, now=NOW)

    assert _doc(db, "ann")["suggested_message"] is None  # first check: the field exists, empty
    assert _doc(db, "cat")["suggested_message"] is None
    assert _doc(db, "dan")["suggested_message"] == "Draft for dan"


def test_the_daily_allowance_counts_checks_in_the_last_24_hours():
    db, client = _setup()
    client.settings.max_activity_checks_per_day = 4
    db.collection("activity").document("dan").set({"updated_at": NOW - timedelta(hours=1)})

    found = get_activity.get_contact_activity(db, client, type="reactions", industries=TARGETS, now=NOW)

    assert (found["allowance"], found["checked"]) == (3, 3)  # ann, bob, then cat; dan counts against today
    assert _doc(db, "cat")["comments"] == [{"text": "old"}] and _doc(db, "cat")["reactions"] == []  # only reactions replaced
    again = get_activity.get_contact_activity(db, client, type="reactions", industries=TARGETS, now=NOW)
    assert (again["allowance"], again["checked"]) == (0, 0)


def test_a_failed_read_is_noted_and_a_rate_limit_stops_the_crawl():
    db, client = _setup()
    client.users.read_errors[("iter_comments", provider_id_of("ann"))] = unipile_errors.NotFound(status=404, title="gone")
    client.users.read_errors[("iter_posts", provider_id_of("bob"))] = unipile_errors.RateLimited(status=429, title="slow down")

    found = get_activity.get_contact_activity(db, client, type=["posts", "comments"], industries=TARGETS, now=NOW)

    assert found["checked"] == 1 and found["stopped"] == "RateLimited: slow down"
    assert _doc(db, "ann")["errors"] == ["comments: NotFound: gone"]
    assert _doc(db, "bob") is None


def test_bad_arguments_raise_before_any_request():
    db, client = _setup()
    for kwargs in ({"type": "likes"}, {"recency": "0d"}, {"recency": "ten"}, {"limit": 0}):
        with pytest.raises(ValueError):
            get_activity.get_contact_activity(db, client, industries=TARGETS, now=NOW, **kwargs)
    assert client.users.iter_relations_calls == 0
