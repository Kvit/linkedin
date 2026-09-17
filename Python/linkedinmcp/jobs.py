"""The jobs that act on LinkedIn: `sync` mirrors new messages into Firestore
and reacts to replies, `daily` plans the day by queueing intros, `tick` sends
AT MOST ONE queued message -- or, when nothing is due or sending alone is
stopped (a sends pause, the message budget), fetches at most one queued
profile (`fetching`) -- and `handle_unipile_webhook` only records that a
sync is wanted. Each is a short, stateless call a scheduler or an HTTP
request makes; everything a job needs to remember between runs lives in
Firestore (`outreach_queue`, `action_log`, `decisions`, `runtime_state`).

`tick` sends a real message to a real person, so two invariants outrank
everything else here:

- **A message is never sent twice.** `queue.claim` moves an item to
  `sending` in a transaction BEFORE the LinkedIn call, and nothing ever sends
  a `sending` item. Only a failure that provably happened before LinkedIn
  accepted anything (contract §3: the budget or circuit refusing locally, a
  401/403/429) `release`s the claim back to `approved`. An outcome nobody can
  know -- a 5xx, a dropped connection, any unexpected exception -- is settled
  `unknown` and put to a human as an alert, never retried.
- **A message is never silently lost.** A claim that never settles (the
  process died mid-send) is swept to `unknown` with an alert after ten
  minutes. `sync` later resolves `unknown` against LinkedIn's own history: an
  outbound message stored after the attempt means it went (`sent`); none
  within 48 hours means it did not (`failed`).

The LinkedIn call is the ONLY thing inside `tick`'s outcome mapping. Settling
a successful send happens outside it: if that Firestore write fails, the
item stays `sending`, the sweep moves it to `unknown`, and `sync` finds the
message -- whereas mapping the Firestore error itself would mislabel a
message LinkedIn accepted. `BaseException` (a SIGTERM mid-send) is not
caught at all, for the same reason: the sweep is the path for "we do not
know".

Every job takes `now` and never reads the wall clock (contract §0); the
default `RuntimeState` is built on `lambda: now` so every read of "now"
inside one job agrees. Every non-dry run writes one `runs` document, on
failure too -- a failure then raises a `job_failed` alert, once per job and
local day, so a job that keeps failing is never silent -- and then lets the
exception propagate. A non-dry run that finds
LinkedIn has restricted the account -- at any step, a read included --
blocks writes and raises one `restricted` alert before its result or
exception leaves it (`_watch_restriction`). A dry run writes nothing at all
-- no run, no lease, no snapshot, no queue change -- and never calls Gemini
(ruling P2-15); it may read from LinkedIn.

Project modules (`messages_sync`, `pipeline`, `functions`) are imported
inside the functions that use them and reached through the module, never
`from x import name`: importing this module stays free (`pipeline` alone
pulls in `google.genai`), and a test can monkeypatch them -- the same seam
`clients.py` documents.
"""

import asyncio
import logging
import math
import random
import re
from datetime import UTC, timedelta
from zoneinfo import ZoneInfo

from linkedinmcp import clock, decisions, fetch_queue, fetching, guards, ledger, monitor, queue
from linkedinmcp import state as runtime_state

logger = logging.getLogger(__name__)

RUNS_COLLECTION = "runs"
WEBHOOK_EVENTS_COLLECTION = "webhook_events"
MESSAGES_COLLECTION = "messages"
EXTRACTED_COLLECTION = "extracted"
ANALYSIS_COLLECTION = "analysis"


# =============================================================================
# Runs
# =============================================================================


def record_run(db, job, started_at, finished_at, *, ok, summary, error=None) -> str:
    """Write `runs/{job}:{started_at in UTC, %Y%m%dT%H%M%S%fZ}` and return
    that id. `error` is an exception's class name, or `None`.

    The id is built from `started_at` converted to UTC, so the `Z` it ends
    with is true whatever offset the caller's datetime carried. A naive
    `started_at` raises `ValueError` before anything is written -- the same
    "never guess a timezone" rule as `clock.local_date` and `ledger.entry`.
    """
    if started_at.tzinfo is None:
        raise ValueError("record_run: `started_at` must be timezone-aware")
    run_id = f"{job}:{started_at.astimezone(UTC):%Y%m%dT%H%M%S%fZ}"
    db.collection(RUNS_COLLECTION).document(run_id).set(
        {
            "job": job,
            "started_at": started_at,
            "finished_at": finished_at,
            "ok": ok,
            "summary": summary,
            "error": error,
        }
    )
    return run_id


def _run_job(db, job, now, *, settings, dry_run, body, client, state):
    """Run `body()` and record the run -- unless `dry_run`, which records
    nothing (ruling P2-15).

    A non-dry run also watches for LinkedIn restricting the account
    (`_watch_restriction`), whatever step meets it. A dry run does not: it
    may not write the block or the alert, and the exception reaches the
    caller all the same.

    `finished_at` is `now`, the same as `started_at`: a job never reads the
    wall clock (contract §0), and its signature carries no clock of its own.
    A failed run is stored with an empty summary and the exception's class
    name, then raises the `job_failed` alert (`_alert_job_failed`), and the
    exception is re-raised.
    """
    if dry_run:
        return body()
    try:
        summary = _watch_restriction(db, client, now, state=state, job=job, body=body)
    except Exception as error:
        run_id = record_run(db, job, now, now, ok=False, summary={}, error=type(error).__name__)
        _alert_job_failed(db, settings, now, job=job, error=error, run_id=run_id)
        raise
    record_run(db, job, now, now, ok=True, summary=summary)
    return summary


def _alert_job_failed(db, settings, now, *, job, error, run_id) -> None:
    """Raise the `job_failed` alert for a run that just failed (final review
    FI4): create-only, keyed `{job}:{local date}` (`_local_day`), so a job
    that fails on every tick all day is ONE alert a day, not silence and
    not one per run. It names the job, the error's class -- never its
    message, which can carry project ids or a contact's data -- and the
    failed run's id.

    Best-effort: this runs while the job's own exception is being handled,
    and a failure to write the alert -- Firestore down, most likely, which
    may be what failed the job -- is logged, never raised in its place.
    """
    name = type(error).__name__
    try:
        decisions.raise_alert(
            db,
            "job_failed",
            f"{job}:{_local_day(now, settings)}",
            (
                f"The {job} job failed ({name}) at {now.isoformat()}. It raises this alert once a day however "
                f"often it fails: get_run_report(job=\"{job}\") lists its runs, newest first, and whether each "
                "one failed."
            ),
            {"job": job, "error": name, "run_id": run_id},
            now,
        )
    except Exception as alert_error:
        logger.warning(
            "the %s job failed (%s), and its job_failed alert could not be written (%s: %s)",
            job, name, type(alert_error).__name__, alert_error,
        )


def _watch_restriction(db, client, now, *, state, job, body):
    """Run `body()`. If LinkedIn restricted the account at any point in it,
    block writes and raise the `restricted` alert (`_note_restriction`)
    before the summary -- or the exception -- leaves the job.

    "At any point" is either an `AccountRestricted` escaping `body()` -- a
    read raises it as readily as a send: the budget recount, the requested
    sync, daily's chat list -- or `client.writes_blocked` being true when
    `body()` ends. The real client sets that flag only while raising
    `AccountRestricted` (`Transport._raise`), so a true flag after a normal
    return means some code caught one. It is never set by a send that
    succeeded: once set, the client refuses every write before its request.

    If recording the restriction itself fails, that error leaves the job in
    place of the one being handled (chained to it), and the next job to meet
    the restriction records it.
    """
    try:
        summary = body()
    except Exception as error:
        from lib.unipile import errors as unipile_errors

        if isinstance(error, unipile_errors.AccountRestricted) or client.writes_blocked:
            _note_job_restriction(db, state, now, job=job, error=type(error).__name__)
        raise
    if client.writes_blocked:
        _note_job_restriction(db, state, now, job=job, error=None)
    return summary


def _note_job_restriction(db, state, now, *, job, error):
    """`_note_restriction` for a restriction a job met outside the send's
    own outcome mapping -- `error` is the class name that escaped, or
    `None` when only the client's flag showed it.
    """
    _note_restriction(
        db,
        state,
        now,
        reason=f"LinkedIn restricted the account (met by the {job} job)",
        question=(
            f"LinkedIn restricted the account; the {job} job met it at {now.isoformat()}. Every send is "
            "blocked until a human clears the block."
        ),
        context={"job": job, "error": error},
    )


def _note_restriction(db, state, now, *, reason, question, context) -> bool:
    """Record that LinkedIn restricted the account: raise the `restricted`
    alert, then `state.block_writes(reason)` -- unless writes are already
    blocked, in which case do nothing. Returns whether it did anything.

    Once per restriction. The alert is keyed by `now` (`_restriction_key`),
    and a restriction met again while writes are still blocked -- the next
    sync's reads, a requested sync on every tick -- is the same restriction,
    so it raises nothing new. Once a human clears the block, the next one is
    a new alert.

    The alert is written BEFORE the block, for the reason `sweep_stale`
    gives: the block is what makes every later job skip this, so an alert
    that failed after it would never be raised. If the alert write fails,
    nothing is blocked: the next job to meet the restriction does both --
    a send tried meanwhile that LinkedIn refuses as restricted meets the §3
    row, which does both. If the block fails after the alert, the next job
    raises a second alert under a new key -- a duplicate only after a failed
    write, never a lost alert.
    """
    if state.writes_blocked():
        return False
    decisions.raise_alert(db, "restricted", _restriction_key(now), question, context, now)
    state.block_writes(reason)
    return True


def _default_state(db, now, state):
    """The caller's `RuntimeState`, or one whose clock is pinned to `now`."""
    return state if state is not None else runtime_state.RuntimeState(db, clock=lambda: now)


# =============================================================================
# sync
# =============================================================================


#: Ruling P2-8: a stored outbound message this long BEFORE the attempt still
#: counts as the attempt -- the send's own timestamp comes from LinkedIn's
#: clock, `sending_at` from ours.
UNKNOWN_MATCH_SLACK = timedelta(minutes=5)

#: Ruling P2-8: an `unknown` this much older than its attempt, with no
#: outbound message found, is taken as never sent.
UNKNOWN_GIVE_UP = timedelta(hours=48)
UNKNOWN_GIVE_UP_ERROR = "no outbound message in LinkedIn history 48 h after the attempt"

#: `queue.list_items` caps a page at 100. Every `unknown` also raised an
#: alert, so more than 100 at once means something else is badly wrong; the
#: rest are resolved by a later sync.
UNKNOWN_PAGE = 100


def sync(db, client, settings, now, *, dry_run=False, state=None, classify=None) -> dict:
    """Mirror the messages LinkedIn holds past the sync's cursor
    (`messages_sync.read_cursor`; the newest stored message until the first
    sync records one) into `messages`, then react to them:

    0. sweep stale claims (`sweep_stale`), as `daily` and `tick` do -- sync
       runs every 15 minutes all week, so a claim a dead tick left behind is
       put to a human within minutes even when no tick runs (ruling P5-4);
    1. the forward pass (`messages_sync`);
    2. every contact with a new INBOUND message has their `pending` and
       `approved` items cancelled ("they replied"): a reply stops queued
       follow-ups, and the agent decides again. This comes straight after
       the forward pass: its messages are found by the cursor from before
       it, which the forward pass has already moved past;
    3. when the forward pass wrote anything, the per-contact stats refresh;
    4. every `unknown` item is resolved against the stored history (ruling
       P2-8) -- see `_resolve_unknowns`;
    5. when `classify` is given, `rows = classify(db)` and one `lead` alert
       per contact that just BECAME a lead (see `_alert_new_leads`).

    With nothing stored yet this returns `{"skipped": "no_history"}`: the
    first backfill is a manual `messages_sync.py` run, not a job's. Under
    `dry_run` nothing is swept and only the forward pass runs, writing
    nothing, and the summary is what they counted; steps 2 to 5 are
    skipped, so Gemini is never called.

    `classify` defaults to `None` -- no Gemini -- so `tick` can run a cheap
    sync; the HTTP endpoint and CLI pass `default_classify`. `state` is used
    only to block writes when LinkedIn turns out to have restricted the
    account (`_watch_restriction`); it defaults to one pinned to `now`.
    """
    state = _default_state(db, now, state)
    return _run_job(
        db, "sync", now, settings=settings, dry_run=dry_run, client=client, state=state,
        body=lambda: _sync(db, client, now, dry_run=dry_run, classify=classify),
    )


def _sync(db, client, now, *, dry_run, classify, beat=None) -> dict:
    """`sync`'s body. `beat(note)`, when given, is called between phases --
    never before the replies' cancellations -- so a job running this
    (`steps.sync_messages`) heartbeats, and stops there if it was taken for
    lost (`monitor.JobLost`)."""
    import messages_sync

    messages_ref = db.collection(MESSAGES_COLLECTION)
    _min_ts, max_ts = messages_sync._watermarks(messages_ref)
    if max_ts is None:
        return {"skipped": "no_history"}
    # The sync's own cursor, which nothing else writes; until the first sync
    # that records one, the newest stored message stands in for it.
    cursor = messages_sync.read_cursor(db) or max_ts

    # Before LinkedIn is read, so an outage there does not hold it up; and
    # before `_resolve_unknowns`, so a claim swept here can be settled from
    # the history in this same sync.
    swept = sweep_stale(db, now, dry_run=dry_run)

    resolver = messages_sync.ContactResolver(client, messages_ref, db.collection(EXTRACTED_COLLECTION), join=True)
    skip_ids = messages_sync._ids_since(messages_ref, cursor)
    written = messages_sync.forward_pass(client, db, messages_ref, resolver, cursor, skip_ids, dry_run=dry_run)
    if dry_run:
        return {"dry_run": True, "would_write": written, "would_sweep": len(swept)}

    # Before anything else can fail: the forward pass has already moved the
    # cursor past these replies, so a cancellation skipped now is never made.
    replied = _replied_contacts(messages_ref, cursor, skip_ids)
    cancelled = sum(queue.cancel_for_contact(db, contact, "they replied", now) for contact in replied)
    if beat is not None:
        beat("refreshing contact stats")

    stats_refreshed = False
    if written:
        messages_sync.refresh_contact_stats(db, messages_ref, db.collection(ANALYSIS_COLLECTION), dry_run=False)
        stats_refreshed = True

    unknown_sent, unknown_failed = _resolve_unknowns(db, now)

    classified = new_leads = 0
    if classify is not None:
        if beat is not None:
            beat("staging new replies")
        rows = classify(db)
        classified = len(rows)
        new_leads = _alert_new_leads(db, rows, now)

    return {
        "stale_swept": len(swept),
        "written": written,
        "stats_refreshed": stats_refreshed,
        "replied_contacts": len(replied),
        "cancelled": cancelled,
        "unknown_sent": unknown_sent,
        "unknown_failed": unknown_failed,
        "classified": classified,
        "new_leads": new_leads,
    }


#: The `messages` fields reply detection reads: the contact, the direction,
#: and what `guards._usable_messages` needs to drop events and deleted ones.
_REPLY_FIELDS = ["contact_doc_id", "is_sender", "timestamp", "is_event", "deleted"]


def _replied_contacts(messages_ref, cursor, skip_ids) -> list[str]:
    """Every contact with an inbound message this sync just stored: one
    timestamped at or after `cursor` -- the forward pass reads from just
    before it -- other than `skip_ids`, those already stored from `cursor`
    on before the pass. Those were handled by the sync that stored them;
    counting one again would cancel what was queued in answer to it. ONE
    range query, projected to the fields read; a message with no resolved
    contact belongs to nobody.

    A message counts only if the guards would count it: it goes through
    `guards._usable_messages` itself, so a system event or a deleted message
    is not a reply here either. Cancelling on one would drop the contact's
    queued intro for good -- an intro id is create-only.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = messages_ref.where(filter=FieldFilter("timestamp", ">=", cursor)).select(_REPLY_FIELDS)
    fresh = [document.to_dict() or {} for document in query.stream() if document.id not in skip_ids]
    usable = guards._usable_messages(fresh)
    return sorted({body["contact_doc_id"] for body in usable if body["is_sender"] == 0 and body.get("contact_doc_id")})


def _resolve_unknowns(db, now) -> tuple[int, int]:
    """Resolve `unknown` items against the stored history (ruling P2-8) and
    return `(resolved_sent, resolved_failed)`.

    An item is `sent` when its contact has a stored OUTBOUND message
    timestamped at or after `sending_at - 5 min` -- the earliest such
    message is taken as the send, and its id recorded. Any outbound document
    counts, an event or a deleted message included: taking more as sent is
    the never-twice direction. Otherwise, once the attempt is MORE than 48 h
    old, it is `failed`. An item with no `sending_at` has no window to match
    against and is left for a human.
    """
    sent = failed = 0
    for item in queue.list_items(db, status=queue.UNKNOWN, limit=UNKNOWN_PAGE):
        sending_at = item.get("sending_at")
        if sending_at is None:
            continue
        message_id = _outbound_at_or_after(db, item["contact_doc_id"], sending_at - UNKNOWN_MATCH_SLACK)
        if message_id is not None:
            if queue.resolve_unknown(db, item["id"], sent=True, now=now, message_id=message_id):
                sent += 1
        elif now - sending_at > UNKNOWN_GIVE_UP:
            if queue.resolve_unknown(db, item["id"], sent=False, now=now, error=UNKNOWN_GIVE_UP_ERROR):
                failed += 1
    return sent, failed


def _outbound_at_or_after(db, doc_id, threshold) -> str | None:
    """The id of the contact's earliest stored outbound message timestamped
    at or after `threshold` (ties broken by id), or `None`.
    """
    outbound = [
        message
        for message in _stored_messages(db, doc_id)
        if message.get("is_sender") == 1 and message.get("timestamp") is not None and message["timestamp"] >= threshold
    ]
    if not outbound:
        return None
    return min(outbound, key=lambda message: (message["timestamp"], message["id"]))["id"]


#: The `messages` fields the jobs read about one contact: what
#: `guards.check_send` and `_outbound_at_or_after` reason over, plus
#: `contact_provider_id` -- who the contact is on LinkedIn, which
#: `messages_sync` stores on every message it attributes to them, and which
#: `_chat_verdict` checks a chat against. `pipeline.load_messages` projects
#: no `contact_provider_id` (and adds `text`, which nothing here reads).
_CONTACT_MESSAGE_FIELDS = [
    "chat_id", "contact_doc_id", "contact_provider_id", "is_sender", "timestamp", "is_event", "deleted",
]


def _stored_messages(db, doc_id) -> list[dict]:
    """The contact's stored `messages` documents as dicts, each with its
    `id`, projected to `_CONTACT_MESSAGE_FIELDS` -- one equality query on
    `contact_doc_id`, the single-field index `pipeline.load_messages` uses.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = (
        db.collection(MESSAGES_COLLECTION)
        .where(filter=FieldFilter("contact_doc_id", "==", doc_id))
        .select(_CONTACT_MESSAGE_FIELDS)
    )
    return [{**(document.to_dict() or {}), "id": document.id} for document in query.stream()]


#: The five row fields a lead alert carries. `transcript` -- the whole
#: conversation, written partly by a stranger -- is deliberately not one.
_LEAD_CONTEXT_FIELDS = ("doc_id", "previous_stage", "stage", "reason", "last_inbound")


def _alert_new_leads(db, rows, now) -> int:
    """One `lead` alert per row whose stage CHANGED to `lead`, keyed
    `{doc_id}:{last_inbound}` -- create-only (ruling P2-14), so re-reading
    the same newest inbound message never alerts twice, while a later
    message that makes them a lead again does. Returns how many were
    created.
    """
    created = 0
    for row in rows:
        if row.get("stage") != "lead" or row.get("previous_stage") == "lead":
            continue
        context = {field: row.get(field) for field in _LEAD_CONTEXT_FIELDS}
        question = (
            f"{row.get('doc_id')} is now a lead (previously {row.get('previous_stage') or 'unclassified'}). "
            "Read the conversation and decide the reply."
        )
        key = f"{row.get('doc_id')}:{row.get('last_inbound')}"
        if decisions.raise_alert(db, "lead", key, question, context, now):
            created += 1
    return created


#: At most this many Gemini classifications in one `default_classify` run.
CLASSIFY_LIMIT = 50


def default_classify(db) -> list[dict]:
    """Classify every contact whose newest inbound message is not yet
    classified -- at most `CLASSIFY_LIMIT` of them, newest conversations
    first -- and return the pipeline's rows.

    `pipeline.run_pipeline` keys each contact on its newest inbound message,
    so a contact already classified on that message costs no Gemini call.
    The cap (ruling P5-4) bounds the Gemini calls of one sync: a backlog --
    after a classification notebook erased stages, say -- is worked through
    over several syncs, 50 at a time. The rows carry a full `transcript`;
    `sync` reads five fields from each and copies nothing else.

    `asyncio.run` needs a thread with no running event loop -- a sync `def`
    endpoint (run in a worker thread) or the CLI, never an `async def`
    endpoint. A new Gemini client per call, because its async pool binds to
    the loop it first runs on (`clients.gemini_client`).
    """
    import pipeline

    from linkedinmcp import clients

    run = asyncio.run(pipeline.run_pipeline(db, clients.gemini_client(), limit=CLASSIFY_LIMIT))
    return run.rows


# =============================================================================
# The stale-claim sweep (sync, daily and tick)
# =============================================================================

#: A claim is held for one LinkedIn call, and `send_message` / `start_chat`
#: pace themselves before it: 20-40 s, and 2-5 min on a random one in ten
#: (`lib/unipile/pacing.py`), plus a 30 s HTTP timeout per phase -- about
#: seven minutes at worst. A claim older than this was never settled: the
#: process died between claim and settle.
STALE_CLAIM_AGE = timedelta(minutes=10)
STALE_CLAIM_REASON = "claimed and never settled"


def sweep_stale(db, now, *, dry_run=False) -> list[str]:
    """Move every `sending` item claimed MORE than ten minutes before `now`
    to `unknown`, with one `unknown_send` alert each, and return the ids
    moved. Under `dry_run`, move nothing and return the ids that would be.

    `unknown`, never back to `approved`: nobody knows whether LinkedIn
    accepted the message before the process died, so resending could send it
    twice. `sync` resolves it from the stored history later (ruling P2-8).

    The alert is written BEFORE the status changes, because only `sending`
    items are swept: once an item is `unknown` nothing would raise its alert
    again. If the alert write fails, the item is still `sending` and the
    next sweep tries both again. If the status change fails after it, the
    next sweep's `raise_alert` finds the alert and writes nothing -- it is
    keyed by the queue id and create-only (ruling P2-14) -- so either way
    the human gets one alert. An item that settles between the scan and the
    status change -- a send still in flight after ten minutes, a threshold
    set above the pacing's worst case of about seven -- keeps an alert about
    a send that did settle.
    """
    stale = queue.stale_sending(db, now - STALE_CLAIM_AGE)
    if dry_run:
        return [item["id"] for item in stale]
    swept = []
    for item in stale:
        sending_at = item.get("sending_at")
        decisions.raise_alert(
            db,
            "unknown_send",
            item["id"],
            (
                f"The send of {item['id']} to {item.get('contact_doc_id')} was claimed and never settled, so "
                "LinkedIn may or may not have delivered it. Sync marks it sent if the message turns up in "
                "the history, and failed if none does within 48 hours."
            ),
            {
                "queue_id": item["id"],
                "contact_doc_id": item.get("contact_doc_id"),
                "kind": item.get("kind"),
                "sending_at": sending_at.isoformat() if sending_at is not None else None,
                "reason": STALE_CLAIM_REASON,
            },
            now,
        )
        if queue.mark_unknown(db, item["id"], STALE_CLAIM_REASON, now):
            swept.append(item["id"])
    return swept


# =============================================================================
# daily
# =============================================================================

#: The `analysis` fields `functions.select_intro_candidates` reads -- the
#: projection `send-intros.ipynb` Phase B streams.
_CANDIDATE_FIELDS = ["industry", "seniority", "handling", "sent_total", "intro_sent_at"]

def _intro_gap(rng, settings) -> timedelta:
    """Ruling P5-3: one random gap between a queued intro's `due_at` and
    the next one's -- the first measured from the run's `now` -- drawn
    uniformly between `settings.intro_gap_min_minutes` and
    `settings.intro_gap_max_minutes`.

    Both are 0 by default since 2026-09-14: the intros are due at once and
    `steps.send_messages` spaces the sends. The gap is never under
    `MIN_INTRO_GAP`, so intros one run queues keep its newest-connection-
    first order in `queue.next_due`, which would otherwise break the tie
    by id.
    """
    drawn = timedelta(seconds=rng.uniform(settings.intro_gap_min_minutes * 60, settings.intro_gap_max_minutes * 60))
    return max(drawn, MIN_INTRO_GAP)


#: The smallest gap between two intros' due times (`_intro_gap`).
MIN_INTRO_GAP = timedelta(milliseconds=1)


def daily(db, client, settings, now, *, dry_run=False, state=None, rng=None) -> dict:
    """Plan the day: queue an intro for new first-degree connections in a
    target industry -- what `send-intros.ipynb` Phases B-D select, queued
    rather than sent, so `tick` sends them one at a time under every guard --
    and (task 3a) queue a profile fetch for new connections generally, for
    task 3b's tick to work through one at a time.

    1. Sweep stale claims (`sweep_stale`).
    2. Read `templates/intro.md` and refuse to go on if `guards.validate_text`
       does -- a broken template is a stop, not a warning. This runs before
       any LinkedIn read, so a broken template costs no API calls.
    3. Map every open chat `attendee_provider_id -> chat_id` (first wins)
       and list the relations.
    4. Holding the `send_intro` step's lock (`monitor.acquire_lock`), queue
       the day's intros with `plan_intros`: connections made within
       `settings.intro_connection_days` (0: every eligible connection), up
       to what is left of `settings.intro_daily_cap` today. When a
       `send_intro` job a person started holds the lock, the intros are
       skipped this run (`intro_step_busy`) -- that job is doing them.
    5. Enqueue a `fetch_queue` entry for every relation connected within
       `settings.new_connection_days` whose profile is not already in
       `extracted` (ledger ruling P3-1) -- see `_enumerate_new_connections`.
       Independent of step 4: every target-industry candidate is also a
       fetch candidate, but so is everyone else newly connected.
    6. Refresh the contact stats once, whatever happened above -- the repair
       for fields a classification notebook erased.

    The intro cap is per local day (MCP v2): intros queued earlier today --
    by an earlier run or by a person's `send_intro` -- count against it. A
    second run the same day adds nothing for anyone already queued (their
    intros are open, and a connection already in `fetch_queue` is a
    create-only id). Under `dry_run` nothing is swept, locked, enqueued or
    refreshed; the summary lists the intro ids that would have been created
    and counts the fetch entries that would.
    """
    state = _default_state(db, now, state)
    rng = rng if rng is not None else random.Random()
    return _run_job(
        db, "daily", now, settings=settings, dry_run=dry_run, client=client, state=state,
        body=lambda: _daily(db, client, settings, now, dry_run=dry_run, state=state, rng=rng),
    )


def _daily(db, client, settings, now, *, dry_run, state, rng) -> dict:
    import functions
    import messages_sync

    swept = sweep_stale(db, now, dry_run=dry_run)
    text = _intro_text(settings)

    chat_ids = open_chat_ids(client)
    relations = list(client.users.iter_relations())

    holder = f"daily:{now.astimezone(UTC):%Y%m%dT%H%M%S%fZ}"
    busy = None if dry_run else monitor.acquire_lock(db, INTRO_STEP, holder, now)
    if busy is None:
        try:
            planned = plan_intros(
                db, settings, now, relations=relations, chat_ids=chat_ids, text=text,
                days=settings.intro_connection_days, dry_run=dry_run, state=state, rng=rng, created_by="daily",
            )
        finally:
            if not dry_run:
                monitor.release_lock(db, INTRO_STEP, holder)
    else:
        planned = {
            "candidates": 0, "open_items": 0, "skipped": dict.fromkeys(functions.SKIP_REASONS, 0),
            "not_new": 0, "cap": None, "created": [],
        }
    created = planned["created"]

    fetch_enqueued, fetch_unusable_slug = _enumerate_new_connections(db, relations, settings, now, dry_run=dry_run)

    analysis_ref = db.collection(ANALYSIS_COLLECTION)
    messages_sync.refresh_contact_stats(db, db.collection(MESSAGES_COLLECTION), analysis_ref, dry_run=dry_run)

    summary: dict = {
        "candidates": planned["candidates"],
        "open_items": planned["open_items"],
        "skipped": planned["skipped"],
        "not_new": planned["not_new"],
        "intro_cap": planned["cap"],
    }
    if busy is not None:
        summary["intro_step_busy"] = busy["job_id"]
    if dry_run:
        summary.update({
            "dry_run": True, "would_enqueue": created, "would_sweep": len(swept), "would_fetch": fetch_enqueued,
        })
    else:
        summary.update({"enqueued": len(created), "stale_swept": len(swept), "fetch_enqueued": fetch_enqueued})
    summary["fetch_unusable_slug"] = fetch_unusable_slug
    summary.update({"queue": queue.counts(db), "decisions": decisions.counts(db), "fetch_queue": fetch_queue.counts(db)})
    return summary


#: The step the intro planning belongs to: the `send_intro` job a person
#: starts (`steps.py`) and the daily job share its lock (`monitor`).
INTRO_STEP = "send_intro"


def open_chat_ids(client) -> dict[str, str]:
    """Every open chat on LinkedIn as `attendee_provider_id -> chat_id`,
    first one wins: the live guard `select_intro_candidates` relies on."""
    chat_ids: dict[str, str] = {}
    for conversation in client.messaging.iter_chats():
        if conversation.attendee_provider_id:
            chat_ids.setdefault(conversation.attendee_provider_id, conversation.id)
    return chat_ids


def _local_midnight(now, tz: str):
    """The start of `now`'s local day in `tz`, in UTC."""
    local = now.astimezone(ZoneInfo(tz))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def intros_created_today(db, settings, now) -> int:
    """How many intro items were queued since local midnight -- by the daily
    job or by a person's `send_intro` -- the count the per-day cap spends.

    One single-field range query on `created_at`; the kind is checked in
    Python, since a second filtered field would need a composite index this
    service never creates (ruling P2-3).
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    since = _local_midnight(now, settings.tz)
    query = db.collection(queue.QUEUE_COLLECTION).where(filter=FieldFilter("created_at", ">=", since))
    return sum(1 for snapshot in query.stream() if (snapshot.to_dict() or {}).get("kind") == "intro")


def _iso_utc(value):
    return value.astimezone(UTC).isoformat() if value is not None else None


def plan_intros(
    db, settings, now, *, relations, chat_ids, text, days, industries=None, seniorities=None,
    max_count=None, doc_ids=None, dry_run=False, state, rng, created_by, tags=None,
) -> dict:
    """Choose who gets the intro and queue it -- `send-intros.ipynb` Phases
    B-D, queued rather than sent. The daily job and the `send_intro` step
    both call this, with the caller already holding the `send_intro` lock.

    1. `functions.select_intro_candidates` over `relations`, the live
       `chat_ids` and the five `analysis` fields it reads -- `industries`
       (default: every configured target) and `seniorities` (default: any)
       narrowing the targets. Newest connection first.
    2. With `days` > 0, only connections made within that many days; one
       with no connection date is not new. `not_new` counts the rest.
    3. With `doc_ids`, only those contacts; `not_eligible` names the ones
       asked for that step 1 or 2 left out.
    4. A candidate with an OPEN queue item is dropped (`open_items`).
    5. Queue `intro:{doc_id}` for the rest in order -- at most `max_count`,
       and never more than what is left of `settings.intro_daily_cap`
       today (`intros_created_today`). An id that already exists -- an
       intro cancelled or failed earlier -- is passed over and does not
       count: a contact gets one intro, ever. Each is due one random gap
       (`_intro_gap`: `settings.intro_gap_min_minutes` to
       `intro_gap_max_minutes`, both 0 by default) after the last one
       queued, the first one gap after `now` (ruling P5-3). Each carries `tags`
       (already cleaned; `[]` without them) for campaign tracking.

    Under `dry_run` nothing is written; `created` lists the ids that would
    be. Returns `candidates`, `open_items`, `skipped` (step 1's tally),
    `not_new`, `not_eligible`, `cap` (`per_day`, `used_today`,
    `remaining`), `created` (queue ids) and `rows`, one per intro.
    """
    import functions

    analysis_ref = db.collection(ANALYSIS_COLLECTION)
    contacts = {document.id: document.to_dict() or {} for document in analysis_ref.select(_CANDIDATE_FIELDS).stream()}
    candidates, skipped = functions.select_intro_candidates(
        relations,
        contacts,
        chat_ids,
        industries=set(settings.target_industries if industries is None else industries),
        seniorities=set(seniorities) if seniorities else None,
        holds=functions.HANDLING_HOLDS,
    )

    not_new = 0
    if days:
        cutoff = now - timedelta(days=days)
        recent = [
            candidate for candidate in candidates
            if candidate["connected_at"] is not None and candidate["connected_at"] >= cutoff
        ]
        not_new = len(candidates) - len(recent)
        candidates = recent

    not_eligible: list[str] = []
    if doc_ids is not None:
        wanted = set(doc_ids)
        not_eligible = sorted(wanted - {candidate["doc_id"] for candidate in candidates})
        candidates = [candidate for candidate in candidates if candidate["doc_id"] in wanted]

    open_ids = queue.open_contact_ids(db)
    eligible = [candidate for candidate in candidates if candidate["doc_id"] not in open_ids]

    used_today = intros_created_today(db, settings, now)
    remaining = max(0, settings.intro_daily_cap - used_today)
    allowance = remaining if max_count is None else min(max_count, remaining)

    require_approval = state.require_approval(settings.require_approval)
    created: list[str] = []
    rows: list[dict] = []
    last_due = now
    for candidate in eligible:
        if len(created) >= allowance:
            break
        queue_id = queue.intro_id(candidate["doc_id"])
        row = {
            "doc_id": candidate["doc_id"],
            "name": candidate["name"],
            "industry": candidate["industry"],
            "seniority": candidate["seniority"],
            "connected_at": _iso_utc(candidate["connected_at"]),
            "queue_id": queue_id,
        }
        if dry_run:
            if queue.get(db, queue_id) is None:
                created.append(queue_id)
                rows.append(row)
            continue
        due_at = last_due + _intro_gap(rng, settings)
        item, was_created = queue.enqueue(
            db,
            queue_id,
            {
                "contact_doc_id": candidate["doc_id"],
                "kind": "intro",
                "text": text,
                "provider_id": candidate["provider_id"],
                "chat_id": candidate["chat_id"],
                "name": candidate["name"],
                "profile_url": candidate["profile_url"],
                "campaign": "intro",
                "template_id": "intro",
                "created_by": created_by,
                "due_at": due_at,
                "tags": list(tags or []),
            },
            require_approval=require_approval,
            now=now,
        )
        if was_created:
            created.append(queue_id)
            last_due = due_at
            rows.append({**row, "status": item["status"], "due_at": _iso_utc(due_at)})

    return {
        "candidates": len(eligible),
        "open_items": len(candidates) - len(eligible),
        "skipped": skipped,
        "not_new": not_new,
        "not_eligible": not_eligible,
        "cap": {"per_day": settings.intro_daily_cap, "used_today": used_today, "remaining": remaining},
        "created": created,
        "rows": rows,
    }


def _enumerate_new_connections(db, relations, settings, now, *, dry_run) -> tuple[int, int]:
    """Queue one `fetch_queue` entry per recent, unstored connection (ledger
    ruling P3-1) -- task 3b's tick fetches and classifies them one at a
    time. Returns `(fetch_enqueued, fetch_unusable_slug)`: how many entries
    were (or, under `dry_run`, would be) created, and how many relations
    carried a slug that is not usable as a Firestore document id
    (`fetch_queue.usable_slug`: empty, containing `/`, `.` or `..`,
    `^__.*__$`, or over 1,500 bytes of UTF-8).

    A relation is skipped, silently, when its `public_identifier` is empty,
    it is already in `extracted` (the profile is stored), it carries no
    `created_at`, or `created_at` is older than `settings.new_connection_days`.
    Everything else is enqueued at the deterministic id `fetch_queue`
    already keys on (the slug itself), so a second run the same day creates
    nothing new for a connection the first run already queued.

    Under `dry_run`, nothing is written. `fetch_queue.usable_slug` is
    checked FIRST, exactly where the real branch's `fetch_queue.enqueue`
    raises its own `ValueError` on the same slugs -- a real Firestore client
    raises constructing a document reference for a slug containing `/`, so a
    dry run must reject it before ever calling `fetch_queue.get`, not only
    the real run. Whether a usable slug WOULD be created is then read with
    `fetch_queue.get`, exactly as the intro loop above checks `queue.get`
    for its own `would_enqueue` count.
    """
    stored_ids = {document.id for document in db.collection(EXTRACTED_COLLECTION).select([]).stream()}
    cutoff = now - timedelta(days=settings.new_connection_days)

    fetch_enqueued = 0
    fetch_unusable_slug = 0
    for relation in relations:
        slug = relation.public_identifier
        if not slug or slug in stored_ids:
            continue
        connected_at = relation.created_at
        if connected_at is None or connected_at < cutoff:
            continue

        if dry_run:
            if not fetch_queue.usable_slug(slug):
                fetch_unusable_slug += 1
                continue
            if fetch_queue.get(db, slug) is None:
                fetch_enqueued += 1
            continue

        name = f"{relation.first_name or ''} {relation.last_name or ''}".strip()
        try:
            created = fetch_queue.enqueue(
                db, slug, provider_id=relation.provider_id, name=name, connected_at=connected_at, now=now,
            )
        except ValueError:
            fetch_unusable_slug += 1
            continue
        if created:
            fetch_enqueued += 1

    return fetch_enqueued, fetch_unusable_slug


def _intro_text(settings) -> str:
    """`templates_dir/intro.md`, stripped -- the notebook's message (ruling
    P2-16) -- or `ValueError` naming the guard's reason when it could not be
    sent as it stands.
    """
    path = settings.templates_dir / "intro.md"
    text = path.read_text(encoding="utf-8").strip()
    verdict = guards.validate_text(text, settings)
    if not verdict.ok:
        raise ValueError(f"{path} cannot be sent: {verdict.reason} ({verdict.detail})")
    return text


# =============================================================================
# tick
# =============================================================================

#: Due items a tick considers -- each refused by a guard or lost to a
#: concurrent claim -- before it stops until the next tick.
MAX_ATTEMPTS = 10

#: Seconds of lease a tick must still hold before a LinkedIn write.
LEASE_FLOOR_SECONDS = 30

#: The rolling window the message cap applies to.
BUDGET_WINDOW = timedelta(hours=24)

#: How long sends pause after a 429 with no usable Retry-After, and after a
#: 401. A Retry-After longer than `MAX_RETRY_AFTER` is cut to it.
DEFAULT_PAUSE = timedelta(hours=1)
MAX_RETRY_AFTER = timedelta(hours=24)


def tick(db, client, settings, now, *, dry_run=False, state=None) -> dict:
    """Send AT MOST ONE queued message -- one LinkedIn write call, whatever
    it returns.

    Holding the tick lease (`{"skipped": "busy"}` without it), and releasing
    it in a `finally`:

    1. sweep stale claims (`sweep_stale`);
    2. skip while writes are blocked -- before the requested sync, whose
       reads a restricted account must not see every minute (ruling
       P5-4); the request waits for the block to be cleared;
    3. run a requested sync first (no classification) and clear exactly the
       request it served (ruling P2-17). A sync that raises stops the tick:
       sending without it could follow up on a reply it would have stored.
       Then skip while writes are blocked -- the sync may have met the
       restriction -- or sends are paused: a sends pause goes straight to
       step 7, without the message recount;
    4. reconcile the budget (`messages_last_24h`, ruling P2-9) and, when it
       is spent, skip sending and go to step 7;
    5. take due items one at a time, at most `MAX_ATTEMPTS`: check the text
       and the send (`guards`), that the item names what its route needs,
       and -- for a send into an existing chat -- that LinkedIn holds that
       chat as a one-to-one chat with the contact (`_chat_verdict`, one
       `get_chat` read); a refusal skips that item for good and the next is
       tried -- except a refusal about the ACCOUNT (`state:*`), which stops
       the tick and leaves the item `approved` (`state:sends_paused` then
       goes to step 7). A chat LinkedIn will not show (404, 422, a 403
       that is not a restriction) skips its item as `item:chat_unavailable`;
       any other `get_chat` failure stops the tick with the item still
       `approved`: no send goes into a chat nobody checked;
    6. with at least `LEASE_FLOOR_SECONDS` of lease left -- checked after
       the chat, so a slow `get_chat` cannot eat into the time the write
       needs -- claim the item and send it; see `_send` for what each
       outcome does to the queue;
    7. when nothing is due, or a stop above is about sending only (a sends
       pause, the message budget -- ruling P3-6), fetch at most one queued
       profile instead (`fetching.fetch_one`, its summary merged into the
       tick's; its pause is `fetch_until`, so a sends pause's `until`
       survives beside it). A tick that sent, or stopped for any other
       reason -- blocked writes, a busy or short lease, `MAX_ATTEMPTS`
       refusals -- does not fetch.

    Under `dry_run` no lease is taken and nothing is written: the same
    checks run over the same items and the summary says what WOULD happen,
    with `fetching.preview` where `_tick` would fetch. It may read from
    LinkedIn (the budget recount, the chat checks).
    """
    state = _default_state(db, now, state)
    return _run_job(
        db, "tick", now, settings=settings, dry_run=dry_run, client=client, state=state,
        body=lambda: (_dry_tick if dry_run else _tick)(db, client, settings, now, state=state),
    )


def _tick(db, client, settings, now, *, state) -> dict:
    owner = state.acquire_tick_lease()
    if owner is None:
        return {"skipped": "busy"}
    try:
        return _tick_holding_lease(db, client, settings, now, state=state, owner=owner)
    finally:
        state.release_tick_lease(owner)


def _tick_holding_lease(db, client, settings, now, *, state, owner, fetch=True) -> dict:
    """`tick`'s steps 1 to 7 for a caller holding the tick lease. With
    `fetch` false -- `steps.send_messages`, which sends and never fetches --
    step 7 is left out: a pass that sends nothing just says why."""
    summary: dict = {"stale_swept": len(sweep_stale(db, now)), "synced": False}

    # Before the requested sync: its forward pass reads LinkedIn, and a
    # restricted account must not be read on every tick (ruling P5-4). The
    # request stays for the first tick after a human clears the block.
    if state.writes_blocked():
        return {**summary, "skipped": "writes_blocked"}

    seen = state.sync_requested()
    if seen is not None:
        sync(db, client, settings, now, state=state)
        state.clear_sync_request(seen)
        summary["synced"] = True

    def fetch_instead(stop: dict) -> dict:
        """Nothing will be sent: fetch at most one profile instead."""
        if not fetch:
            return {**summary, **stop}
        return {**summary, **stop, **fetching.fetch_one(db, client, settings, now, state=state, owner=owner)}

    stopped = _stopped_by_state(state)
    if stopped is not None:
        return fetch_instead(stopped) if _stops_sending_only(stopped) else {**summary, **stopped}

    sent_24h = messages_last_24h(db, client, state, settings, now)
    summary["sent_24h"] = sent_24h
    client.budget.reconcile(message=sent_24h)
    if client.budget.remaining("message") <= 0:
        return fetch_instead({"skipped": "budget"})

    summary["items_skipped"] = 0
    for _attempt in range(MAX_ATTEMPTS):
        item = queue.next_due(db, now)
        if item is None:
            return fetch_instead({"idle": True})
        verdict = _check_item(db, client, item, settings, now, state)
        if not verdict.ok:
            if verdict.reason.startswith("state:"):
                refusal = _state_refusal(verdict, state)
                return fetch_instead(refusal) if _stops_sending_only(refusal) else {**summary, **refusal}
            queue.mark_skipped(db, item["id"], verdict.reason, now)
            summary["items_skipped"] += 1
            continue
        if state.lease_remaining(owner) < LEASE_FLOOR_SECONDS:
            return {**summary, "stopped": "lease_short"}
        claimed = queue.claim(db, item["id"], owner, now)
        if claimed is None:
            continue
        return {**summary, **_send(db, client, settings, now, state=state, item=claimed, owner=owner)}
    return {**summary, "stopped": "max_attempts"}


#: Ruling P3-6: a tick stopped for a reason about sending messages only -- a
#: sends pause, the message budget -- still fetches a profile
#: (`fetching.fetch_one`; the dry tick, `fetching.preview`): the profile
#: budget and the fetch pause are their own. The budget stop fetches
#: directly; of the account-state stops (`_stopped_by_state`,
#: `_state_refusal`) only a sends pause is about sending only. Blocked
#: writes -- a restricted account -- and any other state reason stop the
#: fetch too.
_SENDING_ONLY_STATE_STOPS = frozenset({"sends_paused"})


def _stops_sending_only(stop: dict) -> bool:
    """Whether the account-state stop `stop` -- `{"skipped": ...}` from
    `_stopped_by_state` or `_state_refusal` -- stops sending only (ruling
    P3-6)."""
    return stop.get("skipped") in _SENDING_ONLY_STATE_STOPS


def _stopped_by_state(state) -> dict | None:
    """`{"skipped": ...}` when writes are blocked or sends are paused."""
    if state.writes_blocked():
        return {"skipped": "writes_blocked"}
    until = state.sends_paused_until()
    if until is not None:
        return {"skipped": "sends_paused", "until": until.isoformat()}
    return None


def _state_refusal(verdict, state) -> dict:
    """The summary for a guard refusal about the account (`state:*`): the
    same `{"skipped": ...}` the tick's own state check returns. Such a
    refusal can reach `check_send` even after that check passed -- it
    compares a pause with `now`, the state compares with its own clock, and
    writes can be blocked while the tick recounts -- so it stops the tick
    rather than skip an item that did nothing wrong.
    """
    reason = verdict.reason.removeprefix("state:")
    if reason == "sends_paused":
        until = state.read().get("sends_paused_until")
        return {"skipped": reason, "until": until.isoformat() if until is not None else None}
    return {"skipped": reason}


def messages_last_24h(db, client, state, settings, now, *, dry_run=False) -> int:
    """How many messages this account sent in the 24 h before `now`, as
    ruling P2-9 counts them: LinkedIn's own count, cached as the budget
    snapshot, plus this service's ledger `sent`/`unknown` rows since the
    snapshot was taken.

    The snapshot is re-taken from LinkedIn (`count_messages_sent_since`, a
    walk of recent chats) when it is absent or MORE than
    `settings.budget_snapshot_max_age_minutes` old; otherwise LinkedIn is
    not asked at all. `unknown` rows count because the safe assumption about
    an unknown send is that it went. Under `dry_run` a re-taken count is
    returned without being stored.
    """
    snapshot = state.budget_snapshot()
    max_age = timedelta(minutes=settings.budget_snapshot_max_age_minutes)
    if snapshot is not None and now - snapshot[1] <= max_age:
        count, taken_at = snapshot
        return count + ledger.count_since(db, "message", taken_at, results=("sent", "unknown"))
    count = client.messaging.count_messages_sent_since(now - BUDGET_WINDOW)
    if not dry_run:
        state.store_budget_snapshot(count, now)
    return count


def _check_item(db, client, item, settings, now, state):
    """The verdict on sending `item` now: `guards.validate_text`, then
    `guards.check_send` on the contact's full `analysis` document, stored
    messages and queue items, then `_route_verdict`, then `_chat_verdict`
    -- for a send into an existing chat, LinkedIn's own answer about it.

    A guard that RAISES -- a malformed datetime in some document -- is a
    refusal with reason `guard_error:<ExceptionClass>`, so one bad document
    skips one item instead of failing every later tick on it. Reading the
    data is outside that: a Firestore failure there is not the item's
    fault, and propagates. So does anything `get_chat` raises that is not
    about the chat (`_chat_verdict`): it comes before the claim, so the
    item stays `approved`, and the tick stops.
    """
    doc_id = item["contact_doc_id"]
    snapshot = db.collection(ANALYSIS_COLLECTION).document(doc_id).get()
    contact = (snapshot.to_dict() or {}) if snapshot.exists else None
    messages = _stored_messages(db, doc_id)
    queue_items = queue.items_for_contact(db, doc_id)
    runtime = state.read()
    try:
        verdict = guards.validate_text(item.get("text"), settings)
        if verdict.ok:
            verdict = guards.check_send(item, contact, messages, queue_items, runtime, settings, now)
    except Exception as error:
        return guards.Verdict(
            False, f"guard_error:{type(error).__name__}", "A guard raised on this item's stored data."
        )
    if not verdict.ok:
        return verdict
    verdict = _route_verdict(item)
    if not verdict.ok:
        return verdict
    return _chat_verdict(client, item, messages)


def _opens_chat(item) -> bool:
    """Ruling P2-16: an intro with no chat id opens one (`start_chat`);
    every other send goes into `item["chat_id"]` (`send_message`).
    """
    return item.get("kind") == "intro" and not item.get("chat_id")


def _route_verdict(item):
    """Whether `item` names what its route needs -- checked before the
    claim, so a malformed item is skipped instead of reaching LinkedIn as
    `/chats/None/messages` (a 400, which the outcome mapping could only call
    `unknown`).
    """
    if _opens_chat(item):
        if not item.get("provider_id"):
            return guards.Verdict(
                False, "item:no_provider_id", "An intro with no chat needs the contact's provider id to open one."
            )
    elif not item.get("chat_id"):
        return guards.Verdict(False, "item:no_chat_id", "Only an intro may open a chat; this item names none.")
    return guards.Verdict(True)


#: Unipile's chat `type` for a one-to-one conversation: the node SDK's
#: `ChatTypeSchema` is `{SINGLE: 0, GROUP: 1, CHANNEL: 2}`, and `type` is a
#: required field of its chat object (read 2026-09-10).
#: `lib.unipile.models.Chat` declares no `type`; the value arrives as one of
#: the model's extra fields (`model_extra`).
ONE_TO_ONE_CHAT_TYPE = 0

#: The skip reason for an item whose chat is not a one-to-one chat with its
#: contact, or cannot be checked (`_chat_verdict`).
CHAT_MISMATCH = "item:chat_mismatch"

#: The skip reason for an item whose chat LinkedIn will not show: a 404 (the
#: chat is gone), a 422, or a 403 that is not a restriction (`_chat_verdict`).
CHAT_UNAVAILABLE = "item:chat_unavailable"


def _chat_verdict(client, item, messages):
    """Whether the chat `item` would be sent into is a one-to-one chat with
    its contact, as LinkedIn itself answers it (final review FI2) -- asked
    before the claim, so a mismatch is skipped and never becomes `sending`
    or `unknown`. Only a send into an existing chat (`send_message`) is
    checked: an intro that opens one (`start_chat`) addresses the
    contact's provider id itself.

    `client.messaging.get_chat(chat_id)` must name the contact's provider
    id (`_contact_provider_id`) as its `attendee_provider_id`, and carry
    the one-to-one `type` (`ONE_TO_ONE_CHAT_TYPE`): a group whose attendee
    happens to be the contact is refused, and so is a response with no
    `type` at all. Anything else is `item:chat_mismatch` -- and so is a
    contact whose provider id is not known, without asking LinkedIn.

    `get_chat` answering 404, 422, or a 403 that is not a restriction is
    about this chat -- the same answers to a send mark the item `failed`
    (`_send`) -- so the item is skipped as `item:chat_unavailable`, and the
    queue behind it moves on. Anything else `get_chat` raises (an outage, a
    429, a restriction) propagates (`_check_item`).
    """
    from lib.unipile import errors as unipile_errors

    if _opens_chat(item):
        return guards.Verdict(True)
    provider_id = _contact_provider_id(item, messages)
    if provider_id is None:
        return guards.Verdict(
            False, CHAT_MISMATCH, "The contact's LinkedIn id is not known, so the chat cannot be checked."
        )
    try:
        conversation = client.messaging.get_chat(item["chat_id"])
    except (unipile_errors.NotFound, unipile_errors.UnprocessableError, unipile_errors.PermissionDenied) as error:
        if isinstance(error, unipile_errors.AccountRestricted):
            raise
        return guards.Verdict(
            False, CHAT_UNAVAILABLE, f"LinkedIn will not show this chat ({type(error).__name__})."
        )
    chat_type = (conversation.model_extra or {}).get("type")
    if conversation.attendee_provider_id != provider_id or chat_type != ONE_TO_ONE_CHAT_TYPE:
        return guards.Verdict(
            False, CHAT_MISMATCH, "LinkedIn says this chat is not a one-to-one chat with this contact."
        )
    return guards.Verdict(True)


def _contact_provider_id(item, messages) -> str | None:
    """Who `item`'s contact is on LinkedIn: the one `contact_provider_id`
    their usable stored messages (`guards._usable_messages`) agree on,
    across every chat of theirs; `None` when none carries one, or when
    they disagree.

    Every chat, never only the item's: `messages_sync` copies a message's
    `contact_provider_id` from its chat's `attendee_provider_id`, so a chat
    compared with ids read from that same chat would verify nothing. Usable
    messages only, the ones the chat was chosen from (`send_follow_up`, `send_reply`): an
    event in a group chat says nothing about who the contact is.

    An intro gets here only with no conversation -- a usable message
    refuses it first (`intro:conversation_exists`) -- so it is checked
    against its own `provider_id`, the relation's, which `start_chat`
    would have addressed.
    """
    if item.get("kind") == "intro":
        provider_id = item.get("provider_id")
        return provider_id if isinstance(provider_id, str) and provider_id else None
    known = set()
    for message in guards._usable_messages(messages):
        value = message.get("contact_provider_id")
        if isinstance(value, str) and value:
            known.add(value)
    return known.pop() if len(known) == 1 else None


def _send(db, client, settings, now, *, state, item, owner) -> dict:
    """Make the ONE LinkedIn call for a claimed `item`, and settle it.

    Only the call itself is inside the `try`. `settle(sent)` is outside it:
    if recording the success fails, the exception propagates, the item stays
    `sending`, and the sweep and `sync` take it from there -- the Firestore
    error is never mistaken for LinkedIn's answer. `Exception`, not
    `BaseException`: a SIGTERM mid-send leaves the item `sending` for the
    sweep too.

    A 2xx whose response carries no message or chat id is still sent.
    """
    route = "start_chat" if _opens_chat(item) else "send_message"
    outcome = {"item": item["id"], "kind": item.get("kind"), "route": route}
    try:
        if route == "start_chat":
            result = client.messaging.start_chat([item["provider_id"]], item["text"])
            message_id, chat_id = result.message_id, result.chat_id
        else:
            result = client.messaging.send_message(item["chat_id"], item["text"])
            message_id, chat_id = result.message_id, None
    except Exception as error:
        return {**outcome, **_after_failed_send(db, settings, now, state=state, item=item, owner=owner, error=error)}

    queue.settle(db, item["id"], queue.SENT, now=now, message_id=message_id, chat_id=chat_id)
    return {**outcome, "outcome": "sent", "message_id": message_id}


def _after_failed_send(db, settings, now, *, state, item, owner, error) -> dict:
    """Contract §3, row by row, most specific class first --
    `AccountRestricted` is a `PermissionDenied` and `AccountDisconnected` an
    `AuthenticationError`:

    - `BudgetExhausted`, `CircuitOpen` (raised before any request):
      release, stop.
    - `RateLimited` (a 429): release, pause sends for its Retry-After or an
      hour, counted from `_pause_start`.
    - `AccountRestricted` (a 403): release, then alert `restricted` and
      block writes (`_note_restriction`: nothing new when already blocked).
    - `AuthenticationError` incl. `AccountDisconnected` (a 401): release,
      pause an hour, alert `disconnected`.
    - `UnprocessableError` and subclasses, `NotFound`, any other
      `PermissionDenied` (a 4xx refusal): settle `failed`.
    - everything else -- `ServerError`, `httpx.TransportError`, a 400, any
      exception at all: settle `unknown` and alert `unknown_send`. Nobody
      knows whether LinkedIn accepted it, so it is never retried.

    The queue action comes first, then the rest of the row -- except for
    `unknown`, whose alert is written BEFORE the item is settled: if the
    alert write fails, the item is still `sending` and the stale sweep
    raises the alert when it moves it; if the settle fails after it, the
    sweep finds the alert already there (same key, create-only). `release`
    returning `False` means a sweep already moved the claim to `unknown`;
    that is where it stays.
    """
    from lib.unipile import errors as unipile_errors

    name = type(error).__name__
    queue_id = item["id"]
    contact_doc_id = item.get("contact_doc_id")

    if isinstance(error, (unipile_errors.BudgetExhausted, unipile_errors.CircuitOpen)):
        queue.release(db, queue_id, owner, now, name)
        return {"outcome": "released", "error": name}

    if isinstance(error, unipile_errors.RateLimited):
        queue.release(db, queue_id, owner, now, name)
        until = _pause_start(now, state) + _retry_after(error)
        state.pause_sends(until, f"LinkedIn rate limit ({name})")
        return {"outcome": "released", "error": name, "paused_until": until.isoformat()}

    if isinstance(error, unipile_errors.AccountRestricted):
        queue.release(db, queue_id, owner, now, name)
        _note_restriction(
            db,
            state,
            now,
            reason=f"LinkedIn restricted the account ({name})",
            question=(
                f"LinkedIn restricted the account while sending {queue_id}. Every send is blocked until a "
                "human clears the block; the message was not sent and is back in the queue."
            ),
            context={"queue_id": queue_id, "contact_doc_id": contact_doc_id, "error": name},
        )
        return {"outcome": "released", "error": name, "writes_blocked": True}

    if isinstance(error, unipile_errors.AuthenticationError):
        queue.release(db, queue_id, owner, now, name)
        until = now + DEFAULT_PAUSE
        state.pause_sends(until, f"LinkedIn account disconnected ({name})")
        decisions.raise_alert(
            db,
            "disconnected",
            _local_day(now, settings),
            (
                f"Unipile could not act for the LinkedIn account ({name}) -- it may need reconnecting. "
                f"Sends are paused until {until.isoformat()}; the message was not sent and is back in the queue."
            ),
            {"queue_id": queue_id, "contact_doc_id": contact_doc_id, "error": name, "paused_until": until.isoformat()},
            now,
        )
        return {"outcome": "released", "error": name, "paused_until": until.isoformat()}

    if isinstance(error, (unipile_errors.UnprocessableError, unipile_errors.NotFound, unipile_errors.PermissionDenied)):
        queue.settle(db, queue_id, queue.FAILED, now=now, error=name)
        return {"outcome": "failed", "error": name}

    decisions.raise_alert(
        db,
        "unknown_send",
        queue_id,
        (
            f"Sending {queue_id} to {contact_doc_id} failed with {name}, so LinkedIn may or may not have "
            "delivered it. It will not be retried. Sync marks it sent if the message turns up in the "
            "history, and failed if none does within 48 hours."
        ),
        {"queue_id": queue_id, "contact_doc_id": contact_doc_id, "kind": item.get("kind"), "error": name},
        now,
    )
    queue.settle(db, queue_id, queue.UNKNOWN, now=now, error=name)
    return {"outcome": "unknown", "error": name}


def _pause_start(now, state):
    """When a Retry-After pause starts: the later of the job's `now` -- when
    it started -- and the state's own clock, which a pause is compared
    with (ruling P5-4). The job may be minutes into a sync, a recount or a
    slow answer by the time LinkedIn says 429; counted from `now`, a short
    Retry-After could already be over when it is written, and the next
    tick would meet the same 429 at once."""
    return max(now, state.now())


def _retry_after(error) -> timedelta:
    """A 429's Retry-After as a pause: its seconds when they are a finite
    positive number (at most `MAX_RETRY_AFTER`), else `DEFAULT_PAUSE`.
    `float()` accepts "nan" and "inf", and `timedelta` refuses both.
    """
    seconds = getattr(error, "retry_after", None)
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        return DEFAULT_PAUSE
    return min(timedelta(seconds=seconds), MAX_RETRY_AFTER)


def _local_day(now, settings) -> str:
    """`YYYYMMDD` of `now` in `settings.tz` -- the key that makes a
    `disconnected` alert one per local day rather than one per tick (ruling
    P2-14).
    """
    return f"{clock.local_date(now, settings.tz):%Y%m%d}"


def _restriction_key(now) -> str:
    """The `restricted` alert's key: the moment the restriction was seen, in
    UTC, in the run-id format (`%Y%m%dT%H%M%S%fZ`). One alert per
    restriction, not per day: a restriction that returns after a human
    cleared the block is a new alert, even on the same day. A naive `now`
    raises `ValueError` rather than be read as local time.
    """
    if now.tzinfo is None:
        raise ValueError("_restriction_key: `now` must be timezone-aware")
    return f"{now.astimezone(UTC):%Y%m%dT%H%M%S%fZ}"


def _dry_tick(db, client, settings, now, *, state) -> dict:
    """What `_tick` would do, writing nothing: the would-be sweep and sync,
    the state and budget checks, then the due items in `queue.next_due`'s
    order (see `_due_items`), each checked without being marked (its chat
    check asks LinkedIn, as `_tick`'s does) until one
    would be sent -- and, when none would, or a stop is about sending only
    (ruling P3-6), the profile fetch `_tick` would make instead
    (`fetching.preview`: no LinkedIn call, no write).

    With `MAX_ATTEMPTS` or more due items and none sendable, it stops as
    `_tick` does: `_tick` checks `MAX_ATTEMPTS` items and stops without
    looking for another, so exactly `MAX_ATTEMPTS` refused items is
    `max_attempts` in both, not `idle`.
    """
    summary: dict = {
        "dry_run": True,
        "would_sweep": len(sweep_stale(db, now, dry_run=True)),
        # `_tick` runs no requested sync while writes are blocked.
        "would_sync": state.sync_requested() is not None and not state.writes_blocked(),
    }

    def preview_instead(stop: dict) -> dict:
        """Nothing would be sent: preview the profile fetch instead."""
        return {**summary, **stop, **fetching.preview(db, now, state=state)}

    stopped = _stopped_by_state(state)
    if stopped is not None:
        return preview_instead(stopped) if _stops_sending_only(stopped) else {**summary, **stopped}

    sent_24h = messages_last_24h(db, client, state, settings, now, dry_run=True)
    summary["sent_24h"] = sent_24h
    client.budget.reconcile(message=sent_24h)
    if client.budget.remaining("message") <= 0:
        return preview_instead({"skipped": "budget"})

    would_skip: dict[str, str] = {}
    summary["would_skip"] = would_skip
    due = _due_items(db, now)
    for item in due[:MAX_ATTEMPTS]:
        verdict = _check_item(db, client, item, settings, now, state)
        if verdict.ok:
            route = "start_chat" if _opens_chat(item) else "send_message"
            return {**summary, "would_send": item["id"], "kind": item.get("kind"), "route": route}
        if verdict.reason.startswith("state:"):
            refusal = _state_refusal(verdict, state)
            return preview_instead(refusal) if _stops_sending_only(refusal) else {**summary, **refusal}
        would_skip[item["id"]] = verdict.reason
    if len(due) >= MAX_ATTEMPTS:
        return {**summary, "stopped": "max_attempts"}
    return preview_instead({"idle": True})


def preview_sends(db, client, settings, now, *, state, limit) -> dict:
    """What `steps.send_messages` would send, writing nothing: the state and
    budget checks, then the approved items already due, in send order
    (`_due_items`), each checked as `_tick` checks it -- its chat check asks
    LinkedIn -- until `limit` would be sent or the day's message budget
    would run out.

    Returns `would_send` (rows) and `would_skip` (queue id -> reason), or a
    `stopped` reason when the account would stop every send. A preview marks
    nothing sent, so a second due message to a contact who would get one
    earlier in the run is listed too; the real run refuses it.
    """
    stopped = _stopped_by_state(state)
    if stopped is not None:
        return {"stopped": stopped.pop("skipped"), **stopped, "would_send": [], "would_skip": {}}
    sent_24h = messages_last_24h(db, client, state, settings, now, dry_run=True)
    client.budget.reconcile(message=sent_24h)
    room = min(limit, max(0, client.budget.remaining("message")))
    due = _due_items(db, now)
    would_send: list[dict] = []
    would_skip: dict[str, str] = {}
    for item in due:
        if len(would_send) >= room:
            break
        verdict = _check_item(db, client, item, settings, now, state)
        if verdict.ok:
            would_send.append({
                "queue_id": item["id"], "doc_id": item.get("contact_doc_id"), "name": item.get("name"),
                "kind": item.get("kind"), "route": "start_chat" if _opens_chat(item) else "send_message",
            })
        elif verdict.reason.startswith("state:"):
            refusal = _state_refusal(verdict, state)
            return {"stopped": refusal.pop("skipped"), **refusal, "would_send": would_send, "would_skip": would_skip}
        else:
            would_skip[item["id"]] = verdict.reason
    return {"sent_24h": sent_24h, "due": len(due), "would_send": would_send, "would_skip": would_skip}


def _due_items(db, now) -> list[dict]:
    """`approved` items due at or before `now`, in `queue.next_due`'s order
    (`due_at`, then `created_at`, then id) -- the order `_tick` meets them
    in, for a dry run that cannot mark one skipped to reach the next. Read
    through `queue.list_items`, so at most its 100 newest-created.
    """
    items = [
        item
        for item in queue.list_items(db, status=queue.APPROVED, limit=100)
        if item.get("due_at") is not None and item["due_at"] <= now
    ]
    items.sort(key=lambda item: (item["due_at"], item.get("created_at"), item["id"]))
    return items


# =============================================================================
# handle_unipile_webhook
# =============================================================================

#: A Firestore document id may not contain "/", be "." or "..", or match
#: `__.*__` -- the same rules `messages_sync._check_document_id` enforces --
#: and may be at most 1,500 bytes of UTF-8.
_RESERVED_DOCUMENT_ID = re.compile(r"^__.*__$")
_MAX_DOCUMENT_ID_BYTES = 1500


def handle_unipile_webhook(db, payload, now, *, state=None) -> dict:
    """Record that a sync is wanted because a new message arrived, and
    nothing more -- no sync work inside the request.

    Unipile (docs verified 2026-09-10) delivers six event types to one URL
    and delivers this account's OWN sent messages as `message_received` too,
    told apart by `account_info.user_id == sender.attendee_provider_id`. It
    expects a 200 within 30 s and retries up to five times otherwise, so
    every outcome here is a normal return -- an ignored delivery included --
    and a redelivery is told apart by the create-only
    `webhook_events/{message_id}` document.

    The sync is requested BEFORE that document is created. If the request
    fails, the error leaves the call, Unipile delivers again, and no
    document is there to turn the redelivery away as a duplicate. A
    redelivery therefore requests a sync again, which is harmless -- only
    `accepted` tells it apart.

    Returns `{"accepted": bool, "reason": str}`. A payload missing any key
    this needs, or whose `message_id` cannot be a document id, is ignored
    with a reason, never an exception: the scheduled sync still picks the
    message up, so ignoring is always safe.
    """
    if not isinstance(payload, dict):
        return _webhook_result(False, "payload_not_an_object")
    if payload.get("event") != "message_received":
        return _webhook_result(False, "event_ignored")

    our_id = _field(payload, "account_info", "user_id")
    sender_id = _field(payload, "sender", "attendee_provider_id")
    if not our_id or not sender_id:
        return _webhook_result(False, "sender_unknown")
    if our_id == sender_id:
        return _webhook_result(False, "own_message")

    message_id = payload.get("message_id")
    if not _usable_document_id(message_id):
        return _webhook_result(False, "no_message_id")

    _default_state(db, now, state).request_sync()

    chat_id = payload.get("chat_id")
    from google.api_core import exceptions as api_exceptions

    try:
        db.collection(WEBHOOK_EVENTS_COLLECTION).document(message_id).create(
            {"received_at": now, "chat_id": chat_id if isinstance(chat_id, str) else None}
        )
    except api_exceptions.Conflict:
        return _webhook_result(False, "duplicate")
    return _webhook_result(True, "sync_requested")


def _webhook_result(accepted: bool, reason: str) -> dict:
    return {"accepted": accepted, "reason": reason}


def _field(payload: dict, outer: str, inner: str) -> str | None:
    """`payload[outer][inner]` when it is a non-empty string, else `None` --
    including when `payload[outer]` is missing or not a dict.
    """
    container = payload.get(outer)
    if not isinstance(container, dict):
        return None
    value = container.get(inner)
    return value if isinstance(value, str) and value else None


def _usable_document_id(value) -> bool:
    """Whether `value` can name a Firestore document (see
    `_RESERVED_DOCUMENT_ID`). A string UTF-8 cannot encode -- a lone
    surrogate, which JSON can carry -- cannot, and is refused here rather
    than raise.
    """
    if not isinstance(value, str) or not value or "/" in value or value in (".", ".."):
        return False
    if _RESERVED_DOCUMENT_ID.match(value):
        return False
    try:
        return len(value.encode("utf-8")) <= _MAX_DOCUMENT_ID_BYTES
    except UnicodeEncodeError:
        return False
