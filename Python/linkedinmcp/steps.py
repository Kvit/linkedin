"""The six steps of the outreach process, as the MCP tools run them.

Each is what one of the user's own scripts does, with that script's
settings as the job's parameters, and each is built from the service's
existing job code -- nothing here re-implements a rule:

| step                | the user's script                       | built on                              |
|---------------------|-----------------------------------------|---------------------------------------|
| `sync_messages`     | `messages_sync.py` (+ `pipeline-classify` for new replies) | `jobs._sync`       |
| `get_contacts`      | `new-contacts.ipynb` Phases A-D         | `fetch_queue`, `fetching.fetch_one`   |
| `classify_contacts` | `new-contacts.ipynb` Phase E            | `profiles.classify_profile`           |
| `classify_stages`   | `pipeline-classify.py`                  | `pipeline.run_pipeline`               |
| `send_intro`        | `send-intros.ipynb` Phases B-D (queue)  | `jobs.plan_intros`                    |
| `send_messages`     | `send-intros.ipynb` Phase E (send)      | `jobs._tick_holding_lease`            |

A step is a plain function of one `monitor.Job` -- its `params`, the
clients and settings, `report` for progress -- returning a result dict: a
few counts, and at most `MAX_ROWS` rows naming who was affected. It runs
as a job (`monitor.run`), never inside a tool's own call: every step here
can take minutes.

A dry run views no LinkedIn profile, makes no Gemini call and writes
nothing -- `get_contacts` lists who it would fetch, `classify_contacts` and
`classify_stages` who they would classify, `send_intro` who would get the
intro, `send_messages` who would be sent a message. That is stricter than
the notebooks, whose dry runs still fetch and classify: here a dry run
spends none of the day's budget.
"""

import asyncio
import itertools
import random
import time
from datetime import UTC, datetime, timedelta

from linkedinmcp import clients, fetch_queue, fetching, jobs, monitor, queue

EXTRACTED_COLLECTION = "extracted"
ANALYSIS_COLLECTION = "analysis"

#: Rows a result carries at most; counts cover the rest.
MAX_ROWS = 50

#: Profiles one `get_contacts` job fetches at most. At the notebook's pace
#: -- 20 to 40 s between profiles -- ten take about five minutes, well inside
#: a job's 30-minute ceiling (`monitor.DISPATCH_DEADLINE_SECONDS`).
MAX_PROFILES = 10

#: The gap between two profile fetches: `new-contacts.ipynb`'s own
#: `HumanCadence` bounds, `UNIPILE_MIN_DELAY_SECONDS` and
#: `UNIPILE_MAX_DELAY_SECONDS` by default. The service forces the Unipile
#: client's own pacing to zero (`settings.SERVICE_PACING`), so a job doing
#: several LinkedIn calls in a row spaces them itself.
FETCH_GAP_MIN_SECONDS = 20.0
FETCH_GAP_MAX_SECONDS = 40.0

#: How often, and how long apart, `get_contacts` and `send_messages` try for
#: the tick lease another job may be holding for a few seconds.
LEASE_TRIES = 3
LEASE_RETRY_SECONDS = 5.0

#: Profiles or conversations one classification job sends to Gemini at most.
MAX_CLASSIFY = 50

#: `analysis` ids per `get_all` when checking what is classified already.
CLASSIFIED_PAGE = 250

#: Module attributes read at call time, so a test can replace them: the
#: sleep between profile fetches and the random source of its length.
_sleep = time.sleep
_random = random.Random()


def _iso(value):
    return value.astimezone(UTC).isoformat() if isinstance(value, datetime) else value


# =============================================================================
# sync_messages
# =============================================================================


def sync_messages(job) -> dict:
    """`messages_sync.py`: mirror the messages LinkedIn holds past the newest
    stored one, then react to them (`jobs._sync`) -- cancel queued items for anyone who replied, refresh
    the contact stats, resolve `unknown` sends and, with `classify`, stage
    the new replies with Gemini (`pipeline-classify`) and raise one `lead`
    alert per new lead. It heartbeats between those phases. A dry run only
    counts what the pass would write."""
    params = job.params
    dry_run = bool(params.get("dry_run", True))
    classify = jobs.default_classify if params.get("classify", True) and not dry_run else None
    return jobs._sync(
        job.db, job.client(), job.now, dry_run=dry_run, classify=classify, beat=lambda note: job.report(note=note),
    )


# =============================================================================
# get_contacts
# =============================================================================


def _connection_row(relation, entry) -> dict:
    name = f"{relation.first_name or ''} {relation.last_name or ''}".strip()
    return {
        "doc_id": relation.public_identifier,
        "name": name or None,
        "headline": relation.headline or None,
        "connected_at": _iso(relation.created_at),
        "fetch": entry.get("status") if entry else None,
    }


def _fetch_gap() -> float:
    return _random.uniform(FETCH_GAP_MIN_SECONDS, FETCH_GAP_MAX_SECONDS)


def _acquire_tick_lease(state) -> str | None:
    for attempt in range(LEASE_TRIES):
        owner = state.acquire_tick_lease()
        if owner is not None:
            return owner
        if attempt + 1 < LEASE_TRIES:
            _sleep(LEASE_RETRY_SECONDS)
    return None


#: `fetching.fetch_one` outcomes after which no further profile is tried.
_FETCH_STOPS = frozenset({"writes_blocked", "paused", "budget", "idle", "lease_short"})


def get_contacts(job) -> dict:
    """`new-contacts.ipynb` Phases A-D: the first-degree connections whose
    profile is not stored yet -- every one, or with `days`, those made in
    the last `days` days -- then up to `max_profiles` of their profiles
    fetched and stored.

    1. List the relations and drop those already in `extracted`, those
       outside the window, and slugs no Firestore document can have.
    2. Add each to `fetch_queue` (create-only), so whatever this job does
       not fetch, a later `get_contacts` will.
    3. Fetch up to `max_profiles` of the connections step 1 listed -- never
       the rest of the queue, so `days` bounds the profile views too -- in
       the fetch queue's order, the newest connection first
       (`fetch_queue.next_queued`), 20 to 40 s apart: each through
       `fetching.fetch_one` with `classify=False`, holding the tick lease
       for that one profile so no `send_messages` send runs beside it.
       Stored, not classified -- `classify_contacts` does that. A connection
       the fetch queue is done with (a profile too short to store, one given
       up) is not fetched again. Stops early when none is left, fetches are
       paused, the day's profile budget is spent or writes are blocked.

    The result lists the new connections (`connections`), what each fetch
    did (`fetched`) and the slugs now stored (`stored_slugs`) -- exactly
    what to pass to `classify_contacts(doc_ids=...)`. A dry run stops after
    step 1: no queue entry, no profile view; its `would_fetch` counts what
    step 3 would fetch.
    """
    params = job.params
    days = int(params.get("days", 0))
    max_profiles = int(params.get("max_profiles", MAX_PROFILES))
    dry_run = bool(params.get("dry_run", True))
    db, now = job.db, job.now
    client = job.client()

    relations = list(client.users.iter_relations())
    stored_ids = {document.id for document in db.collection(EXTRACTED_COLLECTION).select([]).stream()}
    cutoff = None if days == 0 else now - timedelta(days=days)
    new, unusable = [], 0
    for relation in relations:
        slug = relation.public_identifier
        if not slug or slug in stored_ids:
            continue
        if cutoff is not None and (relation.created_at is None or relation.created_at < cutoff):
            continue
        if not fetch_queue.usable_slug(slug):
            unusable += 1
            continue
        new.append(relation)
    new.sort(key=lambda relation: relation.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)

    listed = {"new_connections": len(new), "unusable_slugs": unusable}
    rows = [_connection_row(relation, fetch_queue.get(db, relation.public_identifier)) for relation in new[:MAX_ROWS]]
    job.report(done=0, total=max_profiles, note=f"{len(new)} new connections")
    scope = {relation.public_identifier for relation in new}
    if dry_run:
        fetchable = len(scope - fetch_queue.settled_ids(db))
        return {"dry_run": True, **listed, "would_fetch": min(max_profiles, fetchable), "connections": rows}

    enqueued = 0
    for relation in new:
        name = f"{relation.first_name or ''} {relation.last_name or ''}".strip()
        if fetch_queue.enqueue(
            db, relation.public_identifier, provider_id=relation.provider_id, name=name,
            connected_at=relation.created_at, now=now,
        ):
            enqueued += 1

    fetched: list[dict] = []
    stopped = None
    for index in range(max_profiles):
        if index:
            _sleep(_fetch_gap())
        at = job.state.now()
        item = fetch_queue.next_queued(db, at, slugs=scope)
        if item is None:
            stopped = "idle"
            break
        owner = _acquire_tick_lease(job.state)
        if owner is None:
            stopped = "tick_busy"
            break
        try:
            outcome = fetching.fetch_one(
                db, client, job.settings, at, state=job.state, owner=owner, classify=False, slugs=scope,
            )
        finally:
            job.state.release_tick_lease(owner)
        result = outcome.get("fetch")
        if result in _FETCH_STOPS:
            stopped = result
            break
        row = {"doc_id": item["id"], "name": item.get("name"), "outcome": result}
        if "error" in outcome:
            row["error"] = outcome["error"]
        fetched.append(row)
        job.report(done=len(fetched), total=max_profiles, note=f"{item['id']}: {result}")

    stored = [row["doc_id"] for row in fetched if row["outcome"] in ("stored", "already_stored")]
    return {
        **listed,
        "fetch_enqueued": enqueued,
        "fetched": fetched,
        "stored_slugs": stored,
        "stopped": stopped,
        "fetch_queue": fetch_queue.counts(db),
        "connections": rows,
    }


# =============================================================================
# classify_contacts
# =============================================================================

_PROFILE_FIELDS = ["summary", "fullName", "created_at"]


def classify_contacts(job) -> dict:
    """`new-contacts.ipynb` Phase E: classify stored profiles that have no
    classification yet -- industry, function and seniority -- with the
    notebook's Gemini classifier (`profiles.classify_profile`).

    The profiles are `doc_ids` when given, else every profile in
    `extracted` -- or with `days`, those stored in the last `days` days. A
    profile stored before the service stamped `created_at` falls outside
    any window; leave `days` out, or name it in `doc_ids`. One already classified is left alone, as is one whose
    summary is too short to judge (`profiles.SUMMARY_MIN_LEN`). At most
    `max` go to Gemini, newest first. Each result is merged into `analysis`
    in the transaction that never overwrites a classification that appeared
    meanwhile (`fetching._merge_classification`). A dry run lists who would
    be classified and calls nothing.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    import profiles

    params = job.params
    days = int(params.get("days", 0))
    doc_ids = params.get("doc_ids")
    max_count = int(params.get("max", 25))
    dry_run = bool(params.get("dry_run", True))
    db, settings = job.db, job.settings
    extracted = db.collection(EXTRACTED_COLLECTION)
    analysis = db.collection(ANALYSIS_COLLECTION)

    missing: list[str] = []
    if doc_ids:
        found = db.get_all([extracted.document(doc_id) for doc_id in doc_ids], field_paths=_PROFILE_FIELDS)
        profiles_found = {snapshot.id: snapshot.to_dict() or {} for snapshot in found if snapshot.exists}
        missing = sorted(set(doc_ids) - profiles_found.keys())
    else:
        query = extracted
        if days:
            query = extracted.where(filter=FieldFilter("created_at", ">=", job.now - timedelta(days=days)))
        profiles_found = {snapshot.id: snapshot.to_dict() or {} for snapshot in query.select(_PROFILE_FIELDS).stream()}

    # `get_all` in pages, as `pipeline.load_contacts` reads `analysis`: one
    # request per 250 ids, never one for the whole backlog.
    classified: set[str] = set()
    for chunk in itertools.batched(sorted(profiles_found), CLASSIFIED_PAGE):
        references = [analysis.document(doc_id) for doc_id in chunk]
        for snapshot in db.get_all(references, field_paths=list(fetching.CATEGORY_FIELDS)):
            if snapshot.exists and fetching._has_classification(snapshot.to_dict()):
                classified.add(snapshot.id)

    todo, too_short = [], 0
    for doc_id, fields in profiles_found.items():
        if doc_id in classified:
            continue
        if len((fields.get("summary") or "").strip()) <= profiles.SUMMARY_MIN_LEN:
            too_short += 1
            continue
        todo.append((doc_id, fields))
    todo.sort(key=lambda entry: (_iso(entry[1].get("created_at")) or "", entry[0]), reverse=True)

    counts = {
        "unclassified": len(todo), "already_classified": len(classified), "too_short": too_short, "missing": missing,
    }
    if dry_run:
        rows = [{"doc_id": doc_id, "name": fields.get("fullName")} for doc_id, fields in todo[:MAX_ROWS]]
        return {"dry_run": True, **counts, "would_classify": min(max_count, len(todo)), "contacts": rows}

    gemini = clients.gemini_client()
    rows: list[dict] = []
    done = failed = 0
    batch = todo[:max_count]
    for doc_id, fields in batch:
        document = extracted.document(doc_id).get().to_dict() or {}
        summary = (document.get("summary") or "").replace("\n", " ")
        result = profiles.classify_profile(gemini, summary)
        if result is None:
            failed += 1
            rows.append({"doc_id": doc_id, "name": fields.get("fullName"), "outcome": "failed"})
        else:
            wrote = fetching._merge_classification(db, doc_id, profiles.analysis_body(document, result))
            done += int(wrote)
            rows.append({
                "doc_id": doc_id,
                "name": fields.get("fullName"),
                "outcome": "classified" if wrote else "already_classified",
                "industry": result.industry,
                "function": result.function,
                "seniority": result.seniority,
                "target": result.industry in settings.target_industries,
            })
        job.report(done=len(rows), total=len(batch), note=doc_id)
    return {**counts, "classified": done, "failed": failed, "contacts": rows[:MAX_ROWS]}


# =============================================================================
# classify_stages
# =============================================================================


def classify_stages(job) -> dict:
    """`pipeline-classify.py`: the sales-pipeline stage of every contact whose
    newest inbound message is not classified yet -- or of `doc_ids` only,
    `force` classifying them again whatever is stored -- at most `limit`,
    newest conversations first, through `pipeline.run_pipeline`. A new
    `lead` raises one alert, as `sync_messages` does. A dry run plans
    with `pipeline.plan_pipeline` alone: it lists who would be classified
    and calls no Gemini."""
    import pipeline

    params = job.params
    doc_ids = params.get("doc_ids") or None
    limit = int(params.get("limit", MAX_CLASSIFY))
    force = bool(params.get("force", False))
    dry_run = bool(params.get("dry_run", True))
    db = job.db

    if dry_run:
        documents = pipeline.load_messages(db.collection(pipeline.MESSAGES_COLLECTION), doc_ids)
        transcripts = pipeline.build_transcripts(documents)
        stored = pipeline.load_contacts(db, db.collection(pipeline.ANALYSIS_COLLECTION), transcripts)
        forced = frozenset(doc_ids) if (doc_ids and force) else frozenset()
        silent, planned, tally = pipeline.plan_pipeline(transcripts, stored, force=forced)
        rows = [
            {
                "doc_id": contact,
                "stage": (stored.get(contact) or {}).get("pipeline_stage"),
                "last_inbound": _iso(transcripts[contact]["newest_inbound_date"]),
            }
            for contact in planned[:limit][:MAX_ROWS]
        ]
        return {
            "dry_run": True, "would_classify": len(planned[:limit]), "would_mark_silent": len(silent),
            "tally": tally, "contacts": rows,
        }

    run = asyncio.run(pipeline.run_pipeline(db, clients.gemini_client(), contacts=doc_ids, force=force, limit=limit))
    new_leads = jobs._alert_new_leads(db, run.rows, job.state.now())
    rows = [
        {"doc_id": row["doc_id"], "previous_stage": row["previous_stage"], "stage": row["stage"], "reason": row["reason"]}
        for row in run.rows
    ]
    return {"classified": len(run.rows), "failed": run.failed, "new_leads": new_leads, "tally": run.tally,
            "contacts": rows[:MAX_ROWS]}


# =============================================================================
# send_intro
# =============================================================================


def send_intro(job) -> dict:
    """`send-intros.ipynb` Phases B-E, QUEUED: pick who gets the intro with
    `functions.select_intro_candidates` and queue `templates/intro.md` for
    each (`jobs.plan_intros`), due at once (`OUTREACH_INTRO_GAP_*_MINUTES`,
    0 by default). `send_messages` sends them, one at a time, running every
    guard again first -- nothing goes out until it runs, nor while sends
    are paused or writes are blocked.

    Holds the `send_intro` lock for the whole job (`monitor.start` took it),
    which the daily job also takes: the per-day cap is counted once. The
    result says whether sends would go out now (`sender`): whether sends
    are paused or blocked, and whether each waits for approval.
    """
    params = job.params
    dry_run = bool(params.get("dry_run", True))
    db, settings = job.db, job.settings
    text = jobs._intro_text(settings)
    client = job.client()
    planned = jobs.plan_intros(
        db, settings, job.now,
        relations=list(client.users.iter_relations()),
        chat_ids=jobs.open_chat_ids(client),
        text=text,
        days=int(params.get("days", 0)),
        industries=params.get("industries"),
        seniorities=params.get("seniority"),
        max_count=params.get("max"),
        doc_ids=params.get("doc_ids"),
        dry_run=dry_run,
        state=job.state,
        rng=random.Random(),
        created_by="tool",
        tags=params.get("tags"),
    )
    fields = job.state.read()
    result = {
        "candidates": planned["candidates"],
        "open_items": planned["open_items"],
        "skipped": planned["skipped"],
        "not_new": planned["not_new"],
        "not_eligible": planned["not_eligible"],
        "cap": planned["cap"],
        "intros": planned["rows"][:MAX_ROWS],
        "sender": {
            "sends_paused_until": _iso(fields.get("sends_paused_until")),
            "writes_blocked": job.state.writes_blocked(),
            "require_approval": job.state.require_approval(settings.require_approval),
        },
    }
    if dry_run:
        return {"dry_run": True, "would_queue": len(planned["created"]), **result}
    return {"queued": len(planned["created"]), "send_with": "send_messages", **result}


# =============================================================================
# send_messages
# =============================================================================

#: Messages per minute `send_messages` may be set to, and its default.
MIN_FREQUENCY, MAX_FREQUENCY, DEFAULT_FREQUENCY = 0.1, 2.0, 1.0

#: Messages one `send_messages` call may send, and its default. 200 is the
#: day's message cap as configured (`UNIPILE_MAX_MESSAGES_PER_DAY`).
MAX_SEND_LIMIT, DEFAULT_SEND_LIMIT = 200, 50

#: Seconds a job keeps in hand beyond one wait before its dispatch deadline
#: (`monitor.DISPATCH_DEADLINE_SECONDS`): enough for one send and for
#: starting the next job.
HANDOVER_MARGIN_SECONDS = 150

#: The longest a wait between two sends goes without a heartbeat, far under
#: `monitor.LOST_AFTER`.
HEARTBEAT_SECONDS = 60

#: The clock the time limit is measured with; replaced in tests.
_monotonic = time.monotonic


def send_messages(job) -> dict:
    """Send every approved message already due in the queue, one at a time,
    `frequency` a minute, at most `limit` -- intros, follow-ups and replies
    alike, in due order.

    Each message is one pass of the tick's own send code
    (`jobs._tick_holding_lease`, without its profile fetch) holding the tick
    lease for that message only, so every guard runs again right before it
    goes: the text, the contact's touches and replies, pauses, blocked
    writes, the day's message budget, the chat. Messages due later wait for
    a later call.

    Stops when `limit` is reached (`limit`), nothing due is left (`idle`),
    the account stops sends (`writes_blocked`, `sends_paused`, `budget`), or
    a send does not come back `sent` (its outcome: `released`, `failed`,
    `unknown`, with `error`). A job has 30 minutes (the dispatch deadline);
    when the next wait would not leave `HANDOVER_MARGIN_SECONDS` of them, it
    starts the next `send_messages` job with what is left of `limit` and
    stops as `handed_over`, naming it `next_job_id` -- or, when that job
    cannot be started, as `handover_failed` with `next_job_error`.

    A dry run waits for nothing and writes nothing: `jobs.preview_sends`
    lists who would be sent (`would_send`) and who skipped, and why.
    """
    params = job.params
    frequency = float(params.get("frequency", DEFAULT_FREQUENCY))
    limit = int(params.get("limit", DEFAULT_SEND_LIMIT))
    dry_run = bool(params.get("dry_run", True))
    db, settings, client = job.db, job.settings, job.client()
    settings_used = {"frequency": frequency, "limit": limit}

    if dry_run:
        preview = jobs.preview_sends(db, client, settings, job.state.now(), state=job.state, limit=limit)
        preview["would_send"] = preview["would_send"][:MAX_ROWS]
        return {"dry_run": True, **settings_used, **preview}

    gap = 60.0 / frequency
    started = _monotonic()
    sent: list[dict] = []
    skipped = 0
    stopped = None
    extra: dict = {}
    wait = 0.0
    while len(sent) < limit:
        if queue.next_due(db, job.state.now()) is None:
            stopped = "idle"
            break
        if _monotonic() - started + wait + HANDOVER_MARGIN_SECONDS > monitor.DISPATCH_DEADLINE_SECONDS:
            extra = _hand_over(job, frequency, limit - len(sent))
            stopped = "handed_over" if "next_job_id" in extra else "handover_failed"
            break
        _wait(job, wait, done=len(sent), total=limit)
        owner = _acquire_tick_lease(job.state)
        if owner is None:
            stopped = "tick_busy"
            break
        try:
            outcome = jobs._tick_holding_lease(
                db, client, settings, job.state.now(), state=job.state, owner=owner, fetch=False,
            )
        finally:
            job.state.release_tick_lease(owner)
        skipped += outcome.get("items_skipped", 0)
        if outcome.get("outcome") == "sent":
            sent.append(_sent_row(db, outcome))
            job.report(done=len(sent), total=limit, note=outcome["item"])
            wait = gap
            continue
        if outcome.get("stopped") == "max_attempts":
            # Ten due items refused in a row, each now marked skipped: look again at once.
            wait = 0.0
            continue
        stopped, extra = _send_stop(outcome)
        break
    else:
        stopped = "limit"

    return {
        **settings_used,
        "sent": len(sent),
        "skipped": skipped,
        "stopped": stopped,
        **extra,
        "messages": sent[:MAX_ROWS],
        "queue": queue.counts(db),
    }


def _wait(job, seconds: float, *, done: int, total: int) -> None:
    """Sleep `seconds`, heartbeating at least every `HEARTBEAT_SECONDS`."""
    while seconds > 0:
        chunk = min(seconds, HEARTBEAT_SECONDS)
        _sleep(chunk)
        seconds -= chunk
        job.report(done=done, total=total, note="waiting before the next message")


def _sent_row(db, outcome: dict) -> dict:
    item = queue.get(db, outcome["item"]) or {}
    return {
        "queue_id": outcome["item"],
        "doc_id": item.get("contact_doc_id"),
        "name": item.get("name"),
        "kind": outcome.get("kind"),
        "message_id": outcome.get("message_id"),
        "sent_at": _iso(item.get("sent_at")),
    }


def _send_stop(outcome: dict) -> tuple[str, dict]:
    """Why a send pass that sent nothing stops `send_messages`, and what to
    report with it."""
    if "outcome" in outcome:
        details = {key: outcome[key] for key in ("item", "error", "paused_until", "writes_blocked") if key in outcome}
        return outcome["outcome"], details
    if outcome.get("idle"):
        return "idle", {}
    if "skipped" in outcome:
        return outcome["skipped"], {"until": outcome["until"]} if "until" in outcome else {}
    return outcome.get("stopped") or "unexpected", {}


def _hand_over(job, frequency: float, remaining: int) -> dict:
    """Start the next `send_messages` job with `remaining` of the limit,
    taking over this job's lock (`monitor.start`'s `successor_of`)."""
    launched = monitor.launch(
        job.db, job.step, {"frequency": frequency, "limit": remaining, "dry_run": False}, job.settings,
        job.state.now(), created_by=job.id, successor_of=job.id,
    )
    if launched["ok"]:
        return {"next_job_id": launched["job_id"]}
    return {"next_job_error": launched.get("error") or launched.get("reason")}


#: Every step a job can run, by the name its tool and its job id carry.
STEPS = {
    "sync_messages": sync_messages,
    "get_contacts": get_contacts,
    "classify_contacts": classify_contacts,
    "classify_stages": classify_stages,
    jobs.INTRO_STEP: send_intro,
    "send_messages": send_messages,
}
