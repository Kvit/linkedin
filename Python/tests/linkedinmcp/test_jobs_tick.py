"""Tests for `linkedinmcp.jobs.tick`: send AT MOST ONE queued message, so
that a message is never sent twice and never silently lost.

The stub client (`fake_unipile.FakeUnipile`) records every `send_message` /
`start_chat` call in `attempts` BEFORE raising any configured error, so each
outcome test asserts two things together: where the queue item ended up, and
that exactly one send was attempted -- whatever LinkedIn answered, a tick
never retries within itself. The contract's send-outcome table (§3) is
walked row by row in the "outcome mapping" section; a later tick is run where
the row's promise is about the NEXT tick (a released item goes out, a paused
or blocked account sends nothing, an `unknown` item is never retried).

Unless a test says otherwise the tick builds its own `RuntimeState` on
`lambda: now`. The two tests that need time to pass inside one tick -- the
lease running short, a pause the guard sees but the tick's own check does
not -- pass a `RuntimeState` on a clock of their own.

A tick that finds nothing due, or is stopped from sending only -- a sends
pause or the message budget (ruling P3-6) -- goes on to fetch one queued
profile (`fetching.fetch_one`, task 3b); "a tick with nothing to send" at
the end covers that wiring, and `test_fetching.py` the fetch itself.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import messages_sync
import profiles
from lib.unipile import errors as unipile_errors
from lib.unipile.models import Chat
from linkedinmcp import clients, decisions, fetch_queue, guards, jobs, ledger, queue, state
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_unipile import (
    CHANNEL,
    GROUP,
    NO_TYPE,
    FakeUnipile,
    chat,
    make_settings,
    profile,
    provider_id_of,
    seed_contact,
    seed_fetch,
    seed_item,
    seed_message,
    store_snapshot,
)

NOW = datetime(2026, 9, 10, 14, 0, 0, tzinfo=UTC)
FOLLOW_UP = "Checking back in on denial recovery -- is it worth a short call?"
INTRO = "Thanks for connecting! I help labs recover denied claims with AI."


class MutableClock:
    """A `clock` callable a test advances by hand (the `test_state.py`
    pattern), for "time passed inside this tick".
    """

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def one_to_one(doc_id) -> Chat:
    """LinkedIn's one-to-one chat with `doc_id`, `chat-{doc_id}`, as
    `get_chat` answers it."""
    return chat(f"chat-{doc_id}", provider_id_of(doc_id))


def follow_up_ready(db, client, doc_id="kim", *, created=NOW - timedelta(hours=1)) -> str:
    """Seed a follow-up every guard passes -- our opening message seven
    days old, no reply, one touch so far, in `chat-{doc_id}`, which
    `client`'s LinkedIn holds as a one-to-one chat with the contact -- and
    return its queue id.
    """
    seed_contact(db, doc_id, industry="RCM", sent_total=1)
    seed_message(
        db, f"{doc_id}-opening", doc_id, is_sender=1, timestamp=NOW - timedelta(days=7), chat_id=f"chat-{doc_id}"
    )
    client.messaging.chats.append(one_to_one(doc_id))
    queue_id = f"agent:{doc_id}:20260910"
    seed_item(db, queue_id, doc_id, now=created, chat_id=f"chat-{doc_id}", text=FOLLOW_UP)
    return queue_id


def intro_ready(db, doc_id="lee", *, created=NOW - timedelta(hours=1), chat_id=None, provider_id="ACoAALee") -> str:
    """Seed an intro every guard passes -- no conversation, never messaged
    -- and return its queue id.
    """
    seed_contact(db, doc_id, industry="RCM", firstName="Lee")
    queue_id = f"intro:{doc_id}"
    seed_item(db, queue_id, doc_id, kind="intro", now=created, chat_id=chat_id, provider_id=provider_id, text=INTRO)
    return queue_id


def intro_refused(db, doc_id, *, created) -> str:
    """Seed an intro the guard refuses (`intro:conversation_exists`: they
    already wrote to us) and return its queue id.
    """
    seed_contact(db, doc_id, industry="RCM")
    seed_message(
        db, f"{doc_id}-question", doc_id, is_sender=0, timestamp=NOW - timedelta(days=3), chat_id=f"chat-{doc_id}"
    )
    queue_id = f"intro:{doc_id}"
    seed_item(db, queue_id, doc_id, kind="intro", now=created, chat_id=None, provider_id=f"ACoAA{doc_id}", text=INTRO)
    return queue_id


def ledger_rows(db) -> list[dict]:
    return [document.to_dict() for document in db.collection("action_log").stream()]


def runtime(db) -> dict:
    return state.RuntimeState(db, clock=lambda: NOW).read()


def alerts(db) -> list[str]:
    return sorted(decision["id"] for decision in decisions.list_decisions(db, limit=100))


def targets(client) -> list:
    return [attempt[1] for attempt in client.messaging.attempts]


def message_reconciles(client) -> list[dict]:
    """The budget's `reconcile` calls for messages. A tick that sends
    nothing -- nothing due, or stopped from sending only -- goes on to
    reconcile the `profile` budget as well, in a call of its own
    (`fetching.fetch_one`)."""
    return [call for call in client.budget.reconcile_calls if "message" in call]


def profile_reconciles(client) -> list[dict]:
    return [call for call in client.budget.reconcile_calls if "profile" in call]


@pytest.fixture(autouse=True)
def classified(monkeypatch) -> list[str]:
    """Replace Gemini for every test here: `clients.gemini_client` returns a
    stand-in and `profiles.classify_profile` a fixed classification. The
    list holds the summary of every classification asked for.

    Autouse because a tick with a queued profile fetch reaches the
    classifier, and `fetching` treats a Gemini client that cannot be built
    as "unclassified" rather than an error -- so a test that forgot this
    stub would not fail, it would call the real client or quietly store an
    unclassified profile.
    """
    summaries = []

    def classify(client, summary):
        summaries.append(summary)
        return profiles.ProfileAnalysis(industry="Pathology", function="Operations", seniority="Director")

    monkeypatch.setattr(clients, "gemini_client", lambda: "a stand-in gemini client")
    monkeypatch.setattr(profiles, "classify_profile", classify)
    return summaries


# =============================================================================
# sending
# =============================================================================


def test_tick_sends_a_follow_up_into_its_chat_and_settles_it_sent(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert client.messaging.attempts == [("send_message", "chat-kim", FOLLOW_UP)]
    item = queue.get(db, queue_id)
    assert (item["status"], item["message_id"], item["sent_at"]) == (queue.SENT, "sent-1", NOW)
    assert ledger_rows(db) == [
        {"kind": "message", "contact_doc_id": "kim", "result": "sent", "queue_id": queue_id, "at": NOW}
    ]
    assert summary["outcome"] == "sent"
    assert (summary["item"], summary["route"], summary["message_id"]) == (queue_id, "send_message", "sent-1")
    json.dumps(summary)
    assert runtime(db).get("tick_lease_owner") is None


def test_tick_opens_a_new_chat_for_an_intro_and_records_the_chat_and_intro_sent_at(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = intro_ready(db)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert client.messaging.attempts == [("start_chat", ("ACoAALee",), INTRO)]
    item = queue.get(db, queue_id)
    assert (item["status"], item["message_id"], item["chat_id"]) == (queue.SENT, "sent-1", "chat-new-1")
    assert db.collection("analysis").document("lee").get().to_dict() == {
        "industry": "RCM",
        "firstName": "Lee",
        "intro_sent_at": NOW,
    }
    assert [row["result"] for row in ledger_rows(db)] == ["sent"]
    assert summary["route"] == "start_chat"


def test_an_intro_that_already_names_a_chat_is_sent_into_that_chat(tmp_path):
    """LinkedIn holds `chat-lee` as a one-to-one chat with the intro's own
    provider id, so it is sent there by `send_message`."""
    db = FakeFirestore()
    client = FakeUnipile(chats=[chat("chat-lee", "ACoAALee")])
    intro_ready(db, chat_id="chat-lee")

    jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert client.messaging.attempts == [("send_message", "chat-lee", INTRO)]


def test_tick_sends_at_most_one_message(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    first = follow_up_ready(db, client, "kim", created=NOW - timedelta(hours=2))
    second = follow_up_ready(db, client, "max", created=NOW - timedelta(hours=1))

    jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert targets(client) == ["chat-kim"]
    assert queue.get(db, first)["status"] == queue.SENT
    assert queue.get(db, second)["status"] == queue.APPROVED


# =============================================================================
# a restriction met by a read
# =============================================================================


def restricted() -> unipile_errors.AccountRestricted:
    return unipile_errors.AccountRestricted(type="errors/account_restricted", status=403, title="Account restricted")


def test_a_restriction_met_by_the_budget_recount_blocks_writes_and_raises_one_alert_before_any_send(tmp_path):
    """The real client raises `AccountRestricted` from a read too. Here the
    recount (no snapshot, so LinkedIn is asked) raises it: the tick blocks
    writes in the state, raises one `restricted` alert, sends nothing, and
    lets the exception out -- its run recorded failed with that class name,
    and the day's `job_failed` alert for tick raised.
    """
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    client.messaging.read_errors["count_messages_sent_since"] = restricted()

    with pytest.raises(unipile_errors.AccountRestricted):
        jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert client.messaging.attempts == []
    assert queue.get(db, queue_id)["status"] == queue.APPROVED
    assert runtime(db).get("writes_blocked_at") == NOW
    assert alerts(db) == ["alert:job_failed:tick:20260910", "alert:restricted:20260910T140000000000Z"]
    assert runtime(db)["tick_lease_owner"] is None
    run = db.collection("runs").document("tick:20260910T140000000000Z").get().to_dict()
    assert (run["ok"], run["error"]) == (False, "AccountRestricted")


def test_a_restriction_met_by_the_requested_sync_blocks_writes_and_raises_one_alert_before_any_send(tmp_path):
    """No `messages_sync` function is replaced: the requested sync's real
    forward pass asks LinkedIn for new messages and gets `AccountRestricted`.
    Writes are blocked, one `restricted` alert is raised, nothing is sent,
    the sync request is kept, and both runs -- the sync's and the tick's --
    are recorded failed, each raising its job's `job_failed` alert for the
    day. The next tick, on a new client whose reads would meet
    the same restriction, finds writes blocked before the requested sync
    (ruling P5-4): it does not read LinkedIn at all -- no exception --
    returns `skipped: writes_blocked`, keeps the request, and there is
    still one alert.
    """
    db = FakeFirestore()
    settings = make_settings(tmp_path)
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    state.RuntimeState(db, clock=lambda: NOW - timedelta(minutes=1)).request_sync()
    client.messaging.read_errors["iter_all_messages"] = restricted()

    with pytest.raises(unipile_errors.AccountRestricted):
        jobs.tick(db, client, settings, NOW)

    assert client.messaging.attempts == []
    assert queue.get(db, queue_id)["status"] == queue.APPROVED
    assert runtime(db).get("writes_blocked_at") == NOW
    assert runtime(db)["sync_requested_at"] == NOW - timedelta(minutes=1)
    assert alerts(db) == [
        "alert:job_failed:sync:20260910",
        "alert:job_failed:tick:20260910",
        "alert:restricted:20260910T140000000000Z",
    ]
    runs = {document.id: document.to_dict() for document in db.collection("runs").stream()}
    assert {run_id: (run["ok"], run["error"]) for run_id, run in runs.items()} == {
        "sync:20260910T140000000000Z": (False, "AccountRestricted"),
        "tick:20260910T140000000000Z": (False, "AccountRestricted"),
    }

    later = FakeUnipile()
    later.messaging.read_errors["iter_all_messages"] = restricted()
    summary = jobs.tick(db, later, settings, NOW + timedelta(minutes=5))

    assert summary["skipped"] == "writes_blocked"
    assert later.messaging.attempts == []
    assert runtime(db)["sync_requested_at"] == NOW - timedelta(minutes=1)
    assert [alert for alert in alerts(db) if alert.startswith("alert:restricted:")] == [
        "alert:restricted:20260910T140000000000Z"
    ]


def test_a_restriction_whose_alert_cannot_be_written_is_recorded_by_the_next_job_to_meet_it(tmp_path, monkeypatch):
    """The alert is written before the block. Writing it fails: the tick
    fails with nothing blocked and no alert, its run recorded with the
    write's error (the `job_failed` alert fails to write the same way, and
    is only logged). The next tick meets the restriction again, raises the
    alert under its own time, and blocks writes -- and, failing, the day's
    `job_failed` alert. Nothing is sent.
    """
    db = FakeFirestore()
    settings = make_settings(tmp_path)
    clients = [FakeUnipile(), FakeUnipile()]
    follow_up_ready(db, clients[0])
    for client in clients:
        client.messaging.read_errors["count_messages_sent_since"] = restricted()
    monkeypatch.setattr(decisions, "raise_alert", raises_firestore_unavailable)

    with pytest.raises(RuntimeError, match="firestore unavailable"):
        jobs.tick(db, clients[0], settings, NOW)

    assert runtime(db).get("writes_blocked_at") is None
    run = db.collection("runs").document("tick:20260910T140000000000Z").get().to_dict()
    assert (run["ok"], run["error"]) == (False, "RuntimeError")

    monkeypatch.undo()
    with pytest.raises(unipile_errors.AccountRestricted):
        jobs.tick(db, clients[1], settings, NOW + timedelta(minutes=5))

    assert runtime(db).get("writes_blocked_at") == NOW + timedelta(minutes=5)
    assert alerts(db) == ["alert:job_failed:tick:20260910", "alert:restricted:20260910T140500000000Z"]
    assert [len(client.messaging.attempts) for client in clients] == [0, 0]


def test_a_dry_run_that_meets_a_restriction_writes_nothing_and_lets_it_out(tmp_path):
    """A dry run writes nothing at all (ruling P2-15) -- not the block, not
    the alert -- and the exception still leaves the job.
    """
    db = FakeFirestore()
    client = FakeUnipile()
    follow_up_ready(db, client)
    client.messaging.read_errors["count_messages_sent_since"] = restricted()
    before = store_snapshot(db)

    with pytest.raises(unipile_errors.AccountRestricted):
        jobs.tick(db, client, make_settings(tmp_path), NOW, dry_run=True)

    assert store_snapshot(db) == before
    assert client.messaging.attempts == []


# =============================================================================
# a claim a crashed tick left behind
# =============================================================================


def test_a_claim_left_sending_by_a_crashed_tick_is_never_sent_again_and_is_later_swept_to_unknown(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    settings = make_settings(tmp_path)
    queue_id = follow_up_ready(db, client)
    queue.claim(db, queue_id, "a-tick-that-crashed", NOW - timedelta(minutes=2))

    first = jobs.tick(db, client, settings, NOW)

    assert first["idle"] is True
    assert client.messaging.attempts == []
    assert queue.get(db, queue_id)["status"] == queue.SENDING

    second = jobs.tick(db, client, settings, NOW + timedelta(minutes=9))

    assert client.messaging.attempts == []
    item = queue.get(db, queue_id)
    assert (item["status"], item["error"]) == (queue.UNKNOWN, "claimed and never settled")
    assert alerts(db) == [f"alert:unknown_send:{queue_id}"]
    assert second["stale_swept"] == 1


# =============================================================================
# the outcome mapping (contract §3), one row at a time
# =============================================================================


def released_before_any_request():
    return [
        pytest.param(
            lambda: unipile_errors.BudgetExhausted(type="local/budget_exhausted", title="message budget spent"),
            id="BudgetExhausted",
        ),
        pytest.param(
            lambda: unipile_errors.CircuitOpen(type="local/circuit_open", title="writes are blocked"),
            id="CircuitOpen",
        ),
    ]


@pytest.mark.parametrize("make_error", released_before_any_request())
def test_a_send_refused_before_any_request_releases_the_claim_and_the_next_tick_sends_it(tmp_path, make_error):
    db = FakeFirestore()
    client = FakeUnipile()
    settings = make_settings(tmp_path)
    queue_id = follow_up_ready(db, client)
    error = make_error()
    client.messaging.send_error = error

    summary = jobs.tick(db, client, settings, NOW)

    item = queue.get(db, queue_id)
    assert (item["status"], item["sending_at"], item["lease_owner"]) == (queue.APPROVED, None, None)
    assert item["error"] == type(error).__name__
    assert len(client.messaging.attempts) == 1
    assert ledger_rows(db) == []
    assert alerts(db) == []
    assert runtime(db).get("sends_paused_until") is None
    assert runtime(db).get("writes_blocked_at") is None
    assert (summary["outcome"], summary["error"]) == ("released", type(error).__name__)

    client.messaging.send_error = None
    jobs.tick(db, client, settings, NOW + timedelta(minutes=5))

    assert targets(client) == ["chat-kim", "chat-kim"]
    assert queue.get(db, queue_id)["status"] == queue.SENT


@pytest.mark.parametrize(
    "retry_after, paused_for",
    [(120.0, timedelta(seconds=120)), (None, timedelta(hours=1))],
    ids=["with-retry-after", "without-retry-after"],
)
def test_a_rate_limit_releases_the_claim_and_pauses_sends_for_its_retry_after_or_an_hour(
    tmp_path, retry_after, paused_for
):
    db = FakeFirestore()
    client = FakeUnipile()
    settings = make_settings(tmp_path)
    queue_id = follow_up_ready(db, client)
    client.messaging.send_error = unipile_errors.RateLimited(
        type="errors/too_many_requests", status=429, title="Too many requests", retry_after=retry_after
    )

    summary = jobs.tick(db, client, settings, NOW)

    assert queue.get(db, queue_id)["status"] == queue.APPROVED
    assert len(client.messaging.attempts) == 1
    assert runtime(db)["sends_paused_until"] == NOW + paused_for
    assert ledger_rows(db) == []
    assert alerts(db) == []
    assert summary["paused_until"] == (NOW + paused_for).isoformat()
    json.dumps(summary)

    client.messaging.send_error = None
    later = jobs.tick(db, client, settings, NOW + paused_for - timedelta(seconds=1))

    assert later["skipped"] == "sends_paused"
    assert len(client.messaging.attempts) == 1


def test_a_short_retry_after_is_counted_from_the_state_clock_when_it_is_ahead_of_now(tmp_path):
    """Ruling P5-4: the tick's `now` is when it started; the state's clock
    is three minutes on by the time LinkedIn answers 429 with a 60-second
    Retry-After. The pause is counted from the later of the two, so it is
    still in force by the state's own clock when it is written."""
    db = FakeFirestore()
    client = FakeUnipile()
    follow_up_ready(db, client)
    ahead = state.RuntimeState(db, clock=lambda: NOW + timedelta(minutes=3))
    client.messaging.send_error = unipile_errors.RateLimited(
        type="errors/too_many_requests", status=429, title="Too many requests", retry_after=60.0
    )

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW, state=ahead)

    until = NOW + timedelta(minutes=3, seconds=60)
    assert runtime(db)["sends_paused_until"] == until
    assert ahead.sends_paused_until() == until
    assert summary["paused_until"] == until.isoformat()


def test_a_restricted_account_releases_the_claim_blocks_writes_and_raises_one_alert(tmp_path):
    """`AccountRestricted` is a `PermissionDenied`; its row -- not the
    generic 403's `failed` -- is the one that applies.
    """
    db = FakeFirestore()
    client = FakeUnipile()
    settings = make_settings(tmp_path)
    queue_id = follow_up_ready(db, client)
    client.messaging.send_error = unipile_errors.AccountRestricted(
        type="errors/account_restricted", status=403, title="Account restricted"
    )

    summary = jobs.tick(db, client, settings, NOW)

    assert queue.get(db, queue_id)["status"] == queue.APPROVED
    assert len(client.messaging.attempts) == 1
    assert runtime(db)["writes_blocked_at"] == NOW
    assert alerts(db) == ["alert:restricted:20260910T140000000000Z"]
    assert ledger_rows(db) == []
    assert summary["outcome"] == "released"

    client.messaging.send_error = None
    later = jobs.tick(db, client, settings, NOW + timedelta(minutes=5))

    assert later["skipped"] == "writes_blocked"
    assert len(client.messaging.attempts) == 1
    assert alerts(db) == ["alert:restricted:20260910T140000000000Z"]


def test_a_second_restriction_the_same_day_after_a_human_cleared_the_block_raises_a_second_alert(tmp_path):
    """The `restricted` alert is keyed by the moment the restriction was
    seen, not by the day. A human clears the block; an hour later the send
    is refused again, and that is a second alert. Each tick gets a new
    client, as each job does.
    """
    db = FakeFirestore()
    settings = make_settings(tmp_path)
    clients = [FakeUnipile(), FakeUnipile(chats=[one_to_one("kim")])]
    follow_up_ready(db, clients[0])
    for client in clients:
        client.messaging.send_error = restricted()

    jobs.tick(db, clients[0], settings, NOW)
    state.RuntimeState(db, clock=lambda: NOW).unblock_writes()
    jobs.tick(db, clients[1], settings, NOW + timedelta(hours=1))

    assert [len(client.messaging.attempts) for client in clients] == [1, 1]
    assert alerts(db) == ["alert:restricted:20260910T140000000000Z", "alert:restricted:20260910T150000000000Z"]
    assert runtime(db)["writes_blocked_at"] == NOW + timedelta(hours=1)


@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(
            lambda: unipile_errors.AccountDisconnected(type="errors/disconnected_account", status=401, title="x"),
            id="AccountDisconnected",
        ),
        pytest.param(
            lambda: unipile_errors.AuthenticationError(status=401, title="Unauthorized"), id="AuthenticationError"
        ),
    ],
)
def test_a_disconnected_account_releases_the_claim_pauses_an_hour_and_raises_one_alert(tmp_path, make_error):
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    client.messaging.send_error = make_error()

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert queue.get(db, queue_id)["status"] == queue.APPROVED
    assert len(client.messaging.attempts) == 1
    assert runtime(db)["sends_paused_until"] == NOW + timedelta(hours=1)
    assert alerts(db) == ["alert:disconnected:20260910"]
    assert ledger_rows(db) == []
    assert summary["outcome"] == "released"


@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(lambda: unipile_errors.UnprocessableError(status=422, title="x"), id="UnprocessableError"),
        pytest.param(
            lambda: unipile_errors.NoConnectionWithRecipient(type="errors/no_connection_with_recipient", status=422),
            id="NoConnectionWithRecipient",
        ),
        pytest.param(lambda: unipile_errors.NotFound(status=404, title="no such chat"), id="NotFound"),
        pytest.param(lambda: unipile_errors.PermissionDenied(status=403, title="forbidden"), id="PermissionDenied"),
        pytest.param(
            lambda: unipile_errors.FeatureNotSubscribed(type="errors/feature_not_subscribed", status=403),
            id="FeatureNotSubscribed",
        ),
    ],
)
def test_a_refusal_by_linkedin_settles_the_item_failed_and_stops(tmp_path, make_error):
    db = FakeFirestore()
    client = FakeUnipile()
    first = follow_up_ready(db, client, "kim", created=NOW - timedelta(hours=2))
    second = follow_up_ready(db, client, "max", created=NOW - timedelta(hours=1))
    error = make_error()
    client.messaging.send_error = error

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    item = queue.get(db, first)
    assert (item["status"], item["error"]) == (queue.FAILED, type(error).__name__)
    assert [row["result"] for row in ledger_rows(db)] == ["failed"]
    assert targets(client) == ["chat-kim"]
    assert queue.get(db, second)["status"] == queue.APPROVED
    assert alerts(db) == []
    assert runtime(db).get("sends_paused_until") is None
    assert (summary["outcome"], summary["error"]) == ("failed", type(error).__name__)


@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(lambda: unipile_errors.ServerError(status=502, title="Bad gateway"), id="ServerError"),
        pytest.param(lambda: httpx.ReadTimeout("read timed out"), id="ReadTimeout"),
        pytest.param(lambda: httpx.ConnectError("connection refused"), id="ConnectError"),
        pytest.param(lambda: RuntimeError("unexpected"), id="RuntimeError"),
        pytest.param(lambda: unipile_errors.UnipileError(status=400, title="Bad request"), id="UnipileError-400"),
    ],
)
def test_an_unknowable_outcome_settles_unknown_with_one_alert_and_is_never_retried(tmp_path, make_error):
    db = FakeFirestore()
    client = FakeUnipile()
    settings = make_settings(tmp_path)
    first = follow_up_ready(db, client, "kim", created=NOW - timedelta(hours=2))
    second = follow_up_ready(db, client, "max", created=NOW - timedelta(hours=1))
    error = make_error()
    client.messaging.send_error = error

    summary = jobs.tick(db, client, settings, NOW)

    item = queue.get(db, first)
    assert (item["status"], item["error"]) == (queue.UNKNOWN, type(error).__name__)
    assert [row["result"] for row in ledger_rows(db)] == ["unknown"]
    assert alerts(db) == [f"alert:unknown_send:{first}"]
    assert targets(client) == ["chat-kim"]
    assert queue.get(db, second)["status"] == queue.APPROVED
    assert (summary["outcome"], summary["error"]) == ("unknown", type(error).__name__)

    client.messaging.send_error = None
    jobs.tick(db, client, settings, NOW + timedelta(minutes=5))

    assert targets(client) == ["chat-kim", "chat-max"]
    assert queue.get(db, first)["status"] == queue.UNKNOWN


def raises_firestore_unavailable(*args, **kwargs):
    raise RuntimeError("firestore unavailable")


def test_an_unknown_outcome_whose_alert_cannot_be_written_stays_sending_and_is_swept_with_its_alert(
    tmp_path, monkeypatch
):
    """The send raised a 502 and writing the `unknown_send` alert fails: the
    tick raises, and the item is still `sending` with no ledger row. A later
    tick's sweep raises the alert and moves the item to `unknown`. One send
    attempt in all.
    """
    db = FakeFirestore()
    client = FakeUnipile()
    settings = make_settings(tmp_path)
    queue_id = follow_up_ready(db, client)
    client.messaging.send_error = unipile_errors.ServerError(status=502, title="Bad gateway")
    monkeypatch.setattr(decisions, "raise_alert", raises_firestore_unavailable)

    with pytest.raises(RuntimeError, match="firestore unavailable"):
        jobs.tick(db, client, settings, NOW)

    assert queue.get(db, queue_id)["status"] == queue.SENDING
    assert ledger_rows(db) == []
    assert runtime(db)["tick_lease_owner"] is None

    monkeypatch.undo()
    client.messaging.send_error = None
    jobs.tick(db, client, settings, NOW + timedelta(minutes=11))

    assert queue.get(db, queue_id)["status"] == queue.UNKNOWN
    assert alerts(db) == [f"alert:unknown_send:{queue_id}"]
    assert len(client.messaging.attempts) == 1


def test_an_unknown_outcome_whose_settle_fails_after_the_alert_is_swept_without_a_second_alert(tmp_path, monkeypatch):
    """The send raised a 502 and recording `unknown` fails (only that write
    -- any other `settle` goes through): the tick raises with the alert
    already stored and the item still `sending`. A later tick's sweep moves
    it to `unknown`; one `unknown_send` alert and one send attempt in all
    (and the failed run's `job_failed` alert for the day).
    """
    db = FakeFirestore()
    client = FakeUnipile()
    settings = make_settings(tmp_path)
    queue_id = follow_up_ready(db, client)
    client.messaging.send_error = unipile_errors.ServerError(status=502, title="Bad gateway")
    real_settle = queue.settle

    def settle_fails(db_arg, queue_id_arg, status, *, now, message_id=None, chat_id=None, error=None):
        if status == queue.UNKNOWN:
            raise RuntimeError("firestore unavailable")
        real_settle(db_arg, queue_id_arg, status, now=now, message_id=message_id, chat_id=chat_id, error=error)

    monkeypatch.setattr(queue, "settle", settle_fails)

    with pytest.raises(RuntimeError, match="firestore unavailable"):
        jobs.tick(db, client, settings, NOW)

    assert queue.get(db, queue_id)["status"] == queue.SENDING
    assert alerts(db) == ["alert:job_failed:tick:20260910", f"alert:unknown_send:{queue_id}"]

    monkeypatch.undo()
    client.messaging.send_error = None
    jobs.tick(db, client, settings, NOW + timedelta(minutes=11))

    assert queue.get(db, queue_id)["status"] == queue.UNKNOWN
    assert alerts(db) == ["alert:job_failed:tick:20260910", f"alert:unknown_send:{queue_id}"]
    assert len(client.messaging.attempts) == 1


# =============================================================================
# guards
# =============================================================================


def test_a_guard_refusal_skips_the_item_and_the_tick_sends_the_next_due_one(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    refused = intro_refused(db, "nan", created=NOW - timedelta(hours=2))
    sendable = follow_up_ready(db, client, "kim", created=NOW - timedelta(hours=1))

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    item = queue.get(db, refused)
    assert (item["status"], item["skip_reason"]) == (queue.SKIPPED, "intro:conversation_exists")
    assert client.messaging.attempts == [("send_message", "chat-kim", FOLLOW_UP)]
    assert queue.get(db, sendable)["status"] == queue.SENT
    assert summary["items_skipped"] == 1


@pytest.mark.parametrize("guard", ["validate_text", "check_send"])
def test_a_guard_that_raises_skips_that_item_with_the_exception_class_and_moves_on(tmp_path, monkeypatch, guard):
    """Either guard can raise on a malformed value in a stored document --
    a naive datetime, say. Here the named guard raises on its first call
    (the older item, Oli's) and behaves normally after. Oli's item is
    skipped as `guard_error:ValueError` and Kim's still goes out, so one bad
    document cannot fail every later tick.
    """
    db = FakeFirestore()
    client = FakeUnipile()
    broken = follow_up_ready(db, client, "oli", created=NOW - timedelta(hours=2))
    sendable = follow_up_ready(db, client, "kim", created=NOW - timedelta(hours=1))
    real_guard = getattr(guards, guard)
    calls = []

    def raises_once(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise ValueError("local_date: `moment` must be timezone-aware")
        return real_guard(*args, **kwargs)

    monkeypatch.setattr(guards, guard, raises_once)

    jobs.tick(db, client, make_settings(tmp_path), NOW)

    item = queue.get(db, broken)
    assert (item["status"], item["skip_reason"]) == (queue.SKIPPED, "guard_error:ValueError")
    assert queue.get(db, sendable)["status"] == queue.SENT
    assert targets(client) == ["chat-kim"]


def test_a_pause_only_the_guard_sees_leaves_the_item_approved_for_the_next_tick(tmp_path):
    """The tick's own pause check reads the state's clock; `check_send`
    compares with `now`. With the state's clock ten seconds past `now` and a
    pause ending in between, the tick's check passes and the guard refuses
    with `state:sends_paused` -- a refusal about the account, not the item,
    so the item stays `approved` instead of being skipped for good. A sends
    pause is about messages only (ruling P3-6), so the tick goes on to the
    fetch path, and with nothing queued that is `idle`; the summary's
    `until` is still the sends pause.
    """
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    ahead = state.RuntimeState(db, clock=lambda: NOW + timedelta(seconds=10))
    ahead.pause_sends(NOW + timedelta(seconds=5), "a pause that is just ending")

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW, state=ahead)

    assert queue.get(db, queue_id)["status"] == queue.APPROVED
    assert client.messaging.attempts == []
    assert (summary["skipped"], summary["until"]) == ("sends_paused", (NOW + timedelta(seconds=5)).isoformat())
    assert summary["fetch"] == "idle"


def test_a_follow_up_naming_no_chat_is_skipped_before_any_claim(tmp_path):
    """Every guard passes -- there IS a stored conversation -- but the item
    names no chat, and only an intro may open one.
    """
    db = FakeFirestore()
    client = FakeUnipile()
    seed_contact(db, "pia", industry="RCM", sent_total=1)
    seed_message(db, "pia-opening", "pia", is_sender=1, timestamp=NOW - timedelta(days=7), chat_id="chat-pia")
    seed_item(db, "agent:pia:20260910", "pia", now=NOW - timedelta(hours=1), chat_id=None)

    jobs.tick(db, client, make_settings(tmp_path), NOW)

    item = queue.get(db, "agent:pia:20260910")
    assert (item["status"], item["skip_reason"]) == (queue.SKIPPED, "item:no_chat_id")
    assert client.messaging.attempts == []


def test_an_intro_with_neither_a_chat_nor_a_provider_id_is_skipped_before_any_claim(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = intro_ready(db, provider_id=None)

    jobs.tick(db, client, make_settings(tmp_path), NOW)

    item = queue.get(db, queue_id)
    assert (item["status"], item["skip_reason"]) == (queue.SKIPPED, "item:no_provider_id")
    assert client.messaging.attempts == []


def test_tick_gives_up_after_ten_refused_items(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    ids = [intro_refused(db, f"c{n:02d}", created=NOW - timedelta(hours=12 - n)) for n in range(11)]

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert [queue.get(db, queue_id)["status"] for queue_id in ids] == [queue.SKIPPED] * 10 + [queue.APPROVED]
    assert summary["stopped"] == "max_attempts"
    assert client.messaging.attempts == []


# =============================================================================
# the chat is checked with LinkedIn before the claim (final review FI2)
# =============================================================================


def never_claimed(item) -> bool:
    """`queue.claim` stamps `sending_at` and `lease_owner`; neither is set."""
    return item.get("sending_at") is None and item.get("lease_owner") is None


def test_a_one_to_one_chat_with_the_contact_is_checked_with_linkedin_and_then_sent(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert client.messaging.get_chat_calls == ["chat-kim"]
    assert client.messaging.attempts == [("send_message", "chat-kim", FOLLOW_UP)]
    assert queue.get(db, queue_id)["status"] == queue.SENT
    assert summary["outcome"] == "sent"


@pytest.mark.parametrize(
    "linkedin_chat",
    [
        pytest.param(chat("chat-kim", "ACoAA-someone-else"), id="someone-elses-chat"),
        pytest.param(chat("chat-kim", None), id="no-attendee"),
        pytest.param(chat("chat-kim", provider_id_of("kim"), type=GROUP), id="group-chat-naming-the-contact"),
        pytest.param(chat("chat-kim", provider_id_of("kim"), type=CHANNEL), id="channel"),
        pytest.param(chat("chat-kim", provider_id_of("kim"), type=NO_TYPE), id="type-missing"),
    ],
)
def test_a_chat_that_is_not_one_to_one_with_the_contact_is_skipped_before_any_claim(tmp_path, linkedin_chat):
    """FI2(b): LinkedIn says the item's chat is someone else's, has no
    attendee, is a group or a channel -- even one whose attendee is the
    contact -- or carries no chat type at all. The item is skipped with
    `item:chat_mismatch` and never claimed: no `send_message` call, no
    ledger row, no alert."""
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    client.messaging.chats = [linkedin_chat]

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    item = queue.get(db, queue_id)
    assert (item["status"], item["skip_reason"]) == (queue.SKIPPED, "item:chat_mismatch")
    assert never_claimed(item)
    assert client.messaging.get_chat_calls == ["chat-kim"]
    assert client.messaging.attempts == []
    assert ledger_rows(db) == []
    assert alerts(db) == []
    assert summary["items_skipped"] == 1


def test_after_a_chat_mismatch_the_tick_sends_the_next_due_item(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    mismatched = follow_up_ready(db, client, "kim", created=NOW - timedelta(hours=2))
    sendable = follow_up_ready(db, client, "max", created=NOW - timedelta(hours=1))
    client.messaging.chats = [chat("chat-kim", "ACoAA-someone-else"), one_to_one("max")]

    jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert queue.get(db, mismatched)["skip_reason"] == "item:chat_mismatch"
    assert queue.get(db, sendable)["status"] == queue.SENT
    assert client.messaging.attempts == [("send_message", "chat-max", FOLLOW_UP)]


def test_a_contact_whose_stored_messages_name_no_provider_id_is_skipped_without_asking_linkedin(tmp_path):
    """Who the contact is on LinkedIn comes from their stored messages'
    `contact_provider_id`. None of theirs carries one, so nothing can be
    checked: the item is skipped with `item:chat_mismatch` before any
    LinkedIn call."""
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    db.collection("messages").document("kim-opening").set({"contact_provider_id": None}, merge=True)

    jobs.tick(db, client, make_settings(tmp_path), NOW)

    item = queue.get(db, queue_id)
    assert (item["status"], item["skip_reason"]) == (queue.SKIPPED, "item:chat_mismatch")
    assert client.messaging.get_chat_calls == []
    assert client.messaging.attempts == []


def test_a_contact_whose_stored_messages_disagree_on_the_provider_id_is_skipped(tmp_path):
    """The provider id is read across ALL the contact's usable messages,
    in every chat. Two different ones mean the stored history cannot say
    who the contact is, so the item is skipped with `item:chat_mismatch`,
    though LinkedIn holds `chat-kim` as a one-to-one chat with one of them."""
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    seed_message(
        db, "kim-elsewhere", "kim", is_sender=1, timestamp=NOW - timedelta(days=20), chat_id="chat-other",
        contact_provider_id="ACoAA-another-member",
    )

    jobs.tick(db, client, make_settings(tmp_path), NOW)

    item = queue.get(db, queue_id)
    assert (item["status"], item["skip_reason"]) == (queue.SKIPPED, "item:chat_mismatch")
    assert client.messaging.attempts == []


@pytest.mark.parametrize("flag", ["is_event", "deleted"])
def test_an_event_or_deleted_message_does_not_change_who_the_contact_is(tmp_path, flag):
    """The provider id comes from USABLE messages only -- the ones the
    guards reason over. An event (or a deleted message) in a group chat,
    attributed to the contact but carrying another member's provider id,
    does not make the contact ambiguous: their own chat is verified and
    the follow-up is sent there."""
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    seed_message(
        db, "group-event", "kim", is_sender=0, timestamp=NOW - timedelta(days=1), chat_id="chat-GROUP",
        contact_provider_id="ACoAA-group-member", **{flag: 1},
    )

    jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert queue.get(db, queue_id)["status"] == queue.SENT
    assert client.messaging.attempts == [("send_message", "chat-kim", FOLLOW_UP)]


def test_an_intro_naming_a_chat_that_is_not_with_its_provider_id_is_skipped(tmp_path):
    """An intro sent into a chat it names is checked against its own
    `provider_id` -- the relation's -- since an intro has no conversation
    to read one from. LinkedIn's `chat-lee` is someone else's."""
    db = FakeFirestore()
    client = FakeUnipile(chats=[chat("chat-lee", "ACoAA-someone-else")])
    queue_id = intro_ready(db, chat_id="chat-lee")

    jobs.tick(db, client, make_settings(tmp_path), NOW)

    item = queue.get(db, queue_id)
    assert (item["status"], item["skip_reason"]) == (queue.SKIPPED, "item:chat_mismatch")
    assert client.messaging.attempts == []


def test_an_intro_that_opens_a_new_chat_asks_linkedin_about_no_chat(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    intro_ready(db)

    jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert client.messaging.get_chat_calls == []
    assert client.messaging.attempts == [("start_chat", ("ACoAALee",), INTRO)]


@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(lambda: unipile_errors.ServerError(status=502, title="Bad gateway"), id="ServerError"),
        pytest.param(lambda: httpx.ReadTimeout("read timed out"), id="ReadTimeout"),
        pytest.param(
            lambda: unipile_errors.RateLimited(type="errors/too_many_requests", status=429, title="x"),
            id="RateLimited",
        ),
    ],
)
def test_a_chat_check_that_raises_stops_the_tick_and_leaves_the_item_approved(tmp_path, make_error):
    """`get_chat` raising for a reason that is not about this chat -- an
    outage, a timeout, a rate limit -- is a failure before the claim, like
    the budget recount's: the exception leaves the tick, whose run is
    recorded failed with its class name; the item is still `approved` and
    was never claimed, nothing was sent, and the lease is released."""
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    error = make_error()
    client.messaging.read_errors["get_chat"] = error

    with pytest.raises(type(error)):
        jobs.tick(db, client, make_settings(tmp_path), NOW)

    item = queue.get(db, queue_id)
    assert item["status"] == queue.APPROVED
    assert never_claimed(item)
    assert client.messaging.attempts == []
    assert runtime(db)["tick_lease_owner"] is None
    run = db.collection("runs").document("tick:20260910T140000000000Z").get().to_dict()
    assert (run["ok"], run["error"]) == (False, type(error).__name__)


def test_a_chat_linkedin_no_longer_has_is_skipped_and_the_tick_sends_the_next_due_item(tmp_path):
    """`get_chat` answers 404 for `chat-kim`: the chat is gone, and no
    later tick could send into it. The item is skipped with
    `item:chat_unavailable`, never claimed, and the tick goes on to send
    the next due item -- one dead chat does not hold up the queue."""
    db = FakeFirestore()
    client = FakeUnipile()
    gone = follow_up_ready(db, client, "kim", created=NOW - timedelta(hours=2))
    sendable = follow_up_ready(db, client, "max", created=NOW - timedelta(hours=1))
    client.messaging.chats = [one_to_one("max")]

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    item = queue.get(db, gone)
    assert (item["status"], item["skip_reason"]) == (queue.SKIPPED, "item:chat_unavailable")
    assert never_claimed(item)
    assert queue.get(db, sendable)["status"] == queue.SENT
    assert client.messaging.attempts == [("send_message", "chat-max", FOLLOW_UP)]
    assert summary["items_skipped"] == 1


@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(lambda: unipile_errors.UnprocessableError(status=422, title="x"), id="Unprocessable"),
        pytest.param(lambda: unipile_errors.PermissionDenied(status=403, title="x"), id="PermissionDenied"),
    ],
)
def test_a_chat_linkedin_refuses_to_show_is_skipped_before_any_claim(tmp_path, make_error):
    """A 422 or a 403 that is not a restriction is about this chat, as the
    same answers to a send are (`_send`: `failed`). The item is skipped
    with `item:chat_unavailable` before any claim: no send, no ledger row,
    no alert, and the tick's run succeeds."""
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    client.messaging.read_errors["get_chat"] = make_error()

    jobs.tick(db, client, make_settings(tmp_path), NOW)

    item = queue.get(db, queue_id)
    assert (item["status"], item["skip_reason"]) == (queue.SKIPPED, "item:chat_unavailable")
    assert never_claimed(item)
    assert client.messaging.attempts == []
    assert ledger_rows(db) == []
    assert alerts(db) == []
    run = db.collection("runs").document("tick:20260910T140000000000Z").get().to_dict()
    assert run["ok"] is True


def test_the_dry_tick_skips_a_chat_linkedin_no_longer_has(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    gone = follow_up_ready(db, client, "kim", created=NOW - timedelta(hours=2))
    sendable = follow_up_ready(db, client, "max", created=NOW - timedelta(hours=1))
    client.messaging.chats = [one_to_one("max")]
    before = store_snapshot(db)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW, dry_run=True)

    assert summary["would_skip"] == {gone: "item:chat_unavailable"}
    assert summary["would_send"] == sendable
    assert store_snapshot(db) == before


def test_a_restriction_met_by_the_chat_check_blocks_writes_and_sends_nothing(tmp_path):
    """`get_chat` is a read, and the real client raises `AccountRestricted`
    from a read too: the job wrapper blocks writes and raises the one
    `restricted` alert; the item stays `approved`, never claimed."""
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    client.messaging.read_errors["get_chat"] = restricted()

    with pytest.raises(unipile_errors.AccountRestricted):
        jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert never_claimed(queue.get(db, queue_id))
    assert client.messaging.attempts == []
    assert runtime(db)["writes_blocked_at"] == NOW
    assert "alert:restricted:20260910T140000000000Z" in alerts(db)


def test_a_chat_check_that_uses_up_the_lease_stops_the_tick_before_claiming(tmp_path):
    """The chat is checked before the lease floor, so the floor still
    stands between the claim and the send: LinkedIn answers `get_chat` so
    slowly that 25 seconds of lease are left -- under the 30 a write needs
    -- and the tick stops without claiming."""
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    clock = MutableClock(NOW)
    runtime_state = state.RuntimeState(db, clock=clock)
    lookup = client.messaging.get_chat

    def slow_get_chat(chat_id):
        clock.now = NOW + timedelta(seconds=state.LEASE_SECONDS - 25)
        return lookup(chat_id)

    client.messaging.get_chat = slow_get_chat

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW, state=runtime_state)

    assert summary["stopped"] == "lease_short"
    assert client.messaging.get_chat_calls == ["chat-kim"]
    assert never_claimed(queue.get(db, queue_id))
    assert client.messaging.attempts == []


def test_the_dry_tick_checks_the_chat_as_the_real_tick_does(tmp_path):
    """Dry runs may read from LinkedIn: the dry tick asks `get_chat` too,
    reports the mismatched item under `would_skip` and the next one as
    `would_send`, and writes nothing."""
    db = FakeFirestore()
    client = FakeUnipile()
    mismatched = follow_up_ready(db, client, "kim", created=NOW - timedelta(hours=2))
    sendable = follow_up_ready(db, client, "max", created=NOW - timedelta(hours=1))
    client.messaging.chats = [chat("chat-kim", provider_id_of("kim"), type=GROUP), one_to_one("max")]
    before = store_snapshot(db)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW, dry_run=True)

    assert summary["would_skip"] == {mismatched: "item:chat_mismatch"}
    assert summary["would_send"] == sendable
    assert client.messaging.get_chat_calls == ["chat-kim", "chat-max"]
    assert store_snapshot(db) == before
    assert client.messaging.attempts == []


# =============================================================================
# a tick that does not send
# =============================================================================


def test_a_busy_lease_skips_the_tick_without_sending(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    follow_up_ready(db, client)
    owner = state.RuntimeState(db, clock=lambda: NOW).acquire_tick_lease()

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert summary == {"skipped": "busy"}
    assert client.messaging.attempts == []
    assert runtime(db)["tick_lease_owner"] == owner
    run = db.collection("runs").document("tick:20260910T140000000000Z").get().to_dict()
    assert (run["ok"], run["summary"]) == (True, {"skipped": "busy"})


def test_blocked_writes_skip_the_tick(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    state.RuntimeState(db, clock=lambda: NOW).block_writes("restricted yesterday")

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert summary["skipped"] == "writes_blocked"
    assert client.messaging.attempts == []
    assert queue.get(db, queue_id)["status"] == queue.APPROVED


def test_paused_sends_skip_the_tick_and_say_until_when(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    follow_up_ready(db, client)
    state.RuntimeState(db, clock=lambda: NOW).pause_sends(NOW + timedelta(minutes=30), "a human paused")

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert (summary["skipped"], summary["until"]) == ("sends_paused", (NOW + timedelta(minutes=30)).isoformat())
    assert client.messaging.attempts == []


def test_a_budget_spent_after_reconciling_skips_sending(tmp_path):
    """Nothing is sent. The profile budget is its own (ruling P3-6), so the
    tick goes on to the fetch path, which reconciles the `profile` count in
    a call of its own -- exactly these two reconciles, messages first."""
    db = FakeFirestore()
    client = FakeUnipile(sent_24h=7, message_limit=7)
    follow_up_ready(db, client)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert (summary["skipped"], summary["sent_24h"]) == ("budget", 7)
    assert client.budget.reconcile_calls == [{"message": 7}, {"profile": 0}]
    assert client.messaging.attempts == []


def test_a_tick_with_nothing_due_is_idle(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    seed_contact(db, "kim", industry="RCM")
    seed_item(db, "agent:kim:20260911", "kim", now=NOW, due_at=NOW + timedelta(hours=1))

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert summary["idle"] is True
    assert client.messaging.attempts == []


# =============================================================================
# the lease
# =============================================================================


def test_a_lease_too_short_for_a_write_stops_the_tick_before_claiming(tmp_path):
    """The budget recount "takes" all but 25 of the lease's seconds
    (`state.LEASE_SECONDS`), leaving 25 -- under the 30 a LinkedIn write
    needs.
    """
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    clock = MutableClock(NOW)
    runtime_state = state.RuntimeState(db, clock=clock)
    count = client.messaging.count_messages_sent_since

    def slow_count(cutoff):
        clock.now = NOW + timedelta(seconds=state.LEASE_SECONDS - 25)
        return count(cutoff)

    client.messaging.count_messages_sent_since = slow_count

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW, state=runtime_state)

    assert summary["stopped"] == "lease_short"
    assert "fetch" not in summary
    assert queue.get(db, queue_id)["status"] == queue.APPROVED
    assert client.messaging.attempts == []
    assert runtime_state.read()["tick_lease_owner"] is None


def test_the_lease_is_released_when_the_send_raises(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile()
    follow_up_ready(db, client)
    client.messaging.send_error = RuntimeError("the send blew up")

    jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert (runtime(db)["tick_lease_owner"], runtime(db)["tick_lease_until"]) == (None, None)


def test_a_send_that_cannot_be_settled_stays_sending_and_is_swept_to_unknown_never_resent(tmp_path, monkeypatch):
    """LinkedIn accepted the message; recording `sent` failed (only that
    write fails -- any other `settle` goes through). The exception leaves
    the tick, after releasing the lease and recording a failed run; the item
    stays `sending` -- not `approved`, which would send it again, and not
    settled `unknown` as though LinkedIn had raised -- and a later tick's
    sweep moves it to `unknown` with an alert. The failed run raised the
    day's `job_failed` alert for tick.
    """
    db = FakeFirestore()
    client = FakeUnipile()
    settings = make_settings(tmp_path)
    queue_id = follow_up_ready(db, client)
    real_settle = queue.settle

    def settle_fails(db_arg, queue_id_arg, status, *, now, message_id=None, chat_id=None, error=None):
        if status == queue.SENT:
            raise RuntimeError("firestore unavailable")
        real_settle(db_arg, queue_id_arg, status, now=now, message_id=message_id, chat_id=chat_id, error=error)

    monkeypatch.setattr(queue, "settle", settle_fails)

    with pytest.raises(RuntimeError, match="firestore unavailable"):
        jobs.tick(db, client, settings, NOW)

    assert len(client.messaging.attempts) == 1
    assert queue.get(db, queue_id)["status"] == queue.SENDING
    assert runtime(db)["tick_lease_owner"] is None
    run = db.collection("runs").document("tick:20260910T140000000000Z").get().to_dict()
    assert (run["ok"], run["error"]) == (False, "RuntimeError")

    monkeypatch.undo()
    jobs.tick(db, client, settings, NOW + timedelta(minutes=11))

    assert queue.get(db, queue_id)["status"] == queue.UNKNOWN
    assert alerts(db) == ["alert:job_failed:tick:20260910", f"alert:unknown_send:{queue_id}"]
    assert len(client.messaging.attempts) == 1


# =============================================================================
# the budget (ruling P2-9)
# =============================================================================


def test_a_fresh_budget_snapshot_plus_the_ledger_since_it_is_used_without_asking_linkedin(tmp_path):
    """Snapshot of 5, ten minutes old; since then one `sent` and one
    `unknown` row count, a `failed` row does not, and a `sent` row from
    before the snapshot is already inside it.
    """
    db = FakeFirestore()
    client = FakeUnipile(sent_24h=99)
    state.RuntimeState(db, clock=lambda: NOW).store_budget_snapshot(5, NOW - timedelta(minutes=10))
    ledger.record(db, "message", "a", "sent", NOW - timedelta(minutes=5))
    ledger.record(db, "message", "b", "unknown", NOW - timedelta(minutes=3))
    ledger.record(db, "message", "c", "failed", NOW - timedelta(minutes=4))
    ledger.record(db, "message", "d", "sent", NOW - timedelta(minutes=20))

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert client.messaging.count_calls == []
    assert message_reconciles(client) == [{"message": 7}]
    assert summary["sent_24h"] == 7


def test_a_budget_snapshot_exactly_as_old_as_the_limit_is_still_fresh(tmp_path):
    db = FakeFirestore()
    client = FakeUnipile(sent_24h=99)
    state.RuntimeState(db, clock=lambda: NOW).store_budget_snapshot(5, NOW - timedelta(minutes=60))

    jobs.tick(db, client, make_settings(tmp_path, budget_snapshot_max_age_minutes=60), NOW)

    assert client.messaging.count_calls == []
    assert message_reconciles(client) == [{"message": 5}]


@pytest.mark.parametrize("snapshot_age", [None, timedelta(minutes=61)], ids=["absent", "stale"])
def test_an_absent_or_stale_snapshot_is_recounted_from_linkedin_once_and_stored(tmp_path, snapshot_age):
    db = FakeFirestore()
    client = FakeUnipile(sent_24h=9)
    if snapshot_age is not None:
        state.RuntimeState(db, clock=lambda: NOW).store_budget_snapshot(5, NOW - snapshot_age)
        ledger.record(db, "message", "a", "sent", NOW - timedelta(minutes=5))

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert client.messaging.count_calls == [NOW - timedelta(hours=24)]
    assert state.RuntimeState(db, clock=lambda: NOW).budget_snapshot() == (9, NOW)
    assert message_reconciles(client) == [{"message": 9}]
    assert summary["sent_24h"] == 9


# =============================================================================
# a requested sync
# =============================================================================


def recording_sync(calls, db, client, queue_id, *, then=None):
    """A stand-in for `jobs.sync` with its exact signature. It records what
    it was called with and the queue/send state at that moment, then runs
    `then` (if given).
    """

    def fake_sync(db_arg, client_arg, settings_arg, now_arg, *, dry_run=False, state=None, classify=None):
        calls.append(
            {
                "now": now_arg,
                "dry_run": dry_run,
                "classify": classify,
                "state": state,
                "status": queue.get(db, queue_id)["status"] if queue_id else None,
                "attempts": len(client.messaging.attempts),
            }
        )
        if then is not None:
            then()
        return {}

    return fake_sync


def test_tick_runs_a_requested_sync_before_sending_and_clears_that_request(tmp_path, monkeypatch):
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    state.RuntimeState(db, clock=lambda: NOW - timedelta(minutes=1)).request_sync()
    calls = []
    monkeypatch.setattr(jobs, "sync", recording_sync(calls, db, client, queue_id))

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert len(calls) == 1
    assert (calls[0]["now"], calls[0]["dry_run"], calls[0]["classify"]) == (NOW, False, None)
    assert isinstance(calls[0]["state"], state.RuntimeState)
    assert (calls[0]["status"], calls[0]["attempts"]) == (queue.APPROVED, 0)
    assert runtime(db)["sync_requested_at"] is None
    assert summary["synced"] is True
    assert summary["outcome"] == "sent"


def test_a_sync_requested_while_the_tick_syncs_is_left_for_the_next_tick(tmp_path, monkeypatch):
    db = FakeFirestore()
    client = FakeUnipile()
    state.RuntimeState(db, clock=lambda: NOW - timedelta(minutes=1)).request_sync()
    webhook_mid_sync = state.RuntimeState(db, clock=lambda: NOW).request_sync
    monkeypatch.setattr(jobs, "sync", recording_sync([], db, client, None, then=webhook_mid_sync))

    jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert runtime(db)["sync_requested_at"] == NOW


def test_a_requested_sync_that_fails_stops_the_tick_before_any_send_and_keeps_the_request(tmp_path, monkeypatch):
    """Sending without the sync could send a follow-up to someone whose
    reply the sync would have stored -- so the tick fails closed.
    """
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    state.RuntimeState(db, clock=lambda: NOW - timedelta(minutes=1)).request_sync()

    def failing_sync(db_arg, client_arg, settings_arg, now_arg, *, dry_run=False, state=None, classify=None):
        raise RuntimeError("linkedin read failed")

    monkeypatch.setattr(jobs, "sync", failing_sync)

    with pytest.raises(RuntimeError, match="linkedin read failed"):
        jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert client.messaging.attempts == []
    assert queue.get(db, queue_id)["status"] == queue.APPROVED
    assert runtime(db)["sync_requested_at"] == NOW - timedelta(minutes=1)
    assert runtime(db)["tick_lease_owner"] is None


def test_the_requested_sync_is_the_real_one_and_records_its_own_run(tmp_path, monkeypatch):
    db = FakeFirestore()
    state.RuntimeState(db, clock=lambda: NOW - timedelta(minutes=1)).request_sync()
    monkeypatch.setattr(messages_sync, "_watermarks", lambda messages_ref: (None, None))

    jobs.tick(db, FakeUnipile(), make_settings(tmp_path), NOW)

    run_ids = sorted(document.id for document in db.collection("runs").stream())
    assert run_ids == ["sync:20260910T140000000000Z", "tick:20260910T140000000000Z"]
    sync_run = db.collection("runs").document("sync:20260910T140000000000Z").get().to_dict()
    assert sync_run["summary"] == {"skipped": "no_history"}
    assert runtime(db)["sync_requested_at"] is None


def test_blocked_writes_skip_a_requested_sync_and_keep_the_request(tmp_path, monkeypatch):
    """Ruling P5-4: a restricted account is not read on every tick. With
    writes blocked, the tick stops before running the requested sync --
    `skipped: writes_blocked`, `synced` false -- and leaves the request
    for the first tick after a human clears the block."""
    db = FakeFirestore()
    client = FakeUnipile()
    follow_up_ready(db, client)
    state.RuntimeState(db, clock=lambda: NOW - timedelta(minutes=1)).request_sync()
    state.RuntimeState(db, clock=lambda: NOW).block_writes("restricted yesterday")
    calls = []
    monkeypatch.setattr(jobs, "sync", recording_sync(calls, db, client, None))

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert calls == []
    assert (summary["skipped"], summary["synced"]) == ("writes_blocked", False)
    assert runtime(db)["sync_requested_at"] == NOW - timedelta(minutes=1)
    assert client.messaging.attempts == []


def test_a_dry_tick_with_writes_blocked_would_not_sync(tmp_path):
    """The dry tick agrees: a requested sync is not one it `would_sync`
    while writes are blocked."""
    db = FakeFirestore()
    client = FakeUnipile()
    state.RuntimeState(db, clock=lambda: NOW - timedelta(minutes=1)).request_sync()
    state.RuntimeState(db, clock=lambda: NOW).block_writes("restricted yesterday")

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW, dry_run=True)

    assert (summary["would_sync"], summary["skipped"]) == (False, "writes_blocked")


def test_no_sync_runs_when_none_was_requested(tmp_path, monkeypatch):
    db = FakeFirestore()
    client = FakeUnipile()
    calls = []
    monkeypatch.setattr(jobs, "sync", recording_sync(calls, db, client, None))

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert calls == []
    assert summary["synced"] is False


# =============================================================================
# dry run
# =============================================================================


def test_dry_run_leaves_the_store_byte_identical_and_sends_nothing(tmp_path):
    """Everything a real tick would change is present: a stale claim to
    sweep, a sync request to serve, no budget snapshot to store, an intro
    the guard refuses, and a follow-up it would send.
    """
    db = FakeFirestore()
    client = FakeUnipile(sent_24h=3)
    refused = intro_refused(db, "nan", created=NOW - timedelta(hours=2))
    sendable = follow_up_ready(db, client, "kim", created=NOW - timedelta(hours=1))
    seed_item(db, "agent:ivy:20260910", "ivy", now=NOW - timedelta(hours=1))
    queue.claim(db, "agent:ivy:20260910", "a-crashed-tick", NOW - timedelta(minutes=30))
    state.RuntimeState(db, clock=lambda: NOW - timedelta(minutes=1)).request_sync()
    before = store_snapshot(db)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW, dry_run=True)

    assert store_snapshot(db) == before
    assert client.messaging.attempts == []
    assert summary["dry_run"] is True
    assert (summary["would_sweep"], summary["would_sync"], summary["sent_24h"]) == (1, True, 3)
    assert summary["would_skip"] == {refused: "intro:conversation_exists"}
    assert (summary["would_send"], summary["route"]) == (sendable, "send_message")
    json.dumps(summary)


# =============================================================================
# a tick with nothing to send fetches one profile (task 3b)
# =============================================================================


def fetchable(db, client, slug="pat-doe") -> None:
    """Queue a profile fetch, as the daily job would, and give the stub a
    complete profile for it with a summary long enough to store.
    """
    seed_fetch(db, slug, now=NOW - timedelta(hours=1))
    client.users.profiles[slug] = profile(
        slug,
        "ACoAAPat",
        first_name="Pat",
        last_name="Doe",
        headline="Director of Revenue Cycle at Coastal Pathology Associates",
        network_distance="FIRST_DEGREE",
    )


def test_a_tick_with_nothing_due_fetches_stores_and_classifies_one_queued_profile(tmp_path, classified):
    """The summary is the send loop's, `idle` kept, merged with what
    `fetching.fetch_one` returned; the run records that same summary."""
    db = FakeFirestore(clock=lambda: NOW)
    client = FakeUnipile()
    fetchable(db, client)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert summary == {
        "stale_swept": 0,
        "synced": False,
        "sent_24h": 0,
        "items_skipped": 0,
        "idle": True,
        "fetch": "stored",
        "classified": True,
    }
    assert client.users.profile_calls == [("pat-doe", True)]
    assert client.messaging.attempts == []
    assert fetch_queue.get(db, "pat-doe")["status"] == fetch_queue.STORED
    assert db.collection("extracted").document("pat-doe").get().exists
    assert db.collection("analysis").document("pat-doe").get().to_dict()["industry"] == "Pathology"
    assert len(classified) == 1
    assert runtime(db)["tick_lease_owner"] is None
    run = db.collection("runs").document("tick:20260910T140000000000Z").get().to_dict()
    assert (run["ok"], run["summary"]) == (True, summary)
    json.dumps(summary)


def test_a_tick_that_sends_a_message_does_not_fetch(tmp_path, classified):
    db = FakeFirestore()
    client = FakeUnipile()
    follow_up_ready(db, client)
    fetchable(db, client)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert summary["outcome"] == "sent"
    assert "fetch" not in summary
    assert client.users.profile_calls == []
    assert profile_reconciles(client) == []
    assert fetch_queue.get(db, "pat-doe")["status"] == fetch_queue.QUEUED


def writes_blocked(db, client):
    state.RuntimeState(db, clock=lambda: NOW).block_writes("restricted yesterday")


def writes_blocked_during_the_recount(db, client):
    """The tick's own state check passes; writes are blocked while it
    recounts the budget, so the guard refuses the due follow-up with
    `state:writes_blocked`."""
    follow_up_ready(db, client)
    count = client.messaging.count_messages_sent_since

    def blocking_count(cutoff):
        state.RuntimeState(db, clock=lambda: NOW).block_writes("restricted meanwhile")
        return count(cutoff)

    client.messaging.count_messages_sent_since = blocking_count


def sends_paused(db, client):
    state.RuntimeState(db, clock=lambda: NOW).pause_sends(NOW + timedelta(minutes=30), "a human paused")


def message_budget_spent(db, client):
    client.budget.limits["message"] = 0


def lease_held_elsewhere(db, client):
    state.RuntimeState(db, clock=lambda: NOW).acquire_tick_lease()


def eleven_refused_intros(db, client):
    for n in range(11):
        intro_refused(db, f"c{n:02d}", created=NOW - timedelta(hours=12 - n))


@pytest.mark.parametrize(
    "stop, expected",
    [
        pytest.param(writes_blocked, {"skipped": "writes_blocked"}, id="writes-blocked"),
        pytest.param(
            writes_blocked_during_the_recount, {"skipped": "writes_blocked"}, id="writes-blocked-refused-by-the-guard"
        ),
        pytest.param(lease_held_elsewhere, {"skipped": "busy"}, id="busy"),
        pytest.param(eleven_refused_intros, {"stopped": "max_attempts"}, id="max-attempts"),
    ],
)
def test_a_tick_that_stops_for_any_other_reason_does_not_fetch(tmp_path, classified, stop, expected):
    """Blocked writes -- a restricted account -- stop the fetch too, whether
    the tick's own check or the guard finds them (ruling P3-6); so do a
    busy lease and ten refused items."""
    db = FakeFirestore()
    client = FakeUnipile()
    fetchable(db, client)
    stop(db, client)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert {key: summary.get(key) for key in expected} == expected
    assert "fetch" not in summary
    assert client.users.profile_calls == []
    assert profile_reconciles(client) == []
    assert fetch_queue.get(db, "pat-doe")["status"] == fetch_queue.QUEUED


@pytest.mark.parametrize(
    "stop, expected",
    [
        pytest.param(
            sends_paused, {"skipped": "sends_paused", "until": (NOW + timedelta(minutes=30)).isoformat()},
            id="sends-paused",
        ),
        pytest.param(message_budget_spent, {"skipped": "budget"}, id="message-budget-spent"),
    ],
)
def test_a_tick_stopped_by_a_message_side_reason_still_fetches_one_profile(tmp_path, classified, stop, expected):
    """Ruling P3-6: a sends pause or a spent message budget stops sending
    only. The profile budget is its own, so the tick goes on to fetch,
    store and classify one queued profile, and its summary carries both the
    stop and the fetch."""
    db = FakeFirestore()
    client = FakeUnipile()
    follow_up_ready(db, client)
    fetchable(db, client)
    stop(db, client)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert {key: summary.get(key) for key in expected} == expected
    assert (summary["fetch"], summary["classified"]) == ("stored", True)
    assert client.users.profile_calls == [("pat-doe", True)]
    assert client.messaging.attempts == []
    assert fetch_queue.get(db, "pat-doe")["status"] == fetch_queue.STORED
    json.dumps(summary)


def restriction_during_the_recount(db, client, where):
    """Writes become blocked while the tick recounts the budget: in the
    runtime state (another job met the restriction) or on the client's
    breaker (some code caught an `AccountRestricted`)."""
    count = client.messaging.count_messages_sent_since

    def restricted_meanwhile(cutoff):
        if where == "state":
            state.RuntimeState(db, clock=lambda: NOW).block_writes("restricted meanwhile")
        else:
            client.writes_blocked = True
        return count(cutoff)

    client.messaging.count_messages_sent_since = restricted_meanwhile


@pytest.mark.parametrize("where", ["state", "client"])
def test_a_restriction_that_lands_during_the_recount_stops_the_budget_stop_fall_through(tmp_path, classified, where):
    """Ruling P3-8. The tick's own state check passes; writes become blocked
    while it recounts, and the recount leaves no message budget. The budget
    stop falls through to the fetch path (ruling P3-6), which stops at once
    with `fetch: writes_blocked`: no profile is fetched, reconciled or
    marked, and nothing is sent. Either way writes end up blocked in the
    state -- the client's flag is recorded by the job wrapper."""
    db = FakeFirestore()
    client = FakeUnipile(sent_24h=7, message_limit=7)
    follow_up_ready(db, client)
    fetchable(db, client)
    restriction_during_the_recount(db, client, where)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert (summary["skipped"], summary["fetch"]) == ("budget", "writes_blocked")
    assert client.users.profile_calls == []
    assert profile_reconciles(client) == []
    assert fetch_queue.get(db, "pat-doe")["status"] == fetch_queue.QUEUED
    assert client.messaging.attempts == []
    assert runtime(db)["writes_blocked_at"] == NOW


def test_a_dry_tick_whose_recount_meets_blocked_writes_previews_writes_blocked(tmp_path, classified):
    """The dry tick agrees with the real one (ruling P3-8): writes blocked
    while it recounts and no message budget left, so its fetch preview is
    `writes_blocked` rather than a slug to fetch. It writes no run and
    marks nothing."""
    db = FakeFirestore()
    client = FakeUnipile(sent_24h=7, message_limit=7)
    follow_up_ready(db, client)
    fetchable(db, client)
    restriction_during_the_recount(db, client, "state")

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW, dry_run=True)

    assert (summary["skipped"], summary["fetch"]) == ("budget", "writes_blocked")
    assert "slug" not in summary
    assert client.users.profile_calls == []
    assert fetch_queue.get(db, "pat-doe")["status"] == fetch_queue.QUEUED
    assert "runs" not in store_snapshot(db)


def test_a_sends_paused_tick_fetches_without_recounting_messages(tmp_path, classified):
    """With no budget snapshot, a recount would ask LinkedIn. A sends-paused
    tick needs no message count, so it goes straight to the fetch: no
    `count_messages_sent_since` call, no message reconcile, no `sent_24h`."""
    db = FakeFirestore()
    client = FakeUnipile(sent_24h=4)
    fetchable(db, client)
    sends_paused(db, client)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert client.messaging.count_calls == []
    assert message_reconciles(client) == []
    assert "sent_24h" not in summary
    assert summary["fetch"] == "stored"
    assert state.RuntimeState(db, clock=lambda: NOW).budget_snapshot() is None


def test_a_sends_paused_tick_whose_fetch_is_throttled_reports_both_pauses(tmp_path, classified):
    """The sends pause's end is `until` and the fetch back-off's is
    `fetch_until`, side by side in one summary."""
    db = FakeFirestore()
    client = FakeUnipile()
    seed_fetch(db, "pat-doe", now=NOW - timedelta(hours=1))
    client.users.profile_error = unipile_errors.ProfileIncomplete(type="local/profile_incomplete", title="withheld")
    client.users.charge_error = True
    state.RuntimeState(db, clock=lambda: NOW).pause_sends(NOW + timedelta(hours=2), "a human paused")

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert (summary["skipped"], summary["until"]) == ("sends_paused", (NOW + timedelta(hours=2)).isoformat())
    assert (summary["fetch"], summary["fetch_until"]) == ("throttled", (NOW + timedelta(minutes=30)).isoformat())
    run = db.collection("runs").document("tick:20260910T140000000000Z").get().to_dict()
    assert run["summary"] == summary


@pytest.mark.parametrize("sends", ["nothing-due", "sends-paused"])
def test_a_tick_with_fetches_paused_does_not_fetch(tmp_path, classified, sends):
    """Fetches paused (a back-off, a 429, a human): no profile is fetched or
    counted, whether the tick had nothing to send or sends are paused too;
    the fetch pause's end is reported as `fetch_until`."""
    db = FakeFirestore()
    client = FakeUnipile()
    fetchable(db, client)
    runtime_state = state.RuntimeState(db, clock=lambda: NOW)
    runtime_state.pause_fetches(NOW + timedelta(hours=1), "rate limited")
    if sends == "sends-paused":
        sends_paused(db, client)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert (summary["fetch"], summary["fetch_until"]) == ("paused", (NOW + timedelta(hours=1)).isoformat())
    assert client.users.profile_calls == []
    assert profile_reconciles(client) == []
    assert fetch_queue.get(db, "pat-doe")["status"] == fetch_queue.QUEUED
    if sends == "sends-paused":
        assert summary["until"] == (NOW + timedelta(minutes=30)).isoformat()


def test_a_dry_tick_with_nothing_due_previews_the_fetch_and_fetches_nothing(tmp_path, classified):
    db = FakeFirestore()
    client = FakeUnipile()
    fetchable(db, client)
    before = store_snapshot(db)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW, dry_run=True)

    assert store_snapshot(db) == before
    assert (summary["idle"], summary["fetch"], summary["slug"], summary["profiles_24h"]) == (
        True, "would_fetch", "pat-doe", 0,
    )
    assert client.users.profile_calls == []
    assert profile_reconciles(client) == []
    assert classified == []
    json.dumps(summary)


def test_a_dry_tick_that_would_send_previews_no_fetch(tmp_path, classified):
    db = FakeFirestore()
    client = FakeUnipile()
    queue_id = follow_up_ready(db, client)
    fetchable(db, client)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW, dry_run=True)

    assert summary["would_send"] == queue_id
    assert "fetch" not in summary
    assert client.users.profile_calls == []


@pytest.mark.parametrize(
    "stop, expected",
    [
        pytest.param(
            sends_paused, {"skipped": "sends_paused", "until": (NOW + timedelta(minutes=30)).isoformat()},
            id="sends-paused",
        ),
        pytest.param(message_budget_spent, {"skipped": "budget"}, id="message-budget-spent"),
    ],
)
def test_a_dry_tick_stopped_by_a_message_side_reason_previews_the_fetch(tmp_path, classified, stop, expected):
    """The dry tick follows ruling P3-6 as the real one does: a sends pause
    or a spent message budget still previews the fetch, and writes nothing.
    A sends-paused dry tick asks LinkedIn for no message count."""
    db = FakeFirestore()
    client = FakeUnipile()
    follow_up_ready(db, client)
    fetchable(db, client)
    stop(db, client)
    before = store_snapshot(db)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW, dry_run=True)

    assert {key: summary.get(key) for key in expected} == expected
    assert (summary["fetch"], summary["slug"]) == ("would_fetch", "pat-doe")
    assert store_snapshot(db) == before
    assert client.users.profile_calls == []
    if stop is sends_paused:
        assert client.messaging.count_calls == []


def test_a_dry_tick_with_writes_blocked_previews_no_fetch(tmp_path, classified):
    db = FakeFirestore()
    client = FakeUnipile()
    fetchable(db, client)
    writes_blocked(db, client)

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW, dry_run=True)

    assert summary["skipped"] == "writes_blocked"
    assert "fetch" not in summary


def test_with_exactly_ten_refused_items_the_dry_tick_and_the_real_tick_both_stop_at_max_attempts(
    tmp_path, classified
):
    """Minor M1: ten due items, every one refused. The real tick skips all
    ten and stops at `MAX_ATTEMPTS` without looking for an eleventh; the
    dry tick agrees -- `stopped: max_attempts`, not `idle` with a fetch
    preview -- and neither fetches."""
    db = FakeFirestore()
    client = FakeUnipile()
    settings = make_settings(tmp_path)
    for n in range(jobs.MAX_ATTEMPTS):
        intro_refused(db, f"c{n:02d}", created=NOW - timedelta(hours=12 - n))
    fetchable(db, client)

    dry = jobs.tick(db, client, settings, NOW, dry_run=True)
    real = jobs.tick(db, client, settings, NOW)

    assert jobs.MAX_ATTEMPTS == 10
    assert (dry["stopped"], real["stopped"]) == ("max_attempts", "max_attempts")
    assert (len(dry["would_skip"]), real["items_skipped"]) == (10, 10)
    assert "fetch" not in dry
    assert "fetch" not in real
    assert client.users.profile_calls == []


def test_a_restriction_met_by_the_profile_fetch_blocks_writes_raises_one_alert_and_pauses_fetches(
    tmp_path, classified
):
    """The fetch itself only pauses fetches for a day. The job wrapper
    (`jobs._watch_restriction`) finds the client's breaker tripped when the
    tick returns, blocks writes and raises the one `restricted` alert --
    and no `fetch_forbidden` alert is raised. The slug stays queued.
    """
    db = FakeFirestore()
    client = FakeUnipile()
    seed_fetch(db, "pat-doe", now=NOW - timedelta(hours=1))
    client.users.profile_error = restricted()

    summary = jobs.tick(db, client, make_settings(tmp_path), NOW)

    assert (summary["idle"], summary["fetch"]) == (True, "restricted")
    assert runtime(db)["writes_blocked_at"] == NOW
    assert runtime(db)["fetches_paused_until"] == NOW + timedelta(hours=24)
    assert alerts(db) == ["alert:restricted:20260910T140000000000Z"]
    assert fetch_queue.get(db, "pat-doe")["status"] == fetch_queue.QUEUED
    run = db.collection("runs").document("tick:20260910T140000000000Z").get().to_dict()
    assert run["ok"] is True
