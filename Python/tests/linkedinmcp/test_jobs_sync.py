"""Tests for `linkedinmcp.jobs.sync`: mirror LinkedIn's new messages into
`messages`, then react -- a reply cancels the contact's queued items, an
`unknown` send is resolved against the stored history (ruling P2-8), and a
contact newly classified `lead` raises one alert.

`sync` reaches `messages_sync` through the module, so most tests replace
its five functions on that module with `FakeSync`'s, whose signatures are
the real ones exactly -- no `**kwargs` -- so a call of the wrong shape fails
here. The rest run the REAL `messages_sync` code against `FakeFirestore` and
the stub client: the first restricted-account test, whose forward pass meets
`AccountRestricted` from the stub, and the tests under "end to end".
"""

from datetime import UTC, datetime, timedelta

import pytest

import messages_sync
from lib.unipile import errors as unipile_errors
from lib.unipile.models import Message
from linkedinmcp import decisions, jobs, queue, state
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_unipile import (
    FakeUnipile,
    make_settings,
    seed_contact,
    seed_item,
    seed_message,
    store_snapshot,
)

NOW = datetime(2026, 9, 10, 14, 0, 0, tzinfo=UTC)

#: The newest stored message before the sync runs.
MAX_TS = NOW - timedelta(hours=2)

GIVE_UP_ERROR = "no outbound message in LinkedIn history 48 h after the attempt"


class FakeSync:
    """Stands in for the five `messages_sync` functions `sync` calls, and
    records every call. `forward_pass` stores `new_messages` -- `(id, body)`
    pairs -- unless `dry_run`, and returns how many there are either way.
    `refresh_contact_stats` raises `refresh_error` after recording the call,
    when one is given.
    """

    def __init__(self, monkeypatch, *, max_ts, new_messages=(), skip_ids=(), refresh_error=None):
        self.max_ts = max_ts
        self.new_messages = list(new_messages)
        self.skip_ids = set(skip_ids)
        self.refresh_error = refresh_error
        self.calls = []
        self.resolvers = []
        fake = self

        class ContactResolver:
            def __init__(self, client, messages_ref, extracted_ref, *, join):
                fake.calls.append(("ContactResolver", client, messages_ref.id, extracted_ref.id, join))
                fake.resolvers.append(self)

        monkeypatch.setattr(messages_sync, "_watermarks", self._watermarks)
        monkeypatch.setattr(messages_sync, "_ids_since", self._ids_since)
        monkeypatch.setattr(messages_sync, "ContactResolver", ContactResolver)
        monkeypatch.setattr(messages_sync, "forward_pass", self._forward_pass)
        monkeypatch.setattr(messages_sync, "refresh_contact_stats", self._refresh_contact_stats)

    def _watermarks(self, messages_ref):
        self.calls.append(("_watermarks", messages_ref.id))
        oldest = self.max_ts - timedelta(days=400) if self.max_ts is not None else None
        return oldest, self.max_ts

    def _ids_since(self, messages_ref, moment):
        self.calls.append(("_ids_since", messages_ref.id, moment))
        return set(self.skip_ids)

    def _forward_pass(self, client, db, messages_ref, resolver, cursor, skip_ids, *, dry_run):
        self.calls.append(("forward_pass", client, db, messages_ref.id, resolver, cursor, skip_ids, dry_run))
        if not dry_run:
            for message_id, body in self.new_messages:
                messages_ref.document(message_id).set(body)
        return len(self.new_messages)

    def _refresh_contact_stats(self, db, messages_ref, analysis_ref, *, dry_run):
        self.calls.append(("refresh_contact_stats", db, messages_ref.id, analysis_ref.id, dry_run))
        if self.refresh_error is not None:
            raise self.refresh_error
        return {"contacts": 0, "replied": 0, "changed": 0, "cleared": 0, "unattributed": 0, "missing": 0}

    def names(self):
        return [call[0] for call in self.calls]


def message_body(contact_doc_id, *, is_sender, timestamp, chat_id="chat-1"):
    return {
        "chat_id": chat_id,
        "contact_doc_id": contact_doc_id,
        "is_sender": is_sender,
        "timestamp": timestamp,
        "text": "a message",
        "is_event": 0,
        "deleted": 0,
    }


def status(db, queue_id):
    return queue.get(db, queue_id)["status"]


def unknown_item(db, queue_id, contact_doc_id, *, sending_at, kind="follow_up"):
    """An item claimed at `sending_at` and settled `unknown` -- the real
    path a send whose outcome nobody knows takes.
    """
    seed_item(db, queue_id, contact_doc_id, kind=kind, now=sending_at - timedelta(hours=1))
    queue.claim(db, queue_id, "an-earlier-tick", sending_at)
    queue.settle(db, queue_id, queue.UNKNOWN, now=sending_at, error="ServerError")


# =============================================================================
# the forward pass and the stats refresh
# =============================================================================


def test_sync_with_no_cursor_runs_the_forward_pass_from_the_newest_stored_message(monkeypatch, tmp_path):
    """No `runtime_state/messages_sync` document yet: the newest stored
    message stands in for the cursor."""
    db = FakeFirestore()
    client = FakeUnipile()
    fake = FakeSync(
        monkeypatch,
        max_ts=MAX_TS,
        skip_ids={"stored-at-the-watermark"},
        new_messages=[
            ("new-1", message_body("alice", is_sender=1, timestamp=MAX_TS + timedelta(minutes=1))),
            ("new-2", message_body("bob", is_sender=1, timestamp=MAX_TS + timedelta(minutes=2))),
        ],
    )

    summary = jobs.sync(db, client, make_settings(tmp_path), NOW)

    assert fake.names() == [
        "_watermarks", "ContactResolver", "_ids_since", "forward_pass", "refresh_contact_stats",
    ]
    assert fake.calls[1] == ("ContactResolver", client, "messages", "extracted", True)
    assert fake.calls[2] == ("_ids_since", "messages", MAX_TS)
    assert fake.calls[3] == (
        "forward_pass", client, db, "messages", fake.resolvers[0], MAX_TS, {"stored-at-the-watermark"}, False,
    )
    assert fake.calls[4] == ("refresh_contact_stats", db, "messages", "analysis", False)
    assert summary == {
        "stale_swept": 0,
        "written": 2,
        "stats_refreshed": True,
        "replied_contacts": 0,
        "cancelled": 0,
        "unknown_sent": 0,
        "unknown_failed": 0,
        "classified": 0,
        "new_leads": 0,
    }


def test_sync_that_wrote_nothing_does_not_refresh_contact_stats(monkeypatch, tmp_path):
    db = FakeFirestore()
    fake = FakeSync(monkeypatch, max_ts=MAX_TS)

    summary = jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    assert "refresh_contact_stats" not in fake.names()
    assert summary["written"] == 0
    assert summary["stats_refreshed"] is False


def test_sync_with_no_stored_history_reads_nothing_from_linkedin(monkeypatch, tmp_path):
    db = FakeFirestore()
    fake = FakeSync(monkeypatch, max_ts=None)

    summary = jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    assert summary == {"skipped": "no_history"}
    assert fake.names() == ["_watermarks"]


# =============================================================================
# the stale-claim sweep (ruling P5-4: sync runs every 15 minutes, all week)
# =============================================================================


def stale_claim(db, queue_id, contact_doc_id, *, claimed_at):
    """An item claimed at `claimed_at` by a tick that never settled it."""
    seed_item(db, queue_id, contact_doc_id, now=claimed_at - timedelta(hours=1))
    queue.claim(db, queue_id, "a-crashed-tick", claimed_at)


def test_sync_sweeps_a_claim_older_than_ten_minutes_to_unknown_with_one_alert(monkeypatch, tmp_path):
    db = FakeFirestore()
    stale_claim(db, "agent:ivy:20260910", "ivy", claimed_at=NOW - timedelta(minutes=30))
    stale_claim(db, "agent:jon:20260910", "jon", claimed_at=NOW - timedelta(minutes=10))
    FakeSync(monkeypatch, max_ts=MAX_TS)

    summary = jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    assert status(db, "agent:ivy:20260910") == queue.UNKNOWN
    assert status(db, "agent:jon:20260910") == queue.SENDING
    assert alert_ids(db) == ["alert:unknown_send:agent:ivy:20260910"]
    assert summary["stale_swept"] == 1


def test_a_claim_sync_sweeps_is_resolved_sent_in_the_same_sync_when_its_message_is_stored(monkeypatch, tmp_path):
    """The sweep comes first: the claim becomes `unknown`, and the same
    sync's resolution (ruling P2-8) finds the outbound message stored after
    the attempt and settles it `sent`."""
    db = FakeFirestore()
    claimed_at = NOW - timedelta(minutes=30)
    stale_claim(db, "agent:ivy:20260910", "ivy", claimed_at=claimed_at)
    seed_message(db, "ivy-sent", "ivy", is_sender=1, timestamp=claimed_at + timedelta(seconds=40))
    FakeSync(monkeypatch, max_ts=NOW - timedelta(minutes=1))

    summary = jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    item = queue.get(db, "agent:ivy:20260910")
    assert (item["status"], item["message_id"]) == (queue.SENT, "ivy-sent")
    assert (summary["stale_swept"], summary["unknown_sent"]) == (1, 1)


def test_a_dry_sync_counts_the_claims_it_would_sweep_and_moves_none(monkeypatch, tmp_path):
    db = FakeFirestore()
    stale_claim(db, "agent:ivy:20260910", "ivy", claimed_at=NOW - timedelta(minutes=30))
    FakeSync(monkeypatch, max_ts=MAX_TS)
    before = store_snapshot(db)

    summary = jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW, dry_run=True)

    assert summary == {"dry_run": True, "would_write": 0, "would_sweep": 1}
    assert store_snapshot(db) == before


# =============================================================================
# replies
# =============================================================================


def test_a_reply_cancels_that_contacts_pending_and_approved_items_and_no_one_elses(monkeypatch, tmp_path):
    """Alice wrote after the watermark: her `approved` and `pending` items
    are cancelled, her in-flight `sending` item is not. Bob's new message is
    one WE sent; Carol's inbound predates the watermark; an inbound with no
    resolved contact belongs to nobody. None of their items change.
    """
    db = FakeFirestore()
    seed_item(db, "agent:alice:20260909", "alice", now=NOW - timedelta(days=1))
    seed_item(db, "agent:alice:20260910", "alice", now=NOW - timedelta(hours=3), require_approval=True)
    seed_item(db, "agent:alice:20260908", "alice", now=NOW - timedelta(days=2))
    queue.claim(db, "agent:alice:20260908", "another-tick", NOW - timedelta(minutes=1))
    seed_item(db, "agent:bob:20260910", "bob", now=NOW - timedelta(hours=3))
    seed_item(db, "agent:carol:20260910", "carol", now=NOW - timedelta(hours=3))
    seed_message(db, "carol-old", "carol", is_sender=0, timestamp=MAX_TS - timedelta(hours=1))
    FakeSync(
        monkeypatch,
        max_ts=MAX_TS,
        new_messages=[
            ("alice-reply", message_body("alice", is_sender=0, timestamp=MAX_TS + timedelta(minutes=5))),
            ("to-bob", message_body("bob", is_sender=1, timestamp=MAX_TS + timedelta(minutes=6))),
            ("unjoined", message_body(None, is_sender=0, timestamp=MAX_TS + timedelta(minutes=7))),
        ],
    )

    summary = jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    assert status(db, "agent:alice:20260909") == queue.CANCELLED
    assert status(db, "agent:alice:20260910") == queue.CANCELLED
    assert queue.get(db, "agent:alice:20260909")["cancel_reason"] == "they replied"
    assert status(db, "agent:alice:20260908") == queue.SENDING
    assert status(db, "agent:bob:20260910") == queue.APPROVED
    assert status(db, "agent:carol:20260910") == queue.APPROVED
    assert summary["replied_contacts"] == 1
    assert summary["cancelled"] == 2


@pytest.mark.parametrize("flag", ["is_event", "deleted"])
def test_an_inbound_event_or_deleted_message_is_not_a_reply(monkeypatch, tmp_path, flag):
    """Reply detection ignores what the guards ignore. An inbound message
    flagged `is_event` or `deleted`, stored after the watermark, cancels
    nothing: Ada's queued intro stays `approved` -- cancelled, its
    create-only id could never be queued again. Bea's real reply in the
    same sync still cancels her item.
    """
    db = FakeFirestore()
    seed_item(db, "intro:ada", "ada", kind="intro", chat_id=None, provider_id="ACoAAAda", now=NOW - timedelta(hours=3))
    seed_item(db, "agent:bea:20260910", "bea", now=NOW - timedelta(hours=3))
    FakeSync(
        monkeypatch,
        max_ts=MAX_TS,
        new_messages=[
            (f"ada-{flag}", {**message_body("ada", is_sender=0, timestamp=MAX_TS + timedelta(minutes=5)), flag: 1}),
            ("bea-reply", message_body("bea", is_sender=0, timestamp=MAX_TS + timedelta(minutes=6))),
        ],
    )

    summary = jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    assert status(db, "intro:ada") == queue.APPROVED
    assert status(db, "agent:bea:20260910") == queue.CANCELLED
    assert (summary["replied_contacts"], summary["cancelled"]) == (1, 1)


def test_a_stats_refresh_that_raises_still_leaves_the_replied_contacts_items_cancelled(monkeypatch, tmp_path):
    """The forward pass stores Alice's reply and the stats refresh then
    raises: the sync fails, its run recorded failed, and her human-approved
    `reply` item is already cancelled. The next sync's watermark is past
    her reply, so no later sync would find it to cancel on.
    """
    db = FakeFirestore()
    seed_item(db, "agent:alice:20260910", "alice", kind="reply", now=NOW - timedelta(hours=3))
    queue.approve(db, "agent:alice:20260910", NOW - timedelta(hours=2))
    FakeSync(
        monkeypatch,
        max_ts=MAX_TS,
        new_messages=[("alice-reply", message_body("alice", is_sender=0, timestamp=MAX_TS + timedelta(minutes=5)))],
        refresh_error=RuntimeError("firestore unavailable"),
    )

    with pytest.raises(RuntimeError, match="firestore unavailable"):
        jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    assert status(db, "agent:alice:20260910") == queue.CANCELLED
    run = db.collection("runs").document("sync:20260910T140000000000Z").get().to_dict()
    assert (run["ok"], run["error"]) == (False, "RuntimeError")


# =============================================================================
# unknown resolution (ruling P2-8)
# =============================================================================


def test_unknown_resolves_sent_on_the_earliest_outbound_message_from_five_minutes_before_the_attempt(
    monkeypatch, tmp_path
):
    """An outbound message six minutes before the attempt is too early to be
    it. `a-reply-of-theirs` sits exactly on the window's lower edge and would
    win the (timestamp, id) tie -- but it is inbound, so it is not ours.
    `the-send`, exactly on `sending_at - 5 min`, is the earliest outbound at
    or after it, and is the one taken as the send.
    """
    db = FakeFirestore()
    sending_at = NOW - timedelta(hours=1)
    unknown_item(db, "agent:dana:20260910", "dana", sending_at=sending_at)
    seed_message(db, "too-early", "dana", is_sender=1, timestamp=sending_at - timedelta(minutes=6))
    seed_message(db, "a-reply-of-theirs", "dana", is_sender=0, timestamp=sending_at - timedelta(minutes=5))
    seed_message(db, "the-send", "dana", is_sender=1, timestamp=sending_at - timedelta(minutes=5))
    seed_message(db, "a-later-one", "dana", is_sender=1, timestamp=sending_at + timedelta(minutes=30))
    FakeSync(monkeypatch, max_ts=NOW - timedelta(minutes=1))

    summary = jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    item = queue.get(db, "agent:dana:20260910")
    assert item["status"] == queue.SENT
    assert item["message_id"] == "the-send"
    assert summary["unknown_sent"] == 1
    assert summary["unknown_failed"] == 0


def test_unknown_with_no_outbound_message_fails_once_the_attempt_is_more_than_48_hours_old(monkeypatch, tmp_path):
    db = FakeFirestore()
    sending_at = NOW - timedelta(hours=48, seconds=1)
    unknown_item(db, "agent:dana:20260908", "dana", sending_at=sending_at)
    seed_message(db, "too-early", "dana", is_sender=1, timestamp=sending_at - timedelta(minutes=6))
    seed_message(db, "theirs", "dana", is_sender=0, timestamp=sending_at + timedelta(hours=1))
    FakeSync(monkeypatch, max_ts=NOW - timedelta(minutes=1))

    summary = jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    item = queue.get(db, "agent:dana:20260908")
    assert item["status"] == queue.FAILED
    assert item["error"] == GIVE_UP_ERROR
    assert summary["unknown_failed"] == 1
    assert summary["unknown_sent"] == 0


def test_unknown_with_no_outbound_message_exactly_48_hours_after_the_attempt_is_left_unknown(monkeypatch, tmp_path):
    db = FakeFirestore()
    unknown_item(db, "agent:dana:20260908", "dana", sending_at=NOW - timedelta(hours=48))
    FakeSync(monkeypatch, max_ts=NOW - timedelta(minutes=1))

    summary = jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    assert status(db, "agent:dana:20260908") == queue.UNKNOWN
    assert summary["unknown_sent"] == 0
    assert summary["unknown_failed"] == 0


def test_unknown_with_no_attempt_time_is_left_for_a_human(monkeypatch, tmp_path):
    """Without `sending_at` there is no window to match a stored message
    against, however old the item is.
    """
    db = FakeFirestore()
    unknown_item(db, "agent:dana:20260801", "dana", sending_at=NOW - timedelta(days=40))
    db.collection("outreach_queue").document("agent:dana:20260801").set({"sending_at": None}, merge=True)
    seed_message(db, "ours", "dana", is_sender=1, timestamp=NOW - timedelta(days=39))
    FakeSync(monkeypatch, max_ts=NOW - timedelta(minutes=1))

    jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    assert status(db, "agent:dana:20260801") == queue.UNKNOWN


# =============================================================================
# classification and lead alerts
# =============================================================================


def pipeline_row(doc_id, previous_stage, stage, *, last_inbound="2026-09-10"):
    """One `pipeline.run_pipeline` row, with every key the real one has --
    `transcript` included, which must never be copied anywhere.
    """
    return {
        "doc_id": doc_id,
        "profile_url": f"https://www.linkedin.com/in/{doc_id}",
        "previous_stage": previous_stage,
        "stage": stage,
        "reason": f"{doc_id} asked what the pilot would cost.",
        "thinking_level": "low",
        "inbound_total": 2,
        "last_inbound": last_inbound,
        "transcript": "--- conversation 1 ---\n2026-09-10 Them: TRANSCRIPT-MUST-NOT-LEAK",
    }


def test_a_change_to_lead_raises_one_alert_per_contact_and_newest_inbound(monkeypatch, tmp_path):
    db = FakeFirestore()
    FakeSync(monkeypatch, max_ts=MAX_TS)
    rows = [
        pipeline_row("erin", "prospect", "lead"),
        pipeline_row("finn", "lead", "lead"),
        pipeline_row("gail", "", "reject"),
        pipeline_row("hank", "", "lead", last_inbound="2026-09-09"),
    ]
    calls = []

    def classify(db_arg):
        calls.append(db_arg)
        return rows

    first = jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW, classify=classify)
    second = jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW + timedelta(minutes=5), classify=classify)

    alerts = decisions.list_decisions(db, limit=100)
    assert sorted(alert["id"] for alert in alerts) == ["alert:lead:erin:2026-09-10", "alert:lead:hank:2026-09-09"]
    erin = decisions.get(db, "alert:lead:erin:2026-09-10")
    assert erin["context"] == {
        "doc_id": "erin",
        "previous_stage": "prospect",
        "stage": "lead",
        "reason": "erin asked what the pilot would cost.",
        "last_inbound": "2026-09-10",
    }
    assert erin["asked_by"] == "service"
    assert "TRANSCRIPT-MUST-NOT-LEAK" not in repr(store_snapshot(db))
    assert calls == [db, db]
    assert (first["classified"], first["new_leads"]) == (4, 2)
    assert (second["classified"], second["new_leads"]) == (4, 0)


def test_sync_without_a_classifier_raises_no_alert(monkeypatch, tmp_path):
    db = FakeFirestore()
    FakeSync(monkeypatch, max_ts=MAX_TS)

    summary = jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    assert decisions.list_decisions(db) == []
    assert (summary["classified"], summary["new_leads"]) == (0, 0)


# =============================================================================
# a restricted account
# =============================================================================


def restricted() -> unipile_errors.AccountRestricted:
    return unipile_errors.AccountRestricted(type="errors/account_restricted", status=403, title="Account restricted")


def writes_blocked_at(db):
    return state.RuntimeState(db, clock=lambda: NOW).read().get("writes_blocked_at")


def alert_ids(db):
    return sorted(decision["id"] for decision in decisions.list_decisions(db, limit=100))


def test_a_restriction_met_by_sync_blocks_writes_and_raises_one_alert_per_restriction(tmp_path):
    """No `messages_sync` function is replaced: the forward pass asks
    LinkedIn for new messages and gets `AccountRestricted`. Sync blocks
    writes, raises one `restricted` alert and lets the exception out, its
    run recorded failed with that class name -- and, a failed run, the
    day's `job_failed` alert for sync. The next sync -- a new client, the
    account still restricted -- raises no second alert of either kind:
    writes are already blocked, and the day's `job_failed` alert exists.
    """
    db = FakeFirestore()
    seed_message(db, "ours-1", "jane-roe", is_sender=1, timestamp=NOW - timedelta(days=6))
    settings = make_settings(tmp_path)
    clients = [FakeUnipile(), FakeUnipile()]
    for client in clients:
        client.messaging.read_errors["iter_all_messages"] = restricted()

    with pytest.raises(unipile_errors.AccountRestricted):
        jobs.sync(db, clients[0], settings, NOW)

    assert writes_blocked_at(db) == NOW
    assert alert_ids(db) == ["alert:job_failed:sync:20260910", "alert:restricted:20260910T140000000000Z"]
    run = db.collection("runs").document("sync:20260910T140000000000Z").get().to_dict()
    assert (run["ok"], run["error"]) == (False, "AccountRestricted")

    with pytest.raises(unipile_errors.AccountRestricted):
        jobs.sync(db, clients[1], settings, NOW + timedelta(minutes=15))

    assert writes_blocked_at(db) == NOW
    assert alert_ids(db) == ["alert:job_failed:sync:20260910", "alert:restricted:20260910T140000000000Z"]


def test_an_account_restricted_escaping_sync_is_enough_without_the_client_flag(monkeypatch, tmp_path):
    """The stand-in forward pass raises `AccountRestricted` itself, past the
    client, so the client's `writes_blocked` is never set: the exception
    escaping the job is enough on its own to block writes and raise one
    `restricted` alert (beside the failed run's `job_failed` alert).
    """
    db = FakeFirestore()
    client = FakeUnipile()
    FakeSync(monkeypatch, max_ts=MAX_TS)

    def forward_pass_that_raises(client_arg, db_arg, messages_ref, resolver, cursor, skip_ids, *, dry_run):
        raise restricted()

    monkeypatch.setattr(messages_sync, "forward_pass", forward_pass_that_raises)

    with pytest.raises(unipile_errors.AccountRestricted):
        jobs.sync(db, client, make_settings(tmp_path), NOW)

    assert client.writes_blocked is False
    assert writes_blocked_at(db) == NOW
    assert alert_ids(db) == ["alert:job_failed:sync:20260910", "alert:restricted:20260910T140000000000Z"]


def test_a_restriction_some_code_caught_still_blocks_writes_when_sync_ends(monkeypatch, tmp_path):
    """The forward pass returns normally, but the client's breaker tripped
    during it -- the real client sets `writes_blocked` only while raising
    `AccountRestricted`, so something caught one. Sync checks the flag as
    it ends: writes are blocked and one alert is raised, and the run itself
    succeeded.
    """
    db = FakeFirestore()
    client = FakeUnipile()
    FakeSync(monkeypatch, max_ts=MAX_TS)

    def forward_pass_that_caught_a_restriction(
        client_arg, db_arg, messages_ref, resolver, cursor, skip_ids, *, dry_run
    ):
        client_arg.writes_blocked = True
        return 0

    monkeypatch.setattr(messages_sync, "forward_pass", forward_pass_that_caught_a_restriction)

    summary = jobs.sync(db, client, make_settings(tmp_path), NOW)

    assert writes_blocked_at(db) == NOW
    assert alert_ids(db) == ["alert:restricted:20260910T140000000000Z"]
    run = db.collection("runs").document("sync:20260910T140000000000Z").get().to_dict()
    assert (run["ok"], run["error"]) == (True, None)
    assert summary["written"] == 0


# =============================================================================
# dry run
# =============================================================================


def test_dry_run_reports_what_the_forward_pass_counted_and_writes_nothing(monkeypatch, tmp_path):
    """The item below is `unknown`, more than 48 h old, with no outbound
    message -- a real run would fail it. Alice's stored reply is newer than
    the watermark -- a real run would cancel her item. Neither happens, the
    classifier is never called, and the store is unchanged.
    """
    db = FakeFirestore()
    unknown_item(db, "agent:dana:20260901", "dana", sending_at=NOW - timedelta(days=5))
    seed_item(db, "agent:alice:20260910", "alice", now=NOW - timedelta(hours=3))
    seed_message(db, "alice-reply", "alice", is_sender=0, timestamp=MAX_TS + timedelta(minutes=5))
    fake = FakeSync(
        monkeypatch,
        max_ts=MAX_TS,
        new_messages=[("new-1", message_body("alice", is_sender=0, timestamp=MAX_TS + timedelta(minutes=9)))],
    )
    calls = []
    before = store_snapshot(db)

    summary = jobs.sync(
        db, FakeUnipile(), make_settings(tmp_path), NOW, dry_run=True, classify=lambda db_arg: calls.append(db_arg)
    )

    assert summary == {"dry_run": True, "would_write": 1, "would_sweep": 0}
    assert fake.calls[3][-1] is True
    assert "refresh_contact_stats" not in fake.names()
    assert calls == []
    assert store_snapshot(db) == before


# =============================================================================
# end to end, against the real messages_sync code
# =============================================================================


def test_sync_against_the_real_messages_sync_stores_a_reply_and_cancels_the_queued_follow_up(tmp_path):
    """No `messages_sync` function is replaced here. Our opening message is
    stored; LinkedIn's mailbox (the stub's) holds it plus Jane's reply. The
    real forward pass stores the reply with the contact resolved from the
    stored sibling in the same chat, the real stats refresh merges her
    tallies into her existing `analysis` document, and the reply cancels
    her queued follow-up.
    """
    db = FakeFirestore()
    opened = NOW - timedelta(days=6)
    replied = NOW - timedelta(hours=1)
    seed_contact(db, "jane-roe", firstName="Jane", industry="RCM")
    seed_message(
        db, "ours-1", "jane-roe", is_sender=1, timestamp=opened, chat_id="chat-jane",
        contact_provider_id="ACoAAJane", contact_member_id="111",
    )
    seed_item(db, "agent:jane-roe:20260910", "jane-roe", now=NOW - timedelta(hours=2), chat_id="chat-jane")
    client = FakeUnipile()
    client.messaging.mailbox = [
        Message(id="ours-1", chat_id="chat-jane", is_sender=1, text="Thanks for connecting", timestamp=opened),
        Message(
            id="reply-1", chat_id="chat-jane", is_sender=0, sender_id="ACoAAJane",
            text="Tell me more", timestamp=replied,
        ),
    ]

    summary = jobs.sync(db, client, make_settings(tmp_path), NOW)

    stored = db.collection("messages").document("reply-1").get().to_dict()
    assert stored["contact_doc_id"] == "jane-roe"
    assert stored["is_sender"] == 0
    assert stored["timestamp"] == replied
    assert db.collection("analysis").document("jane-roe").get().to_dict()["replied_total"] == 1
    assert status(db, "agent:jane-roe:20260910") == queue.CANCELLED
    assert summary["written"] == 1
    assert summary["stats_refreshed"] is True
    assert (summary["replied_contacts"], summary["cancelled"]) == (1, 1)


def test_a_reply_stored_at_exactly_the_watermark_cancels_and_the_message_already_there_does_not(tmp_path):
    """The newest stored message is Bob's; Jane's reply carries the same
    timestamp and only arrives now. The forward pass reads from just before
    that moment, skipping what is stored there, so it stores Jane's reply --
    which cancels her queued follow-up. Bob's message was handled by the
    sync that stored it: the item queued for him since stays approved."""
    db = FakeFirestore()
    opened, newest = NOW - timedelta(days=6), NOW - timedelta(hours=1)
    seed_contact(db, "jane-roe", firstName="Jane", industry="RCM")
    seed_message(
        db, "ours-1", "jane-roe", is_sender=1, timestamp=opened, chat_id="chat-jane",
        contact_provider_id="ACoAAJane", contact_member_id="111",
    )
    seed_message(db, "bob-1", "bob", is_sender=0, timestamp=newest, chat_id="chat-bob")
    seed_item(db, "agent:jane-roe:20260910", "jane-roe", now=NOW - timedelta(hours=2), chat_id="chat-jane")
    seed_item(db, "agent:bob:20260910", "bob", now=NOW - timedelta(minutes=30), chat_id="chat-bob")
    client = FakeUnipile()
    client.messaging.mailbox = [
        Message(id="ours-1", chat_id="chat-jane", is_sender=1, text="Thanks for connecting", timestamp=opened),
        Message(id="bob-1", chat_id="chat-bob", is_sender=0, sender_id="ACoAABob", text="Sounds good", timestamp=newest),
        Message(id="reply-1", chat_id="chat-jane", is_sender=0, sender_id="ACoAAJane", text="Tell me more", timestamp=newest),
    ]

    summary = jobs.sync(db, client, make_settings(tmp_path), NOW)

    assert db.collection("messages").document("reply-1").get().to_dict()["contact_doc_id"] == "jane-roe"
    assert status(db, "agent:jane-roe:20260910") == queue.CANCELLED
    assert status(db, "agent:bob:20260910") == queue.APPROVED
    assert (summary["replied_contacts"], summary["cancelled"]) == (1, 1)


def test_sync_reads_from_its_cursor_so_a_newer_document_written_elsewhere_hides_no_reply(tmp_path):
    """The sync last got through our opening message to Jane. Since then
    something other than the sync stored a document ten minutes old -- the
    newest in `messages` -- and Jane replied an hour ago. Reading from the
    newest stored document would start past her reply; reading from the
    cursor stores it, cancels her queued follow-up, and moves the cursor to
    her reply."""
    db = FakeFirestore()
    opened, replied = NOW - timedelta(days=6), NOW - timedelta(hours=1)
    seed_contact(db, "jane-roe", firstName="Jane", industry="RCM")
    seed_message(
        db, "ours-1", "jane-roe", is_sender=1, timestamp=opened, chat_id="chat-jane",
        contact_provider_id="ACoAAJane", contact_member_id="111",
    )
    db.collection("runtime_state").document("messages_sync").set({"synced_through": opened})
    seed_message(db, "written-elsewhere", None, is_sender=1, timestamp=NOW - timedelta(minutes=10), chat_id="chat-other")
    seed_item(db, "agent:jane-roe:20260910", "jane-roe", now=NOW - timedelta(hours=2), chat_id="chat-jane")
    client = FakeUnipile()
    client.messaging.mailbox = [
        Message(id="ours-1", chat_id="chat-jane", is_sender=1, text="Thanks for connecting", timestamp=opened),
        Message(
            id="reply-1", chat_id="chat-jane", is_sender=0, sender_id="ACoAAJane",
            text="Tell me more", timestamp=replied,
        ),
    ]

    summary = jobs.sync(db, client, make_settings(tmp_path), NOW)

    assert db.collection("messages").document("reply-1").get().to_dict()["contact_doc_id"] == "jane-roe"
    assert status(db, "agent:jane-roe:20260910") == queue.CANCELLED
    assert messages_sync.read_cursor(db) == replied
    assert (summary["written"], summary["replied_contacts"], summary["cancelled"]) == (1, 1, 1)


def test_a_message_the_service_sent_is_stored_with_its_queue_items_tags(tmp_path):
    """The tick settled `sent-1` with its message id; when the sync stores
    that message, it carries the item's tags. Every other message -- Jane's
    reply here -- is stored with none."""
    db = FakeFirestore()
    opened, followed_up, replied = NOW - timedelta(days=6), NOW - timedelta(hours=3), NOW - timedelta(hours=1)
    seed_contact(db, "jane-roe", firstName="Jane", industry="RCM")
    seed_message(
        db, "ours-1", "jane-roe", is_sender=1, timestamp=opened, chat_id="chat-jane",
        contact_provider_id="ACoAAJane", contact_member_id="111",
    )
    seed_item(db, "agent:jane-roe:20260910", "jane-roe", now=followed_up, chat_id="chat-jane", tags=["recovr", "stage-2"])
    queue.claim(db, "agent:jane-roe:20260910", "tick-owner", followed_up)
    queue.settle(db, "agent:jane-roe:20260910", queue.SENT, now=followed_up, message_id="sent-1")
    client = FakeUnipile()
    client.messaging.mailbox = [
        Message(id="ours-1", chat_id="chat-jane", is_sender=1, text="Thanks for connecting", timestamp=opened),
        Message(id="sent-1", chat_id="chat-jane", is_sender=1, text="Checking back in", timestamp=followed_up),
        Message(id="reply-1", chat_id="chat-jane", is_sender=0, sender_id="ACoAAJane", text="Tell me more", timestamp=replied),
    ]

    jobs.sync(db, client, make_settings(tmp_path), NOW)

    assert db.collection("messages").document("sent-1").get().to_dict()["tags"] == ["recovr", "stage-2"]
    assert db.collection("messages").document("reply-1").get().to_dict()["tags"] == []
