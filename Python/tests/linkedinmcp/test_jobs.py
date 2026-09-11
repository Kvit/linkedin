"""Tests for the parts of `linkedinmcp.jobs` that are not one of the three
jobs themselves: `record_run` (one `runs` document per non-dry job run,
written on failure too, after which the exception propagates), the
`job_failed` alert a failed run raises once per job and local day,
`handle_unipile_webhook` (records that a sync is wanted, nothing more), and
`default_classify` (the Gemini-backed classifier the HTTP endpoint and CLI
hand to `sync`).

`sync`, `daily` and `tick` each have their own file: `test_jobs_sync.py`,
`test_jobs_daily.py`, `test_jobs_tick.py`. Shared fixtures live in
`fake_unipile.py`.

No test here constructs a real Firestore, Unipile or Gemini client: the
database is `FakeFirestore`, the LinkedIn client is `FakeUnipile`, and
`default_classify`'s two collaborators are monkeypatched on their modules.
"""

import logging
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import messages_sync
import pipeline
from linkedinmcp import clients, decisions, jobs, state
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_unipile import FakeUnipile, make_settings, store_snapshot, write_template

NOW = datetime(2026, 9, 10, 14, 0, 0, tzinfo=UTC)

#: Our own LinkedIn provider id, as Unipile reports it in `account_info.user_id`.
OUR_ID = "ACoAAOurOwnAccount"


def runs(db) -> dict:
    return {doc.id: doc.to_dict() for doc in db.collection("runs").stream()}


# =============================================================================
# record_run
# =============================================================================


def test_record_run_writes_one_document_under_the_job_and_start_time_id():
    db = FakeFirestore()

    run_id = jobs.record_run(
        db, "tick", NOW, NOW + timedelta(seconds=3), ok=True, summary={"idle": True}
    )

    assert run_id == "tick:20260910T140000000000Z"
    assert runs(db) == {
        run_id: {
            "job": "tick",
            "started_at": NOW,
            "finished_at": NOW + timedelta(seconds=3),
            "ok": True,
            "summary": {"idle": True},
            "error": None,
        }
    }


def test_record_run_stores_a_failure_with_the_error_class_name():
    db = FakeFirestore()

    run_id = jobs.record_run(db, "sync", NOW, NOW, ok=False, summary={}, error="RuntimeError")

    assert runs(db)[run_id]["ok"] is False
    assert runs(db)[run_id]["error"] == "RuntimeError"


def test_record_run_builds_the_id_from_the_utc_time_whatever_the_offset_given():
    db = FakeFirestore()
    same_instant_in_kolkata = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))

    run_id = jobs.record_run(db, "daily", same_instant_in_kolkata, NOW, ok=True, summary={})

    assert run_id == "daily:20260910T140000000000Z"


def test_record_run_refuses_a_naive_start_time_and_writes_nothing():
    db = FakeFirestore()

    with pytest.raises(ValueError):
        jobs.record_run(db, "tick", datetime(2026, 9, 10, 14, 0), NOW, ok=True, summary={})

    assert runs(db) == {}


# --- the wrapper every job runs inside ------------------------------------------


def test_a_non_dry_job_records_its_summary_as_a_successful_run(monkeypatch, tmp_path):
    """`sync` with nothing stored returns `{"skipped": "no_history"}`; the
    same dict is stored as the run's summary.
    """
    db = FakeFirestore()
    monkeypatch.setattr(messages_sync, "_watermarks", lambda messages_ref: (None, None))

    summary = jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    assert summary == {"skipped": "no_history"}
    assert runs(db) == {
        "sync:20260910T140000000000Z": {
            "job": "sync",
            "started_at": NOW,
            "finished_at": NOW,
            "ok": True,
            "summary": {"skipped": "no_history"},
            "error": None,
        }
    }


def test_a_job_that_raises_records_a_failed_run_and_then_re_raises(monkeypatch, tmp_path):
    db = FakeFirestore()

    def broken_watermarks(messages_ref):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(messages_sync, "_watermarks", broken_watermarks)

    with pytest.raises(RuntimeError, match="firestore unavailable"):
        jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    assert runs(db) == {
        "sync:20260910T140000000000Z": {
            "job": "sync",
            "started_at": NOW,
            "finished_at": NOW,
            "ok": False,
            "summary": {},
            "error": "RuntimeError",
        }
    }


def test_a_dry_run_records_no_run(monkeypatch, tmp_path):
    db = FakeFirestore()
    monkeypatch.setattr(messages_sync, "_watermarks", lambda messages_ref: (None, None))

    jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW, dry_run=True)

    assert store_snapshot(db) == {}


# --- a job that fails raises one alert per job and local day (final review FI4)


def broken_watermarks(messages_ref):
    raise RuntimeError("firestore unavailable")


def alert_ids(db) -> list[str]:
    return sorted(decision["id"] for decision in decisions.list_decisions(db, limit=100))


def test_a_failed_run_raises_one_job_failed_alert_for_the_job_and_local_day(monkeypatch, tmp_path):
    """After its failed `runs` record, the job raises a create-only
    `job_failed` alert keyed `{job}:{local date}`: a service alert naming
    the job, the error's class (never its message) and the failed run's id,
    and pointing at `get_run_report`. The exception still leaves the job."""
    db = FakeFirestore()
    monkeypatch.setattr(messages_sync, "_watermarks", broken_watermarks)

    with pytest.raises(RuntimeError, match="firestore unavailable"):
        jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    assert alert_ids(db) == ["alert:job_failed:sync:20260910"]
    alert = decisions.get(db, "alert:job_failed:sync:20260910")
    assert alert["context"] == {"job": "sync", "error": "RuntimeError", "run_id": "sync:20260910T140000000000Z"}
    assert (alert["asked_by"], alert["status"]) == ("service", "pending")
    assert "get_run_report" in alert["question"]
    assert "firestore unavailable" not in alert["question"]


def test_a_job_that_keeps_failing_raises_one_alert_a_day_per_job(monkeypatch, tmp_path):
    """Three failed syncs the same local day make one alert; a failed daily
    the same day makes its own; a failed sync the next day, a second."""
    db = FakeFirestore()
    settings = make_settings(tmp_path)
    monkeypatch.setattr(messages_sync, "_watermarks", broken_watermarks)
    write_template(tmp_path, "Thanks for connecting!")
    client = FakeUnipile()
    client.messaging.read_errors["iter_chats"] = RuntimeError("linkedin unavailable")

    for minutes in (0, 15, 30):
        with pytest.raises(RuntimeError):
            jobs.sync(db, FakeUnipile(), settings, NOW + timedelta(minutes=minutes))
    with pytest.raises(RuntimeError):
        jobs.daily(db, client, settings, NOW)
    with pytest.raises(RuntimeError):
        jobs.sync(db, FakeUnipile(), settings, NOW + timedelta(days=1))

    assert alert_ids(db) == [
        "alert:job_failed:daily:20260910",
        "alert:job_failed:sync:20260910",
        "alert:job_failed:sync:20260911",
    ]
    assert len(runs(db)) == 5


def test_the_job_failed_key_is_the_local_date_in_settings_tz(monkeypatch, tmp_path):
    """02:00 UTC on 10 September is 21:00 on the 9th in Chicago."""
    db = FakeFirestore()
    monkeypatch.setattr(messages_sync, "_watermarks", broken_watermarks)

    with pytest.raises(RuntimeError):
        jobs.sync(db, FakeUnipile(), make_settings(tmp_path, tz="America/Chicago"), datetime(2026, 9, 10, 2, tzinfo=UTC))

    assert alert_ids(db) == ["alert:job_failed:sync:20260909"]


def test_a_job_failed_alert_that_cannot_be_written_does_not_hide_the_jobs_own_error(monkeypatch, tmp_path, caplog):
    """The alert is best-effort: writing it fails, the failure is logged,
    and the error that leaves the job is still the job's own, after its
    `runs` record."""
    db = FakeFirestore()
    monkeypatch.setattr(messages_sync, "_watermarks", broken_watermarks)

    def alert_store_down(*args, **kwargs):
        raise ValueError("decisions unavailable")

    monkeypatch.setattr(decisions, "raise_alert", alert_store_down)

    with caplog.at_level(logging.WARNING, logger="linkedinmcp.jobs"):
        with pytest.raises(RuntimeError, match="firestore unavailable"):
            jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)

    assert runs(db)["sync:20260910T140000000000Z"]["error"] == "RuntimeError"
    assert "ValueError" in caplog.text
    assert "sync" in caplog.text


def test_a_run_that_succeeds_or_a_dry_run_that_fails_raises_no_job_failed_alert(monkeypatch, tmp_path):
    db = FakeFirestore()
    monkeypatch.setattr(messages_sync, "_watermarks", lambda messages_ref: (None, None))
    jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW)
    monkeypatch.setattr(messages_sync, "_watermarks", broken_watermarks)

    with pytest.raises(RuntimeError):
        jobs.sync(db, FakeUnipile(), make_settings(tmp_path), NOW + timedelta(minutes=15), dry_run=True)

    assert alert_ids(db) == []


# =============================================================================
# handle_unipile_webhook
# =============================================================================


def new_message(message_id="msg-1", *, sender=None, user_id=OUR_ID, event="message_received", chat_id="chat-9"):
    """A `message_received` delivery with every key Unipile's docs list.
    `sender` defaults to a contact (not us), so the default is an inbound
    message.
    """
    return {
        "account_id": "acc-1",
        "account_type": "LINKEDIN",
        "account_info": {"type": "LINKEDIN", "feature": "classic", "user_id": user_id},
        "event": event,
        "chat_id": chat_id,
        "timestamp": "2026-09-10T13:59:58.000Z",
        "webhook_name": "linkedin-outreach",
        "message_id": message_id,
        "message": "Thanks, tell me more about the pricing.",
        "sender": {
            "attendee_id": "att-7",
            "attendee_name": "Jane Roe",
            "attendee_provider_id": sender or "ACoAATheContact",
            "attendee_profile_url": "https://www.linkedin.com/in/jane-roe",
        },
        "attendees": [],
        "attachments": [],
        "reaction": "",
        "reaction_sender": None,
    }


def sync_requested_at(db):
    return state.RuntimeState(db, clock=lambda: NOW).sync_requested()


def test_webhook_accepts_a_new_inbound_message_and_requests_a_sync():
    db = FakeFirestore()

    result = jobs.handle_unipile_webhook(db, new_message(), NOW)

    assert result == {"accepted": True, "reason": "sync_requested"}
    assert db.collection("webhook_events").document("msg-1").get().to_dict() == {
        "received_at": NOW,
        "chat_id": "chat-9",
    }
    assert sync_requested_at(db) == NOW


def test_webhook_accepts_a_redelivered_message_id_only_once():
    """The second delivery of the same `message_id` -- Unipile retries up to
    five times -- is not accepted, and the event document keeps the first
    delivery's `received_at`. It does request a sync again (the first
    request was cleared before it arrived): `request_sync` runs before the
    create-only document, and a second request is harmless.
    """
    db = FakeFirestore()
    first = jobs.handle_unipile_webhook(db, new_message(), NOW)
    state.RuntimeState(db, clock=lambda: NOW).clear_sync_request(NOW)

    second = jobs.handle_unipile_webhook(db, new_message(), NOW + timedelta(seconds=30))

    assert first["accepted"] is True
    assert second == {"accepted": False, "reason": "duplicate"}
    assert [doc.id for doc in db.collection("webhook_events").stream()] == ["msg-1"]
    assert db.collection("webhook_events").document("msg-1").get().to_dict()["received_at"] == NOW
    assert sync_requested_at(db) == NOW + timedelta(seconds=30)


def test_a_sync_request_that_fails_stores_no_event_so_the_redelivery_is_accepted(monkeypatch):
    """`request_sync` fails: the webhook raises -- the delivery gets no 200,
    so Unipile sends it again -- and no event document was stored to turn
    that redelivery away as a duplicate. The redelivery is accepted and
    requests the sync.
    """
    db = FakeFirestore()
    failing = state.RuntimeState(db, clock=lambda: NOW)

    def request_sync_fails():
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(failing, "request_sync", request_sync_fails)

    with pytest.raises(RuntimeError, match="firestore unavailable"):
        jobs.handle_unipile_webhook(db, new_message(), NOW, state=failing)

    assert list(db.collection("webhook_events").stream()) == []

    second = jobs.handle_unipile_webhook(db, new_message(), NOW + timedelta(seconds=30))

    assert second == {"accepted": True, "reason": "sync_requested"}
    assert sync_requested_at(db) == NOW + timedelta(seconds=30)


def test_webhook_ignores_our_own_outbound_message():
    """Unipile delivers the messages this account sends as
    `message_received` too; they are told apart by the sender being us.
    """
    db = FakeFirestore()

    result = jobs.handle_unipile_webhook(db, new_message(sender=OUR_ID), NOW)

    assert result == {"accepted": False, "reason": "own_message"}
    assert store_snapshot(db) == {}


@pytest.mark.parametrize(
    "event", ["message_reaction", "message_read", "message_edited", "message_deleted", "message_delivered"]
)
def test_webhook_ignores_every_event_but_message_received(event):
    db = FakeFirestore()

    result = jobs.handle_unipile_webhook(db, new_message(event=event), NOW)

    assert result == {"accepted": False, "reason": "event_ignored"}
    assert store_snapshot(db) == {}


def _without(key):
    payload = new_message()
    del payload[key]
    return payload


def _with(**changes):
    payload = new_message()
    payload.update(changes)
    return payload


@pytest.mark.parametrize(
    "payload, reason",
    [
        (None, "payload_not_an_object"),
        (["message_received"], "payload_not_an_object"),
        ("message_received", "payload_not_an_object"),
        ({}, "event_ignored"),
        (_without("event"), "event_ignored"),
        (_without("account_info"), "sender_unknown"),
        (_with(account_info="LINKEDIN"), "sender_unknown"),
        (_with(account_info={"type": "LINKEDIN"}), "sender_unknown"),
        (_without("sender"), "sender_unknown"),
        (_with(sender=None), "sender_unknown"),
        (_with(sender={"attendee_name": "Jane Roe"}), "sender_unknown"),
        (_with(sender={"attendee_provider_id": ""}), "sender_unknown"),
        (_without("message_id"), "no_message_id"),
        (_with(message_id=None), "no_message_id"),
        (_with(message_id=""), "no_message_id"),
        (_with(message_id=12345), "no_message_id"),
        (_with(message_id="a/b"), "no_message_id"),
        (_with(message_id=".."), "no_message_id"),
        (_with(message_id="__reserved__"), "no_message_id"),
        (_with(message_id="x" * 1501), "no_message_id"),
        (_with(message_id="é" * 751), "no_message_id"),
        (_with(message_id="\ud800"), "no_message_id"),
    ],
)
def test_webhook_ignores_a_malformed_payload_with_a_reason_and_never_raises(payload, reason):
    """The last three `message_id`s cannot be a Firestore document id:
    1,501 UTF-8 bytes; 751 characters that are 1,502 bytes (the limit is
    bytes, not characters); and a lone surrogate, which JSON can carry and
    UTF-8 cannot encode at all.
    """
    db = FakeFirestore()

    result = jobs.handle_unipile_webhook(db, payload, NOW)

    assert result == {"accepted": False, "reason": reason}
    assert store_snapshot(db) == {}


@pytest.mark.parametrize("message_id", ["x" * 1500, "é" * 750], ids=["1500-ascii", "750-two-byte"])
def test_webhook_accepts_a_message_id_of_exactly_1500_utf8_bytes(message_id):
    db = FakeFirestore()

    result = jobs.handle_unipile_webhook(db, _with(message_id=message_id), NOW)

    assert result == {"accepted": True, "reason": "sync_requested"}
    assert [doc.id for doc in db.collection("webhook_events").stream()] == [message_id]


def test_webhook_stores_a_missing_or_non_string_chat_id_as_none():
    db = FakeFirestore()

    result = jobs.handle_unipile_webhook(db, _with(chat_id={"nested": "x"}), NOW)

    assert result["accepted"] is True
    assert db.collection("webhook_events").document("msg-1").get().to_dict() == {
        "received_at": NOW,
        "chat_id": None,
    }


def test_webhook_uses_the_given_state_for_the_sync_request():
    db = FakeFirestore()
    later = NOW + timedelta(minutes=2)

    jobs.handle_unipile_webhook(db, new_message(), NOW, state=state.RuntimeState(db, clock=lambda: later))

    assert sync_requested_at(db) == later


# =============================================================================
# default_classify
# =============================================================================


def test_default_classify_runs_the_pipeline_on_a_new_gemini_client_and_returns_its_rows(monkeypatch):
    """At most 50 Gemini calls a run (ruling P5-4: `limit=50`, newest
    conversations first): a backlog is worked through over several syncs,
    never in one long one."""
    db = FakeFirestore()
    gemini = object()
    calls = []

    async def fake_run_pipeline(db_arg, client_arg, **kwargs):
        calls.append((db_arg, client_arg, kwargs))
        return SimpleNamespace(rows=[{"doc_id": "jane-roe", "stage": "lead"}])

    monkeypatch.setattr(clients, "gemini_client", lambda: gemini)
    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)

    rows = jobs.default_classify(db)

    assert rows == [{"doc_id": "jane-roe", "stage": "lead"}]
    assert calls == [(db, gemini, {"limit": 50})]
