"""The job monitor: how a process step the MCP tools start runs, and how
anyone asks how it is going.

A process step (`steps.STEPS`: get contacts, classify them, send intros, ...)
takes seconds to minutes -- a dry `send_intro` alone reads every chat and
every connection, about a minute in production -- so a tool never runs one
inside its own call. It starts a JOB and answers at once with the job's id;
`get_job` reports on it. One job is one `runs/{step}:{started_at UTC}`
document, the same collection and id scheme the scheduled jobs write, so
`get_run_report` lists both:

- `start` takes the step's lock and writes the job `queued`, in one
  transaction. One live job per step: a second start is refused with the
  live job's id. A lock whose job has gone quiet -- `running` with no
  heartbeat for `LOST_AFTER`, or `queued` that long -- is treated as free,
  and that job is marked `failed` (`lost`) right there, so a job that died
  never blocks its step even while no scheduled job runs to sweep it.
- `run` is the worker. It claims the job (`queued` -> `running`, in a
  transaction, so a job runs once however often it is delivered), runs the
  step with a `Job` that reports progress and heartbeats, and records
  `succeeded` with the step's result or `failed` with the error's class
  name -- raising the same `job_failed` alert the scheduled jobs raise --
  then releases the lock.
- `get` and `wait` read a job; `wait` polls until it finishes or the wait
  runs out, so a caller spends one call per 45 seconds, not one per poll.

The executor (`settings.job_executor`) decides where `run` happens:
`inline` calls it in the same thread (tests, local runs); `cloud_tasks`
creates an HTTP task that calls `POST /jobs/run/{job_id}` on this service
(`app.py`), so the job runs inside a request of its own -- with its CPU
allocated for the whole job, which a background thread on Cloud Run would
not get -- and its state lives in Firestore, which every instance shares.
The queue retries nothing (`max_attempts=1`, set on the queue) and the task
name is derived from the job id, so a job is delivered at most once.

Locks live in their own document, `runtime_state/locks`, one field per step,
so a slow lock transaction never contends with the tick lease in
`runtime_state/linkedin`. The scheduled `daily` job takes the `send_intro`
lock for its intro step (`acquire_lock`), which is what keeps the per-day
intro cap exact when a person starts `send_intro` at the same moment.
"""

import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from linkedinmcp import clients, clock, state as runtime_state

logger = logging.getLogger(__name__)

RUNS_COLLECTION = "runs"
LOCKS_DOCUMENT = "locks"

QUEUED, RUNNING, SUCCEEDED, FAILED = "queued", "running", "succeeded", "failed"
LIVE = (QUEUED, RUNNING)

#: A live job silent this long is lost: its worker died, or its task was
#: never delivered. A step heartbeats as it goes -- `get_contacts` and
#: `classify_contacts` after each profile, `sync_messages` between its
#: phases -- and `classify_stages` and `send_intro` are one phase of a few
#: minutes each, all far shorter than this. A worker that was only slow
#: learns it was taken for lost at its next heartbeat, and stops there
#: (`JobLost`).
LOST_AFTER = timedelta(minutes=10)
LOST_ERROR = "lost"


class JobLost(Exception):
    """The job is no longer `running`: silent for `LOST_AFTER`, it was
    marked lost when a newer job took its step. Raised by `Job.report`, so
    the step stops at its next heartbeat instead of running beside the
    newer job; its record keeps saying lost (`finish`)."""

#: The longest `wait` may block, and how often it looks. Under a minute, so
#: a caller whose own request times out at 60 s still gets its answer.
MAX_WAIT_SECONDS = 45
POLL_SECONDS = 2.0

#: A Cloud Task's dispatch deadline -- the longest the queue waits for the
#: worker's answer, its own ceiling -- matching `--timeout=1800` in
#: `deploy.cmd`. Every step is capped to finish well inside it.
DISPATCH_DEADLINE_SECONDS = 1800

#: The time source `wait` measures its deadline with, and how it sleeps:
#: module attributes, read at call time, so a test can replace them.
_monotonic = time.monotonic
_sleep = time.sleep


def job_id(step: str, at: datetime) -> str:
    """`{step}:{at in UTC, %Y%m%dT%H%M%S%fZ}` -- the scheduled runs' own id
    scheme (`jobs.record_run`), so `get_run_report(job=step)` finds both."""
    return f"{step}:{at.astimezone(UTC):%Y%m%dT%H%M%S%fZ}"


def step_of(job: str) -> str:
    """The step a job id names: everything before its first `:`."""
    return job.split(":", 1)[0]


def _locks_ref(db):
    return db.collection(runtime_state.STATE_COLLECTION).document(LOCKS_DOCUMENT)


def _is_quiet(run: dict | None, since: datetime | None, now: datetime) -> bool:
    """Whether a lock's holder no longer counts: its job has finished, or has
    been silent for `LOST_AFTER`. A holder with no job document -- the
    scheduled `daily`, which writes its run only when it ends -- counts for
    `LOST_AFTER` after it took the lock."""
    if run is None:
        return since is None or now - since > LOST_AFTER
    if run.get("status") not in LIVE:
        return True
    stamp = run.get("heartbeat_at") or run.get("queued_at")
    return stamp is None or now - stamp > LOST_AFTER


def _take_lock(
    transaction, db, step: str, holder: str, now: datetime, successor_of: str | None = None
) -> dict | None:
    """Inside `transaction`: take `step`'s lock for `holder`, or return the
    live holder that keeps it (`{"job_id", "status"}`). A quiet holder's
    live job is marked failed/lost in the same transaction. A holder named
    as `successor_of` hands the lock over, live or not: it is the job
    starting `holder`, and is still running while it does."""
    locks_ref = _locks_ref(db)
    runs = db.collection(RUNS_COLLECTION)
    held = (locks_ref.get(transaction=transaction).to_dict() or {}).get(step) or {}
    current = held.get("job_id")
    if current and current != successor_of:
        current_ref = runs.document(current)
        snapshot = current_ref.get(transaction=transaction)
        run = (snapshot.to_dict() or {}) if snapshot.exists else None
        if not _is_quiet(run, held.get("since"), now):
            return {"job_id": current, "status": (run or {}).get("status", RUNNING)}
        if run is not None and run.get("status") in LIVE:
            transaction.set(
                current_ref,
                {"status": FAILED, "ok": False, "error": LOST_ERROR, "finished_at": now},
                merge=True,
            )
    transaction.set(locks_ref, {step: {"job_id": holder, "since": now}}, merge=True)
    return None


def acquire_lock(db, step: str, holder: str, now: datetime) -> dict | None:
    """Take `step`'s lock for `holder` -- a caller that is not a monitor job,
    the scheduled `daily` -- and return `None`; or return the live job that
    holds it, taking nothing."""
    from google.cloud import firestore

    @firestore.transactional
    def _acquire(transaction):
        return _take_lock(transaction, db, step, holder, now)

    return _acquire(db.transaction())


def release_lock(db, step: str, holder: str) -> bool:
    """Free `step`'s lock, only when `holder` still holds it."""
    from google.cloud import firestore

    locks_ref = _locks_ref(db)

    @firestore.transactional
    def _release(transaction):
        held = (locks_ref.get(transaction=transaction).to_dict() or {}).get(step) or {}
        if held.get("job_id") != holder:
            return False
        transaction.set(locks_ref, {step: None}, merge=True)
        return True

    return _release(db.transaction())


def start(
    db, step: str, params: dict, now: datetime, *, created_by: str = "tool", successor_of: str | None = None
) -> dict:
    """Take `step`'s lock and write a `queued` job, in one transaction.

    Returns `{"ok": True, "job_id", "status": "queued"}`, or `{"ok": False,
    "reason": "already_running", "job_id", "status"}` naming the live job
    that holds the step, having written nothing.

    `successor_of` is the running job of the same step that starts this one
    to carry on its work (`steps.send_messages`): the lock passes from it to
    the new job, and its own `finish` then leaves the lock alone.
    """
    from google.cloud import firestore

    new_id = job_id(step, now)
    if new_id == successor_of:
        new_id = job_id(step, now + timedelta(microseconds=1))
    reference = db.collection(RUNS_COLLECTION).document(new_id)

    @firestore.transactional
    def _start(transaction):
        holder = _take_lock(transaction, db, step, new_id, now, successor_of)
        if holder is not None:
            return {"ok": False, "reason": "already_running", **holder}
        transaction.set(
            reference,
            {
                "job": step,
                "status": QUEUED,
                "params": params,
                "created_by": created_by,
                "started_at": now,
                "queued_at": now,
                "heartbeat_at": now,
                "claimed_at": None,
                "finished_at": None,
                "progress": None,
                "result": None,
                "summary": None,
                "ok": None,
                "error": None,
            },
        )
        return {"ok": True, "job_id": new_id, "status": QUEUED}

    return _start(db.transaction())


def claim(db, job: str, now: datetime) -> dict | None:
    """`queued` -> `running`, in a transaction. The job's fields, or `None`
    when there is no such job or it is not `queued` -- already claimed by
    an earlier delivery, finished, or marked lost."""
    from google.cloud import firestore

    reference = db.collection(RUNS_COLLECTION).document(job)

    @firestore.transactional
    def _claim(transaction):
        snapshot = reference.get(transaction=transaction)
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        if data.get("status") != QUEUED:
            return None
        transaction.set(reference, {"status": RUNNING, "claimed_at": now, "heartbeat_at": now}, merge=True)
        return data

    return _claim(db.transaction())


def _summary_of(result: dict | None) -> dict | None:
    """The scalar entries of a result -- the flat dict of counts
    `get_run_report` shows as a run's `summary`."""
    if result is None:
        return None
    return {key: value for key, value in result.items() if value is None or isinstance(value, (bool, int, float, str))}


def finish(db, job: str, now: datetime, *, result: dict | None = None, error: str | None = None) -> bool:
    """Record the job's outcome -- `succeeded` with `result`, or `failed`
    with `error`, an exception's class name -- and release its step's lock.

    Only while the job is live, checked in the same transaction: a job
    already marked lost keeps that record, and this returns `False` having
    written nothing -- the lock is the newer job's by then, and
    `release_lock` leaves it alone.
    """
    from google.cloud import firestore

    ok = error is None
    reference = db.collection(RUNS_COLLECTION).document(job)

    @firestore.transactional
    def _finish(transaction):
        if (reference.get(transaction=transaction).to_dict() or {}).get("status") not in LIVE:
            return False
        transaction.set(
            reference,
            {
                "status": SUCCEEDED if ok else FAILED,
                "ok": ok,
                "result": result,
                "summary": _summary_of(result),
                "error": error,
                "finished_at": now,
                "heartbeat_at": now,
            },
            merge=True,
        )
        return True

    finished = _finish(db.transaction())
    release_lock(db, step_of(job), job)
    return finished


def get(db, job: str) -> dict | None:
    """A job's fields plus `id`, `status` and `lost`, or `None`.

    A scheduled run carries no `status`; it reads as `succeeded` or `failed`
    from its `ok`, with its `summary` as its `result`. `lost` is true for a
    live job silent for `LOST_AFTER` -- reported, not written: the next
    `start` of that step marks it failed.
    """
    snapshot = db.collection(RUNS_COLLECTION).document(job).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    status = data.get("status")
    if status is None:
        status = SUCCEEDED if data.get("ok") else FAILED
        data = {**data, "result": data.get("summary")}
    lost = status in LIVE and _is_quiet(data, None, clock.utcnow())
    return {**data, "id": job, "status": status, "lost": lost}


def wait(db, job: str, seconds: float) -> dict | None:
    """`get(db, job)`, polled every `POLL_SECONDS` until the job is no
    longer live or `seconds` (at most `MAX_WAIT_SECONDS`) have passed."""
    deadline = _monotonic() + max(0.0, min(float(seconds), MAX_WAIT_SECONDS))
    while True:
        found = get(db, job)
        remaining = deadline - _monotonic()
        if found is None or found["status"] not in LIVE or found["lost"] or remaining <= 0:
            return found
        _sleep(min(POLL_SECONDS, remaining))


@dataclass
class Job:
    """What a step function is handed: its parameters, the clients and
    settings, and `report` for progress and the heartbeat.

    `now` is the moment the job was claimed. A step that runs for minutes
    reads the time again through `state.now()` for anything it stamps.
    `client()` builds the LinkedIn client on first use, and only then --
    the classification steps never talk to LinkedIn.
    """

    id: str
    step: str
    params: dict
    db: Any
    settings: Any
    state: Any
    now: datetime
    _client: Any = field(default=None, repr=False)

    def client(self):
        if self._client is None:
            self._client = clients.unipile_client()
        return self._client

    @property
    def used_linkedin(self) -> bool:
        return self._client is not None

    def report(self, *, done: int | None = None, total: int | None = None, note: str | None = None) -> None:
        """Store progress and beat the heart: call it after every unit.

        Raises `JobLost`, writing nothing, once the job is no longer
        `running` -- checked in the same transaction as the write.
        """
        from google.cloud import firestore

        reference = self.db.collection(RUNS_COLLECTION).document(self.id)
        changes = {"heartbeat_at": self.state.now(), "progress": {"done": done, "total": total, "note": note}}

        @firestore.transactional
        def _report(transaction):
            if (reference.get(transaction=transaction).to_dict() or {}).get("status") != RUNNING:
                return False
            transaction.set(reference, changes, merge=True)
            return True

        if not _report(self.db.transaction()):
            raise JobLost(self.id)

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()


def run(job: str, settings, *, now_fn: Callable[[], datetime] | None = None) -> dict:
    """The worker: claim `job`, run its step, record the outcome.

    Returns `{"job_id", "ran": False, "status"}` when there was nothing to
    run (not queued: delivered twice, finished, lost) and `{"job_id", "ran":
    True, "status", "error"?}` otherwise. A step that raises is recorded
    `failed` and raises the `job_failed` alert, never re-raised: the outcome
    is in the job, and the caller -- a Cloud Task -- must not retry it. A
    step that met LinkedIn restricting the account blocks writes and raises
    the `restricted` alert, as a scheduled job does (`jobs._watch_restriction`).
    """
    from lib.unipile import errors as unipile_errors
    from linkedinmcp import jobs, steps

    # Read at call time, not bound as a default, so a test replacing
    # `clock.utcnow` moves this job's clock too.
    now_fn = now_fn or clock.utcnow
    db = clients.firestore_client()
    claimed = claim(db, job, now_fn())
    if claimed is None:
        found = get(db, job)
        return {"job_id": job, "ran": False, "status": found["status"] if found else None}

    step = claimed.get("job") or step_of(job)
    runtime = runtime_state.RuntimeState(db, now_fn)
    context = Job(
        id=job, step=step, params=claimed.get("params") or {}, db=db, settings=settings, state=runtime, now=now_fn()
    )
    try:
        function = steps.STEPS.get(step)
        if function is None:
            raise ValueError(f"unknown step: {step!r}")
        result = function(context)
        if context.used_linkedin and context.client().writes_blocked:
            jobs._note_job_restriction(db, runtime, now_fn(), job=step, error=None)
    except Exception as error:
        name = type(error).__name__
        logger.exception("job %s failed", job)
        if isinstance(error, unipile_errors.AccountRestricted) or (
            context.used_linkedin and context.client().writes_blocked
        ):
            jobs._note_job_restriction(db, runtime, now_fn(), job=step, error=name)
        finish(db, job, now_fn(), error=name)
        jobs._alert_job_failed(db, settings, now_fn(), job=step, error=error, run_id=job)
        return {"job_id": job, "ran": True, "status": FAILED, "error": name}
    finally:
        context.close()
    if not finish(db, job, now_fn(), result=result):
        logger.warning("job %s ended after it was taken for lost; its record and result stand as lost", job)
        return {"job_id": job, "ran": True, "status": FAILED, "error": LOST_ERROR}
    return {"job_id": job, "ran": True, "status": SUCCEEDED}


# =============================================================================
# Executors
# =============================================================================


def task_name(job: str) -> str:
    """A Cloud Tasks task id for `job`: its characters outside
    `[A-Za-z0-9_-]` -- the only ones a task id may hold -- replaced by `-`.
    Deterministic, so the queue refuses a second task for the same job."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", job)


def submit(job: str, settings) -> None:
    """Hand `job` to the configured executor."""
    if settings.job_executor == "inline":
        run(job, settings)
        return
    _submit_cloud_task(job, settings)


def _submit_cloud_task(job: str, settings) -> None:
    """One HTTP task on `settings.jobs_queue` that POSTs `/jobs/run/{job}`
    back to this service with its key, the official client doing the
    call. The queue itself carries `max_attempts=1`."""
    from google.cloud import tasks_v2
    from google.protobuf import duration_pb2

    from lib import firestore as lib_firestore
    from lib.config import ConfigError

    if not settings.service_url:
        raise ConfigError("OUTREACH_SERVICE_URL is not set, so a Cloud Task has nowhere to call back.")
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(lib_firestore.PROJECT, settings.jobs_location, settings.jobs_queue)
    url = f"{settings.service_url.rstrip('/')}/jobs/run/{urllib.parse.quote(job, safe='')}"
    task = tasks_v2.Task(
        name=f"{parent}/tasks/{task_name(job)}",
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=url,
            headers={"x-api-key": settings.api_key.get_secret_value()},
        ),
        dispatch_deadline=duration_pb2.Duration(seconds=DISPATCH_DEADLINE_SECONDS),
    )
    client.create_task(parent=parent, task=task)


def launch(
    db, step: str, params: dict, settings, now: datetime, *, created_by: str = "tool", successor_of: str | None = None
) -> dict:
    """`start` the job, then `submit` it. Returns `{"ok": True, "job_id",
    "status"}` -- `queued` from Cloud Tasks, already finished from the
    inline executor -- or `start`'s refusal, or `{"ok": False, "reason":
    "not_started", "job_id", "error"}` when the executor refused it (the job
    is then recorded failed and its lock freed)."""
    started = start(db, step, params, now, created_by=created_by, successor_of=successor_of)
    if not started["ok"]:
        return started
    try:
        submit(started["job_id"], settings)
    except Exception as error:
        logger.exception("could not start job %s", started["job_id"])
        finish(db, started["job_id"], clock.utcnow(), error=type(error).__name__)
        return {"ok": False, "reason": "not_started", "job_id": started["job_id"], "error": type(error).__name__}
    found = get(db, started["job_id"])
    return {"ok": True, "job_id": started["job_id"], "status": found["status"] if found else QUEUED}
