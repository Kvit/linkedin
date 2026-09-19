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


def _post_id(when: datetime) -> str:
    """A LinkedIn post id created at `when` (the top 41 bits hold epoch ms)."""
    return str(int(when.timestamp() * 1000) << 22)


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
    client.users.reactions[ann] = [reaction(_post_id(NOW - timedelta(hours=12)))] + [reaction(f"r-{n}") for n in range(6)]
    for reacted in client.users.reactions[ann]:
        client.users.posts_by_id[reacted.post_id] = post(reacted.post_id, NOW - timedelta(days=4), text="Liked post")
    client.users.posts_by_id["post-of-c-new"] = post("post-of-c-new", NOW - timedelta(days=2), text="The post", author="Kim Lee")
    client.users.posts[cat] = [post("p-cat", NOW - timedelta(days=40))]  # outside recency, still dates the activity
    client.users.reactions[bob] = [reaction(_post_id(NOW - timedelta(days=20)))]  # outside recency: not kept or looked up
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
    assert client.budget.throttle_calls == 15  # 3 list reads for 3 contacts, then ann's 1 commented + 5 reacted posts
    assert client.budget.reconcile_calls == [{"profile": 0}, {"reaction": 0}]  # seeded from the day's checks and likes
    stored = _doc(db, "ann")
    assert stored["updated_at"] == NOW and stored["name"] == "Ann Doe"
    assert [p["text"] for p in stored["posts"]] == ["Recent post"]
    assert stored["posts"][0]["share_url"] == "https://li/p-new"  # tracking query dropped
    assert [c["text"] for c in stored["comments"]] == ["Recent comment"]
    assert stored["comments"][0]["post"] == {
        "author": "Kim Lee", "text": "The post", "url": "https://li/post-of-c-new", "date": NOW - timedelta(days=2),
    }
    assert len(stored["reactions"]) == 5
    assert stored["reactions"][0]["date"] == NOW - timedelta(hours=12)
    assert [r["post"]["text"] for r in stored["reactions"]] == ["Liked post"] * 5
    assert stored["last_activity"] == NOW - timedelta(hours=12)  # the reaction is newer than the comment
    assert stored["profile_changes"] == [{"field": "headline", "before": "Biller", "after": "RCM Director"}]
    assert stored["unknown_before"] == ["position"]
    assert _doc(db, "bob")["profile_changes"] == [] and _doc(db, "bob")["unknown_before"] == ["about"]
    assert _doc(db, "bob")["reactions"] == [] and _doc(db, "bob")["last_activity"] == NOW - timedelta(days=20)
    assert _doc(db, "cat")["comments"] == []  # replaced
    assert _doc(db, "cat")["last_activity"] == NOW - timedelta(days=30)  # the stored date is later than the 40-day post
    assert _doc(db, "dan") == {"updated_at": NOW - timedelta(days=2)}
    assert [row["doc_id"] for row in found["contacts"]] == ["ann"]


def test_the_newest_five_of_each_kind_are_kept_with_the_post_they_were_on():
    db, client = _setup()
    ann = provider_id_of("ann")
    client.users.posts[ann] = [post(f"p{n}", NOW - timedelta(days=n)) for n in range(1, 7)] + [
        post("rp", NOW - timedelta(days=30), reposted_at=NOW - timedelta(hours=2), author="Orig Co"),  # old post, new repost
    ]
    on = ["x1", "x1", "x2", "x3", "x4", "x5", "x6"]  # the two newest comments are on the same post
    client.users.comments[ann] = [comment(f"c{n}", NOW - timedelta(hours=n + 1), post_id=target) for n, target in enumerate(on)]
    client.users.reactions[ann] = [reaction(target) for target in ("x2", "y1", "y2", "y3", "y4", "y5")]
    for target in {*on, "y1", "y2", "y3", "y4", "y5"}:
        client.users.posts_by_id[target] = post(target, NOW - timedelta(days=3), text=f"Post {target}")

    get_activity.get_contact_activity(db, client, type=["posts", "comments", "reactions"], industries=TARGETS,
                                      limit=1, now=NOW)

    stored = _doc(db, "ann")
    assert len(stored["posts"]) == 5
    assert stored["posts"][0]["date"] == NOW - timedelta(hours=2) and stored["posts"][0]["author"] == "Orig Co"
    assert [c["post"]["text"] for c in stored["comments"]] == ["Post x1", "Post x1", "Post x2", "Post x3", "Post x4"]
    assert [r["post"]["text"] for r in stored["reactions"]] == ["Post x2", "Post y1", "Post y2", "Post y3", "Post y4"]
    assert client.users.post_calls == ["x1", "x2", "x3", "x4", "y1", "y2", "y3", "y4"]  # each post read once
    assert stored["last_activity"] == NOW - timedelta(hours=1)  # the newest comment


def test_doc_ids_recheck_exactly_those_contacts_in_order():
    db, client = _setup()

    found = get_activity.get_contact_activity(db, client, type="reactions", industries=TARGETS,
                                              doc_ids=["dan", "gus", "cat"], now=NOW)

    assert [call[1] for call in client.users.activity_calls] == [provider_id_of("dan"), provider_id_of("cat")]
    assert (found["checked"], found["not_in_audience"]) == (2, ["gus"])  # gus is not a connection


def test_the_newest_own_post_is_liked_once():
    db, client = _setup()
    ann, bob, cat = (provider_id_of(slug) for slug in ("ann", "bob", "cat"))
    client.users.posts[ann] = [
        post("rp", NOW - timedelta(days=9), reposted_at=NOW - timedelta(hours=1)),  # a repost: never liked
        post("own-new", NOW - timedelta(days=1)),
        post("own-old", NOW - timedelta(days=3)),
    ]
    client.users.posts[bob] = [post("liked", NOW - timedelta(days=1), user_reacted="LIKE")]  # already liked
    client.users.posts[cat] = [post("stale", NOW - timedelta(days=20))]  # outside recency

    found = get_activity.get_contact_activity(db, client, type="posts", industries=TARGETS, limit=3, now=NOW)

    assert client.users.liked == ["urn:li:activity:own-new"] and found["likes"] == 1
    stored = _doc(db, "ann")
    assert [p["liked_at"] for p in stored["posts"]] == [None, NOW, None] and stored["last_liked_at"] == NOW
    assert "last_liked_at" not in _doc(db, "bob")
    off = get_activity.get_contact_activity(db, client, type="posts", industries=TARGETS, doc_ids=["ann"], like=False,
                                            now=NOW)
    assert off["likes"] == 0 and client.users.liked == ["urn:li:activity:own-new"]


def test_likes_stop_at_the_daily_cap():
    db, client = _setup()
    client.budget.limits["reaction"] = 1
    db.collection("activity").document("dan").update({"last_liked_at": NOW - timedelta(hours=2)})  # today's one like
    client.users.posts[provider_id_of("ann")] = [post("own", NOW - timedelta(days=1))]

    found = get_activity.get_contact_activity(db, client, type="posts", industries=TARGETS, limit=1, now=NOW)

    assert (found["likes"], found["likes_skipped"], client.users.liked) == (0, "budget", [])
    assert client.budget.reconcile_calls[-1] == {"reaction": 1}


def test_each_contact_is_stamped_when_it_is_checked(monkeypatch):
    db, client = _setup()
    clock = iter([NOW, NOW + timedelta(minutes=3), NOW + timedelta(minutes=6)])
    monkeypatch.setattr(get_activity, "_utcnow", lambda: next(clock))

    get_activity.get_contact_activity(db, client, type="reactions", industries=TARGETS, limit=2)

    assert _doc(db, "ann")["updated_at"] == NOW + timedelta(minutes=3)
    assert _doc(db, "bob")["updated_at"] == NOW + timedelta(minutes=6)


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
