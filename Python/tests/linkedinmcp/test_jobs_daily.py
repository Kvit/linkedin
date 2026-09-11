"""Tests for `linkedinmcp.jobs.daily` (plan the day: queue intros for new
first-degree connections in a target industry) and `jobs.sweep_stale` (the
stale-claim sweep `daily` and `tick` share).

Candidates come from the REAL `functions.select_intro_candidates`, fed the
stub client's relations and chats and the `analysis` documents seeded here,
so the tests exercise the actual selection rules -- newest connection first,
off-target and existing-chat skips -- rather than a re-statement of them.
`messages_sync.refresh_contact_stats` is replaced on its module by a recorder
(the `stats` fixture); its own behaviour is `test_jobs_sync.py`'s end-to-end
test's business.
"""

import random
from datetime import UTC, datetime, timedelta

import pytest

import messages_sync
from lib.unipile import errors as unipile_errors
from linkedinmcp import decisions, fetch_queue, jobs, queue, state
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_unipile import (
    FakeUnipile,
    chat,
    make_settings,
    relation,
    seed_contact,
    seed_item,
    store_snapshot,
    write_template,
)

NOW = datetime(2026, 9, 10, 14, 0, 0, tzinfo=UTC)

TEMPLATE = "Thanks for connecting! I help labs recover denied claims with AI.\nHappy to stay in touch.\n"


@pytest.fixture
def stats(monkeypatch):
    """Replace `messages_sync.refresh_contact_stats`; the list it returns
    records `(db, messages collection, analysis collection, dry_run)` per
    call.
    """
    calls = []

    def fake_refresh_contact_stats(db, messages_ref, analysis_ref, *, dry_run):
        calls.append((db, messages_ref.id, analysis_ref.id, dry_run))
        return {"contacts": 0, "replied": 0, "changed": 0, "cleared": 0, "unattributed": 0, "missing": 0}

    monkeypatch.setattr(messages_sync, "refresh_contact_stats", fake_refresh_contact_stats)
    return calls


def three_new_connections(db):
    """Three target-industry contacts, connected one, two and three days
    ago, returned oldest-first so a test proves `daily` orders them.
    """
    for slug in ("newest", "middle", "oldest"):
        seed_contact(db, slug, industry="RCM", seniority="Director")
    return [
        relation("oldest", "ACoAAOldest", NOW - timedelta(days=3)),
        relation("middle", "ACoAAMiddle", NOW - timedelta(days=2)),
        relation("newest", "ACoAANewest", NOW - timedelta(days=1), first="Nia", last="West"),
    ]


def queued_ids(db):
    return sorted(document.id for document in db.collection("outreach_queue").stream())


class EdgeRandom:
    """Stands in for `random.Random`: `uniform(low, high)` returns each
    bound in turn -- low, high, low, ... -- and records every `(low, high)`
    it was asked for."""

    def __init__(self):
        self.calls: list[tuple] = []

    def uniform(self, low, high):
        self.calls.append((low, high))
        return low if len(self.calls) % 2 else high


# =============================================================================
# enqueueing intros
# =============================================================================


def test_daily_enqueues_at_most_the_cap_of_new_intros_newest_connection_first(tmp_path, stats):
    """The first intro is due one random gap after `now` -- here the gap's
    low bound, one minute (ruling P5-3)."""
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db))
    write_template(tmp_path, TEMPLATE)

    summary = jobs.daily(db, client, make_settings(tmp_path, intro_daily_cap=2), NOW, rng=EdgeRandom())

    assert queued_ids(db) == ["intro:middle", "intro:newest"]
    item = queue.get(db, "intro:newest")
    assert {key: item[key] for key in (
        "contact_doc_id", "kind", "text", "provider_id", "chat_id", "name", "profile_url",
        "campaign", "template_id", "created_by", "status", "approved_by", "due_at", "created_at",
    )} == {
        "contact_doc_id": "newest",
        "kind": "intro",
        "text": TEMPLATE.strip(),
        "provider_id": "ACoAANewest",
        "chat_id": None,
        "name": "Nia West",
        "profile_url": "https://www.linkedin.com/in/newest",
        "campaign": "intro",
        "template_id": "intro",
        "created_by": "daily",
        "status": queue.APPROVED,
        "approved_by": "auto",
        "due_at": NOW + timedelta(minutes=1),
        "created_at": NOW,
    }
    assert summary["enqueued"] == 2
    assert summary["candidates"] == 3


def test_daily_spreads_its_intros_with_cumulative_gaps_from_the_settings(tmp_path, stats):
    """Ruling P5-3: each intro is due one random gap after the one before
    it, the first one gap after `now` -- never all at once. The gap is
    drawn between `intro_gap_min_minutes` and `intro_gap_max_minutes`, 1
    and 5 by default; with draws at the two bounds in turn, the three
    intros are due 1, 6 and 7 minutes after `now`, newest connection
    first."""
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db))
    write_template(tmp_path, TEMPLATE)
    rng = EdgeRandom()

    jobs.daily(db, client, make_settings(tmp_path), NOW, rng=rng)

    assert rng.calls == [(60.0, 300.0)] * 3
    due = [queue.get(db, f"intro:{slug}")["due_at"] for slug in ("newest", "middle", "oldest")]
    assert due == [NOW + timedelta(minutes=1), NOW + timedelta(minutes=6), NOW + timedelta(minutes=7)]


def test_the_intro_gap_bounds_are_the_settings_bounds(tmp_path, stats):
    """The spacing is configuration, not a constant: the wider bounds set
    here are what the gaps are drawn between."""
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db))
    write_template(tmp_path, TEMPLATE)
    rng = EdgeRandom()
    settings = make_settings(tmp_path, intro_gap_min_minutes=10, intro_gap_max_minutes=30)

    jobs.daily(db, client, settings, NOW, rng=rng)

    assert rng.calls == [(600.0, 1800.0)] * 3
    due = [queue.get(db, f"intro:{slug}")["due_at"] for slug in ("newest", "middle", "oldest")]
    assert due == [NOW + timedelta(minutes=10), NOW + timedelta(minutes=40), NOW + timedelta(minutes=50)]


def test_with_a_seeded_random_source_every_gap_is_between_the_configured_bounds(tmp_path, stats):
    """Ten intros from `random.Random(20260910)`: every gap between
    consecutive due times -- the first measured from `now` -- lies in
    [1, 5] minutes, and they are not all the same gap."""
    db = FakeFirestore()
    relations = []
    for n in range(10):
        seed_contact(db, f"c{n}", industry="RCM")
        relations.append(relation(f"c{n}", f"ACoAAc{n}", NOW - timedelta(hours=n + 1)))
    client = FakeUnipile(relations=relations)
    write_template(tmp_path, TEMPLATE)

    jobs.daily(db, client, make_settings(tmp_path, intro_daily_cap=10), NOW, rng=random.Random(20260910))

    due = sorted(queue.get(db, f"intro:c{n}")["due_at"] for n in range(10))
    gaps = [later - earlier for earlier, later in zip([NOW, *due], due)]
    assert len(gaps) == 10
    assert all(timedelta(minutes=1) <= gap <= timedelta(minutes=5) for gap in gaps)
    assert len(set(gaps)) > 1


def test_an_intro_id_passed_over_takes_no_gap(tmp_path, stats):
    """A candidate whose one intro id is already spent creates nothing, so
    the next intro is due one gap after the last one CREATED."""
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db))
    seed_item(db, "intro:middle", "middle", kind="intro", chat_id=None, provider_id="ACoAAMiddle",
              text="an older intro text", now=NOW - timedelta(days=1))
    queue.cancel(db, "intro:middle", "not now", NOW - timedelta(hours=20))
    write_template(tmp_path, TEMPLATE)

    jobs.daily(db, client, make_settings(tmp_path), NOW, rng=EdgeRandom())

    assert queue.get(db, "intro:newest")["due_at"] == NOW + timedelta(minutes=1)
    assert queue.get(db, "intro:oldest")["due_at"] == NOW + timedelta(minutes=2)


def test_the_daily_intros_only_connections_made_within_intro_connection_days(tmp_path, stats):
    """MCP v2: the backlog of older connections belongs to the notebook, so
    with a 14-day window only the connections made in the last 14 days get
    the daily intro; the others are counted as `not_new`."""
    db = FakeFirestore()
    seed_contact(db, "recent", industry="RCM")
    seed_contact(db, "older", industry="RCM")
    client = FakeUnipile(relations=[
        relation("recent", "ACoAARecent", NOW - timedelta(days=3)),
        relation("older", "ACoAAOlder", NOW - timedelta(days=30)),
    ])
    write_template(tmp_path, TEMPLATE)

    summary = jobs.daily(db, client, make_settings(tmp_path, intro_connection_days=14), NOW)

    assert queued_ids(db) == ["intro:recent"]
    assert (summary["candidates"], summary["not_new"]) == (1, 1)
    assert summary["intro_cap"] == {"per_day": 10, "used_today": 0, "remaining": 10}


def test_a_second_daily_the_same_day_enqueues_nothing_new(tmp_path, stats):
    """Both candidates were queued by the first run; their open intros drop
    them from the second, and create-only ids would refuse them anyway.
    """
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db)[1:])
    write_template(tmp_path, TEMPLATE)
    settings = make_settings(tmp_path, intro_daily_cap=5)
    jobs.daily(db, client, settings, NOW)
    after_first = store_snapshot(db)["outreach_queue"]

    summary = jobs.daily(db, client, settings, NOW + timedelta(hours=3))

    assert store_snapshot(db)["outreach_queue"] == after_first
    assert summary["enqueued"] == 0


def test_an_intro_id_that_already_exists_is_passed_over_and_does_not_count(tmp_path, stats):
    """`intro:newest` was cancelled earlier -- not open, so `newest` is
    still a candidate, but its one intro id is spent. The cap of two goes to
    the next two.
    """
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db))
    seed_item(db, "intro:newest", "newest", kind="intro", chat_id=None, provider_id="ACoAANewest",
              text="an older intro text", now=NOW - timedelta(days=1))
    queue.cancel(db, "intro:newest", "not now", NOW - timedelta(hours=20))
    write_template(tmp_path, TEMPLATE)

    summary = jobs.daily(db, client, make_settings(tmp_path, intro_daily_cap=2), NOW)

    assert queued_ids(db) == ["intro:middle", "intro:newest", "intro:oldest"]
    assert queue.get(db, "intro:newest")["status"] == queue.CANCELLED
    assert queue.get(db, "intro:newest")["text"] == "an older intro text"
    assert summary["enqueued"] == 2


def test_a_candidate_with_an_open_queue_item_is_dropped(tmp_path, stats):
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db))
    seed_item(db, "agent:newest:20260909", "newest", now=NOW - timedelta(days=1))
    write_template(tmp_path, TEMPLATE)

    summary = jobs.daily(db, client, make_settings(tmp_path), NOW)

    assert queued_ids(db) == ["agent:newest:20260909", "intro:middle", "intro:oldest"]
    assert summary["candidates"] == 2
    assert summary["open_items"] == 1


def test_the_runtime_require_approval_override_queues_intros_pending(tmp_path, stats):
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db))
    state.RuntimeState(db, clock=lambda: NOW).set_require_approval(True)
    write_template(tmp_path, TEMPLATE)

    jobs.daily(db, client, make_settings(tmp_path, require_approval=False), NOW)

    assert queue.get(db, "intro:newest")["status"] == queue.PENDING


def test_selection_skips_off_target_unclassified_and_already_chatting_connections(tmp_path, stats):
    """The skip tally is `select_intro_candidates`' own. An open chat is
    read from LinkedIn (a chat with no attendee id is ignored), not from
    Firestore.
    """
    db = FakeFirestore()
    seed_contact(db, "target", industry="Pathology")
    seed_contact(db, "hospital", industry="Hospital")
    seed_contact(db, "chatting", industry="RCM")
    client = FakeUnipile(
        relations=[
            relation("target", "ACoAATarget", NOW - timedelta(days=1)),
            relation("hospital", "ACoAAHospital", NOW - timedelta(days=1)),
            relation("unknown-person", "ACoAAUnknown", NOW - timedelta(days=1)),
            relation("chatting", "ACoAAChatting", NOW - timedelta(days=1)),
        ],
        chats=[chat("chat-a", None), chat("chat-b", "ACoAAChatting")],
    )
    write_template(tmp_path, TEMPLATE)

    summary = jobs.daily(db, client, make_settings(tmp_path), NOW)

    assert queued_ids(db) == ["intro:target"]
    assert summary["skipped"] == {
        "unclassified": 1,
        "off_target": 1,
        "handling": 0,
        "already_messaged": 0,
        "intro_already_sent": 0,
        "existing_chat": 1,
    }


def test_daily_refreshes_contact_stats_once_even_with_nothing_to_queue(tmp_path, stats):
    db = FakeFirestore()
    write_template(tmp_path, TEMPLATE)

    summary = jobs.daily(db, FakeUnipile(), make_settings(tmp_path), NOW)

    assert stats == [(db, "messages", "analysis", False)]
    assert summary["enqueued"] == 0


def test_a_restriction_met_by_daily_blocks_writes_and_raises_one_alert(tmp_path, stats):
    """Listing the chats -- a read -- raises `AccountRestricted`: daily
    queues nothing, blocks writes, raises one `restricted` alert, and lets
    the exception out, its run recorded failed with that class name -- a
    failed run, so the day's `job_failed` alert for daily is raised too.
    """
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db))
    client.messaging.read_errors["iter_chats"] = unipile_errors.AccountRestricted(
        type="errors/account_restricted", status=403, title="Account restricted"
    )
    write_template(tmp_path, TEMPLATE)

    with pytest.raises(unipile_errors.AccountRestricted):
        jobs.daily(db, client, make_settings(tmp_path), NOW)

    assert queued_ids(db) == []
    assert state.RuntimeState(db, clock=lambda: NOW).read().get("writes_blocked_at") == NOW
    assert sorted(d["id"] for d in decisions.list_decisions(db, limit=100)) == [
        "alert:job_failed:daily:20260910",
        "alert:restricted:20260910T140000000000Z",
    ]
    run = db.collection("runs").document("daily:20260910T140000000000Z").get().to_dict()
    assert (run["ok"], run["error"]) == (False, "AccountRestricted")


def test_daily_reports_the_open_queue_and_decision_counts(tmp_path, stats):
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db))
    decisions.ask(db, "Is this worth a call?", ["yes", "no"], {}, NOW - timedelta(hours=1))
    write_template(tmp_path, TEMPLATE)

    summary = jobs.daily(db, client, make_settings(tmp_path), NOW)

    assert summary["queue"] == {"pending": 0, "approved": 3, "sending": 0, "unknown": 0}
    assert summary["decisions"] == {"pending": 1, "answered": 0}
    assert summary["fetch_queue"] == {"queued": 3, "stored": 0, "short": 0, "failed": 0}


# =============================================================================
# enumerating new connections into the fetch queue (task 3a)
# =============================================================================


def test_daily_enqueues_a_fetch_queue_entry_for_each_new_unstored_connection(tmp_path, stats):
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db))
    write_template(tmp_path, TEMPLATE)

    summary = jobs.daily(db, client, make_settings(tmp_path), NOW)

    for slug in ("oldest", "middle", "newest"):
        item = fetch_queue.get(db, slug)
        assert item["status"] == fetch_queue.QUEUED
        assert item["queued_at"] == NOW
    assert fetch_queue.get(db, "newest")["provider_id"] == "ACoAANewest"
    assert fetch_queue.get(db, "newest")["name"] == "Nia West"
    assert fetch_queue.get(db, "newest")["connected_at"] == NOW - timedelta(days=1)
    assert summary["fetch_enqueued"] == 3
    assert summary["fetch_unusable_slug"] == 0


def test_daily_skips_a_connection_whose_profile_is_already_in_extracted(tmp_path, stats):
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db))
    db.collection("extracted").document("middle").set({"fullName": "already have this one"})
    write_template(tmp_path, TEMPLATE)

    summary = jobs.daily(db, client, make_settings(tmp_path), NOW)

    assert fetch_queue.get(db, "middle") is None
    assert fetch_queue.get(db, "oldest") is not None
    assert fetch_queue.get(db, "newest") is not None
    assert summary["fetch_enqueued"] == 2


def test_daily_skips_a_connection_older_than_new_connection_days(tmp_path, stats):
    db = FakeFirestore()
    seed_contact(db, "old-conn", industry="RCM")
    client = FakeUnipile(relations=[relation("old-conn", "ACoAAOld", NOW - timedelta(days=20))])
    write_template(tmp_path, TEMPLATE)

    summary = jobs.daily(db, client, make_settings(tmp_path, new_connection_days=14), NOW)

    assert fetch_queue.get(db, "old-conn") is None
    assert summary["fetch_enqueued"] == 0


def test_daily_skips_a_connection_with_no_created_at(tmp_path, stats):
    db = FakeFirestore()
    seed_contact(db, "no-date", industry="RCM")
    client = FakeUnipile(relations=[relation("no-date", "ACoAANoDate", None)])
    write_template(tmp_path, TEMPLATE)

    summary = jobs.daily(db, client, make_settings(tmp_path), NOW)

    assert fetch_queue.get(db, "no-date") is None
    assert summary["fetch_enqueued"] == 0


def test_daily_skips_a_connection_with_an_empty_public_identifier(tmp_path, stats):
    db = FakeFirestore()
    client = FakeUnipile(relations=[relation("", "ACoAAEmpty", NOW - timedelta(days=1))])
    write_template(tmp_path, TEMPLATE)

    summary = jobs.daily(db, client, make_settings(tmp_path), NOW)

    assert summary["fetch_enqueued"] == 0
    assert summary["fetch_unusable_slug"] == 0


def test_daily_counts_an_unusable_slug_without_raising(tmp_path, stats):
    """A slug `fetch_queue.enqueue` rejects (containing `/`) is counted and
    skipped rather than aborting the whole run."""
    db = FakeFirestore()
    client = FakeUnipile(relations=[
        relation("bad/slug", "ACoAABad", NOW - timedelta(days=1)),
        *three_new_connections(db),
    ])
    write_template(tmp_path, TEMPLATE)

    summary = jobs.daily(db, client, make_settings(tmp_path), NOW)

    assert fetch_queue.get(db, "bad/slug") is None
    assert summary["fetch_unusable_slug"] == 1
    assert summary["fetch_enqueued"] == 3


def test_a_second_daily_the_same_day_enqueues_no_new_fetch_queue_entries(tmp_path, stats):
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db))
    write_template(tmp_path, TEMPLATE)
    settings = make_settings(tmp_path)
    jobs.daily(db, client, settings, NOW)
    after_first = store_snapshot(db)["fetch_queue"]

    summary = jobs.daily(db, client, settings, NOW + timedelta(hours=3))

    assert store_snapshot(db)["fetch_queue"] == after_first
    assert summary["fetch_enqueued"] == 0


# =============================================================================
# the template
# =============================================================================


def test_a_template_with_an_unfilled_slot_stops_daily_before_reading_linkedin(tmp_path, stats):
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db))
    write_template(tmp_path, "Hi {first_name}, thanks for connecting!")

    with pytest.raises(ValueError, match="text:unfilled_slot"):
        jobs.daily(db, client, make_settings(tmp_path), NOW)

    assert queued_ids(db) == []
    assert client.messaging.iter_chats_calls == 0
    assert client.users.iter_relations_calls == 0
    run = db.collection("runs").document("daily:20260910T140000000000Z").get().to_dict()
    assert (run["ok"], run["error"]) == (False, "ValueError")


def test_a_template_linking_to_an_unlisted_domain_stops_daily(tmp_path, stats):
    db = FakeFirestore()
    write_template(tmp_path, "Thanks for connecting -- see https://example.com for more.")

    with pytest.raises(ValueError, match="text:link_not_allowed"):
        jobs.daily(db, FakeUnipile(), make_settings(tmp_path, allowed_link_domains=[]), NOW)


# =============================================================================
# the stale-claim sweep
# =============================================================================


def claimed(db, queue_id, contact_doc_id, *, at, kind="follow_up"):
    seed_item(db, queue_id, contact_doc_id, kind=kind, now=at - timedelta(hours=1))
    queue.claim(db, queue_id, "a-crashed-tick", at)


def test_daily_sweeps_a_claim_older_than_ten_minutes_to_unknown_with_one_alert(tmp_path, stats):
    """Claimed ten minutes and a second ago: swept. Claimed exactly ten
    minutes ago: not yet -- "more than ten minutes" is strict.
    """
    db = FakeFirestore()
    claimed(db, "agent:ivy:20260910", "ivy", at=NOW - timedelta(minutes=10, seconds=1))
    claimed(db, "agent:jon:20260910", "jon", at=NOW - timedelta(minutes=10))
    write_template(tmp_path, TEMPLATE)

    summary = jobs.daily(db, FakeUnipile(), make_settings(tmp_path), NOW)

    ivy = queue.get(db, "agent:ivy:20260910")
    assert ivy["status"] == queue.UNKNOWN
    assert ivy["error"] == "claimed and never settled"
    assert queue.get(db, "agent:jon:20260910")["status"] == queue.SENDING
    alert = decisions.get(db, "alert:unknown_send:agent:ivy:20260910")
    assert alert["status"] == "pending"
    assert alert["context"]["queue_id"] == "agent:ivy:20260910"
    assert alert["context"]["contact_doc_id"] == "ivy"
    assert [d["id"] for d in decisions.list_decisions(db, limit=100)] == ["alert:unknown_send:agent:ivy:20260910"]
    assert summary["stale_swept"] == 1


def test_sweep_stale_returns_the_ids_it_moved_and_is_a_no_op_the_second_time():
    db = FakeFirestore()
    claimed(db, "agent:ivy:20260910", "ivy", at=NOW - timedelta(minutes=30))

    first = jobs.sweep_stale(db, NOW)
    second = jobs.sweep_stale(db, NOW + timedelta(minutes=5))

    assert first == ["agent:ivy:20260910"]
    assert second == []
    assert len(decisions.list_decisions(db, limit=100)) == 1


def raises_firestore_unavailable(*args, **kwargs):
    raise RuntimeError("firestore unavailable")


def test_a_sweep_whose_alert_cannot_be_written_leaves_the_claim_sending_for_the_next_sweep(monkeypatch):
    """Writing the alert fails: the sweep raises, and the item is still
    `sending` with no ledger row -- nothing moved it to `unknown`. The next
    sweep raises the alert and moves the item.
    """
    db = FakeFirestore()
    claimed(db, "agent:ivy:20260910", "ivy", at=NOW - timedelta(minutes=30))
    monkeypatch.setattr(decisions, "raise_alert", raises_firestore_unavailable)

    with pytest.raises(RuntimeError, match="firestore unavailable"):
        jobs.sweep_stale(db, NOW)

    assert queue.get(db, "agent:ivy:20260910")["status"] == queue.SENDING
    assert list(db.collection("action_log").stream()) == []

    monkeypatch.undo()
    swept = jobs.sweep_stale(db, NOW + timedelta(minutes=5))

    assert swept == ["agent:ivy:20260910"]
    assert queue.get(db, "agent:ivy:20260910")["status"] == queue.UNKNOWN
    assert [d["id"] for d in decisions.list_decisions(db, limit=100)] == ["alert:unknown_send:agent:ivy:20260910"]


def test_a_sweep_whose_status_change_fails_after_the_alert_raises_no_second_alert(monkeypatch):
    """`mark_unknown` fails: the sweep raises with the alert already stored
    and the item still `sending`. The next sweep moves the item, and its
    `raise_alert` writes nothing (create-only): one alert in all.
    """
    db = FakeFirestore()
    claimed(db, "agent:ivy:20260910", "ivy", at=NOW - timedelta(minutes=30))
    monkeypatch.setattr(queue, "mark_unknown", raises_firestore_unavailable)

    with pytest.raises(RuntimeError, match="firestore unavailable"):
        jobs.sweep_stale(db, NOW)

    assert queue.get(db, "agent:ivy:20260910")["status"] == queue.SENDING
    assert [d["id"] for d in decisions.list_decisions(db, limit=100)] == ["alert:unknown_send:agent:ivy:20260910"]

    monkeypatch.undo()
    swept = jobs.sweep_stale(db, NOW + timedelta(minutes=5))

    assert swept == ["agent:ivy:20260910"]
    assert queue.get(db, "agent:ivy:20260910")["status"] == queue.UNKNOWN
    assert [d["id"] for d in decisions.list_decisions(db, limit=100)] == ["alert:unknown_send:agent:ivy:20260910"]


# =============================================================================
# dry run
# =============================================================================


def test_dry_run_reports_the_intros_it_would_queue_and_writes_nothing(tmp_path, stats):
    db = FakeFirestore()
    client = FakeUnipile(relations=three_new_connections(db))
    seed_item(db, "intro:newest", "newest", kind="intro", chat_id=None, provider_id="ACoAANewest",
              now=NOW - timedelta(days=1))
    queue.cancel(db, "intro:newest", "not now", NOW - timedelta(hours=20))
    claimed(db, "agent:ivy:20260910", "ivy", at=NOW - timedelta(minutes=30))
    write_template(tmp_path, TEMPLATE)
    before = store_snapshot(db)

    summary = jobs.daily(db, client, make_settings(tmp_path, intro_daily_cap=2), NOW, dry_run=True)

    assert store_snapshot(db) == before
    assert summary["dry_run"] is True
    assert summary["would_enqueue"] == ["intro:middle", "intro:oldest"]
    assert summary["would_sweep"] == 1
    assert summary["would_fetch"] == 3
    assert stats == [(db, "messages", "analysis", True)]


def test_dry_run_counts_an_unusable_slug_without_raising_or_writing(tmp_path, stats):
    """A dry run applies the same usability check a real run reaches through
    `fetch_queue.enqueue`: `bad/slug` is skipped and counted in
    `fetch_unusable_slug` rather than counted as fetchable, `would_fetch`
    counts only the three clean connections, and nothing is written.
    """
    db = FakeFirestore()
    client = FakeUnipile(relations=[
        relation("bad/slug", "ACoAABad", NOW - timedelta(days=1)),
        *three_new_connections(db),
    ])
    write_template(tmp_path, TEMPLATE)
    before = store_snapshot(db)

    summary = jobs.daily(db, client, make_settings(tmp_path), NOW, dry_run=True)

    assert store_snapshot(db) == before
    assert summary["would_fetch"] == 3
    assert summary["fetch_unusable_slug"] == 1
