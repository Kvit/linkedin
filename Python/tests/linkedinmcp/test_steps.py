"""Tests for `linkedinmcp.steps`, the five process steps the MCP tools start
as jobs. Each is called directly with a `monitor.Job`, on a `FakeFirestore`
and the stub LinkedIn client; the monitor's own behaviour is
`test_monitor.py`'s.

A few tests per step, on what the design promises: a dry run spends nothing
(no profile view, no Gemini call, no write), the settings narrow a run, and
the day's intro cap holds across the daily job and a person's run.
"""

from datetime import UTC, datetime, timedelta

import pytest

import profiles
from linkedinmcp import clients, fetch_queue, jobs, monitor, queue, state, steps
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_unipile import (
    FakeUnipile,
    make_settings,
    profile,
    relation,
    seed_contact,
    seed_item,
    seed_message,
    write_template,
)

NOW = datetime(2026, 9, 11, 14, 0, 0, tzinfo=UTC)

TEMPLATE = "Thanks for connecting! I help labs recover denied claims with AI.\nHappy to stay in touch.\n"

LONG_SUMMARY = "Director of revenue cycle at a regional pathology group, running billing and denials."


def make_job(db, settings, step, params, client=None) -> monitor.Job:
    """A claimed job: its run document `running`, as `monitor.claim` leaves
    it, so the step's reports are accepted."""
    db.collection("runs").document(f"{step}:1").set({"job": step, "status": "running"})
    return monitor.Job(
        id=f"{step}:1", step=step, params=params, db=db, settings=settings,
        state=state.RuntimeState(db, clock=lambda: NOW), now=NOW, _client=client,
    )


def full_profile(slug: str):
    """A complete profile whose summary is well over `SUMMARY_MIN_LEN`."""
    return profile(
        slug,
        f"ACoAA-{slug}",
        first_name="Pat",
        last_name="Doe",
        headline="Director of Revenue Cycle at Coastal Pathology Associates",
        summary="I run billing, coding and denial management for a regional pathology group.",
        work_experience=[{"position": "Director of Revenue Cycle", "company": "Coastal Pathology", "start": "3/1/2019"}],
    )


@pytest.fixture
def db():
    return FakeFirestore(clock=lambda: NOW)


@pytest.fixture
def no_gemini(monkeypatch):
    """Fail the test if anything builds a Gemini client or classifies."""

    def refuse(*args, **kwargs):
        raise AssertionError("Gemini was called")

    monkeypatch.setattr(clients, "gemini_client", refuse)
    monkeypatch.setattr(profiles, "classify_profile", refuse)


# =============================================================================
# get_contacts
# =============================================================================


def test_a_dry_get_contacts_lists_the_new_connections_and_views_no_profile(db, tmp_path):
    db.collection("extracted").document("stored").set({"summary": LONG_SUMMARY})
    client = FakeUnipile(relations=[
        relation("fresh", "ACoAAFresh", NOW - timedelta(days=2), first="Fay", last="Resh"),
        relation("stored", "ACoAAStored", NOW - timedelta(days=1)),
        relation("old", "ACoAAOld", NOW - timedelta(days=40)),
    ])

    result = steps.get_contacts(make_job(
        db, make_settings(tmp_path), "get_contacts", {"days": 14, "max_profiles": 5, "dry_run": True}, client,
    ))

    assert result["new_connections"] == 1
    assert [(row["doc_id"], row["name"]) for row in result["connections"]] == [("fresh", "Fay Resh")]
    assert result["would_fetch"] == 1
    assert client.users.profile_calls == []
    assert list(db.collection("fetch_queue").stream()) == []


def test_get_contacts_stores_up_to_max_profiles_paced_and_leaves_them_unclassified(db, tmp_path, monkeypatch, no_gemini):
    sleeps: list[float] = []
    monkeypatch.setattr(steps, "_sleep", sleeps.append)
    slugs = ("ann", "bob", "cy")
    client = FakeUnipile(relations=[relation(slug, f"ACoAA-{slug}", NOW - timedelta(days=n + 1)) for n, slug in enumerate(slugs)])
    for slug in slugs:
        client.users.profiles[slug] = full_profile(slug)

    result = steps.get_contacts(make_job(
        db, make_settings(tmp_path), "get_contacts", {"days": 14, "max_profiles": 2, "dry_run": False}, client,
    ))

    assert result["fetch_enqueued"] == 3
    assert result["stored_slugs"] == ["ann", "bob"]
    assert [call[0] for call in client.users.profile_calls] == ["ann", "bob"]
    assert db.collection("extracted").document("ann").get().exists
    assert not db.collection("analysis").document("ann").get().exists
    assert result["fetch_queue"]["queued"] == 1
    assert len(sleeps) == 1 and steps.FETCH_GAP_MIN_SECONDS <= sleeps[0] <= steps.FETCH_GAP_MAX_SECONDS
    assert state.RuntimeState(db, clock=lambda: NOW).read().get("tick_lease_owner") is None


def test_get_contacts_fetches_only_the_connections_it_listed_so_the_dry_run_matches(db, tmp_path, monkeypatch, no_gemini):
    """`days` narrows what is fetched, not only what is queued: a 2018
    connection already waiting in the fetch queue is left for the tick, and
    one whose profile came back too short is not fetched again -- so the dry
    run's `would_fetch` is what the live run fetches."""
    monkeypatch.setattr(steps, "_sleep", lambda seconds: None)
    old = datetime(2018, 5, 1, tzinfo=UTC)
    fetch_queue.enqueue(db, "old-conn", provider_id="ACoAA-old-conn", name="Old Conn", connected_at=old, now=NOW)
    fetch_queue.enqueue(
        db, "was-short", provider_id="ACoAA-was-short", name="Was Short", connected_at=NOW - timedelta(days=3), now=NOW,
    )
    fetch_queue.mark(db, "was-short", fetch_queue.SHORT, NOW)
    client = FakeUnipile(relations=[
        relation("old-conn", "ACoAA-old-conn", old),
        relation("was-short", "ACoAA-was-short", NOW - timedelta(days=3)),
        relation("fresh", "ACoAA-fresh", NOW - timedelta(days=2)),
    ])
    for slug in ("old-conn", "was-short", "fresh"):
        client.users.profiles[slug] = full_profile(slug)
    settings = make_settings(tmp_path)
    params = {"days": 14, "max_profiles": 5}

    dry = steps.get_contacts(make_job(db, settings, "get_contacts", {**params, "dry_run": True}, client))
    live = steps.get_contacts(make_job(db, settings, "get_contacts", {**params, "dry_run": False}, client))

    assert (dry["new_connections"], dry["would_fetch"]) == (2, 1)
    assert [row["doc_id"] for row in live["fetched"]] == ["fresh"]
    assert [call[0] for call in client.users.profile_calls] == ["fresh"]
    assert fetch_queue.get(db, "old-conn")["status"] == fetch_queue.QUEUED


# =============================================================================
# classify_contacts
# =============================================================================


def test_classify_contacts_classifies_only_unclassified_recent_profiles(db, tmp_path, monkeypatch):
    for doc_id, days_ago in (("new", 1), ("done", 1), ("stale", 30)):
        db.collection("extracted").document(doc_id).set({
            "summary": LONG_SUMMARY, "fullName": f"{doc_id.title()} Doe", "created_at": NOW - timedelta(days=days_ago),
        })
    seed_contact(db, "done", industry="RCM", function="Finance", seniority="VP")
    summaries: list[str] = []
    monkeypatch.setattr(clients, "gemini_client", lambda: "gemini")
    monkeypatch.setattr(profiles, "classify_profile", lambda client, summary: summaries.append(summary) or (
        profiles.ProfileAnalysis(industry="Pathology", function="Operations", seniority="Director")
    ))
    settings = make_settings(tmp_path)

    dry = steps.classify_contacts(make_job(db, settings, "classify_contacts", {"days": 14, "dry_run": True}))
    assert (dry["would_classify"], [row["doc_id"] for row in dry["contacts"]], summaries) == (1, ["new"], [])

    result = steps.classify_contacts(make_job(db, settings, "classify_contacts", {"days": 14, "dry_run": False}))

    assert result["classified"] == 1
    assert result["contacts"] == [{
        "doc_id": "new", "name": "New Doe", "outcome": "classified",
        "industry": "Pathology", "function": "Operations", "seniority": "Director", "target": True,
    }]
    stored = db.collection("analysis").document("new").get().to_dict()
    assert (stored["industry"], stored["function"], stored["seniority"]) == ("Pathology", "Operations", "Director")
    assert db.collection("analysis").document("done").get().to_dict()["industry"] == "RCM"


# =============================================================================
# classify_stages
# =============================================================================


def test_a_dry_classify_stages_plans_the_new_replies_without_calling_gemini(db, tmp_path, no_gemini):
    seed_contact(db, "lee", industry="RCM", function="Finance", seniority="VP")
    seed_message(db, "m1", "lee", is_sender=1, timestamp=NOW - timedelta(days=3), text="Hi Lee")
    seed_message(db, "m2", "lee", is_sender=0, timestamp=NOW - timedelta(days=1), text="Tell me more")

    result = steps.classify_stages(make_job(db, make_settings(tmp_path), "classify_stages", {"dry_run": True}))

    assert result["would_classify"] == 1
    assert [row["doc_id"] for row in result["contacts"]] == ["lee"]
    assert db.collection("analysis").document("lee").get().to_dict().get("pipeline_stage") is None


# =============================================================================
# send_intro
# =============================================================================


def test_send_intro_queues_new_target_connections_within_the_days_and_the_days_cap(db, tmp_path):
    write_template(tmp_path, TEMPLATE)
    for slug in ("new1", "new2", "old"):
        seed_contact(db, slug, industry="RCM", seniority="Director")
    seed_contact(db, "lab", industry="Pathology", seniority="Staff")
    client = FakeUnipile(relations=[
        relation("new1", "ACoAANew1", NOW - timedelta(days=1)),
        relation("new2", "ACoAANew2", NOW - timedelta(days=2)),
        relation("old", "ACoAAOld", NOW - timedelta(days=40)),
        relation("lab", "ACoAALab", NOW - timedelta(days=1)),
    ])
    # Queued earlier today, by the daily job: one of the day's two.
    seed_item(db, "intro:earlier", "earlier", now=NOW - timedelta(hours=1), kind="intro", created_by="daily")
    settings = make_settings(tmp_path, intro_daily_cap=2)

    result = steps.send_intro(make_job(
        db, settings, "send_intro", {"days": 14, "industries": ["RCM"], "dry_run": False}, client,
    ))

    assert result["cap"] == {"per_day": 2, "used_today": 1, "remaining": 1}
    assert result["not_new"] == 1
    assert result["queued"] == 1
    assert [row["doc_id"] for row in result["intros"]] == ["new1"]
    item = queue.get(db, "intro:new1")
    assert (item["kind"], item["created_by"], item["text"]) == ("intro", "tool", TEMPLATE.strip())
    assert result["sender"] == {
        "last_tick_at": None, "sends_paused_until": None, "writes_blocked": False, "require_approval": False,
    }


def test_a_dry_send_intro_queues_nothing(db, tmp_path):
    write_template(tmp_path, TEMPLATE)
    seed_contact(db, "new1", industry="RCM", seniority="Director")
    client = FakeUnipile(relations=[relation("new1", "ACoAANew1", NOW - timedelta(days=1))])

    result = steps.send_intro(make_job(db, make_settings(tmp_path), "send_intro", {"dry_run": True}, client))

    assert (result["would_queue"], [row["doc_id"] for row in result["intros"]]) == (1, ["new1"])
    assert list(db.collection("outreach_queue").stream()) == []


def test_send_intro_says_when_writes_are_blocked(db, tmp_path):
    write_template(tmp_path, TEMPLATE)
    job = make_job(db, make_settings(tmp_path), "send_intro", {"dry_run": True}, FakeUnipile())
    job.state.block_writes("LinkedIn restricted the account")

    assert steps.send_intro(job)["sender"]["writes_blocked"] is True


# =============================================================================
# sync_messages
# =============================================================================


def test_sync_messages_classifies_new_replies_only_on_a_real_run(db, tmp_path, monkeypatch):
    calls: list[tuple] = []

    def fake_sync(db, client, now, *, dry_run, classify, beat):
        calls.append((dry_run, classify))
        beat("staging new replies")
        return {"written": 0}

    monkeypatch.setattr(jobs, "_sync", fake_sync)
    settings = make_settings(tmp_path)

    steps.sync_messages(make_job(db, settings, "sync_messages", {"dry_run": False}, FakeUnipile()))
    steps.sync_messages(make_job(db, settings, "sync_messages", {"dry_run": True}, FakeUnipile()))
    steps.sync_messages(make_job(db, settings, "sync_messages", {"classify": False, "dry_run": False}, FakeUnipile()))

    assert calls == [(False, jobs.default_classify), (True, None), (False, None)]
