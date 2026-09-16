"""The Home screen's two buttons, run on the outreach service over MCP.

**Get New Contacts** starts `get_contacts` (up to `MAX_PROFILES` profile
views), then `classify_contacts` on exactly the profiles it stored.
**Sync Messages** starts `sync_messages`, which also stages new replies.
Every call passes `dry_run=False`: each step tool defaults to a dry run.

One run at a time, held in memory (`Routine` on `app.state.routine`): the
service runs a single instance, and a restart forgets the run -- its jobs go
on finishing on the outreach service, but a second step not yet started is
never started. The run advances only when the Home screen is loaded
(`advance`), which reloads itself every few seconds while a run is going:
Cloud Run gives this container CPU only while it answers a request, so a
background task would stall between requests.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from webapp import render

logger = logging.getLogger(__name__)

router = APIRouter()

#: Profiles one Get New Contacts press views: the most `get_contacts` takes.
MAX_PROFILES = 10

#: Button path: its label, and the tool and arguments of its first step.
ACTIONS = {
    "new-contacts": ("Get New Contacts", "get_contacts", {"max_profiles": MAX_PROFILES, "dry_run": False}),
    "sync-messages": ("Sync Messages", "sync_messages", {"classify": True, "dry_run": False}),
}

#: How the panel names each step.
STEP_LABELS = {
    "get_contacts": "Load new contacts",
    "classify_contacts": "Classify them",
    "sync_messages": "Sync messages and stage replies",
}

#: Plain names for the counts the steps return; any other key is shown with
#: its underscores as spaces.
LABELS = {
    "new_connections": "new connections",
    "unusable_slugs": "connections without a usable profile id",
    "fetch_enqueued": "added to the fetch queue",
    "stored_slugs": "profiles stored",
    "stopped": "stopped because",
    "too_short": "profiles too short to classify",
    "missing": "not found",
    "stale_swept": "stuck sends released",
    "written": "messages stored",
    "stats_refreshed": "contact stats refreshed",
    "replied_contacts": "contacts who replied",
    "cancelled": "queued messages cancelled",
    "unknown_sent": "unclear sends found sent",
    "unknown_failed": "unclear sends marked failed",
    "classified": "classified",
    "new_leads": "new leads",
}

#: Result lists the panel leaves out: `get_contacts` lists every new
#: connection, fetched or not.
HIDDEN = {"connections"}


async def call_tool(url: str, api_key: str, name: str, arguments: dict) -> dict:
    """One outreach tool call, in its own MCP session."""
    transport = StreamableHttpTransport(url, headers={"x-api-key": api_key})
    async with Client(transport, timeout=60) as client:
        return (await client.call_tool(name, arguments)).data


@dataclass
class Run:
    """One button press: its steps as `{tool, job_id, status, progress,
    result}`, newest last."""

    action: str
    label: str
    started_at: datetime
    steps: list[dict] = field(default_factory=list)
    finished: bool = False
    error: str | None = None
    note: str | None = None
    problem: str | None = None


@dataclass
class Routine:
    """The latest run, and the lock every change to it takes."""

    run: Run | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def running(self) -> bool:
        return self.run is not None and not self.run.finished


async def _call(app, name: str, arguments: dict) -> dict:
    settings, outreach = app.state.settings, app.state.outreach
    return await call_tool(settings.outreach_url, outreach.api_key.get_secret_value(), name, arguments)


async def _start(app, run: Run, tool: str, arguments: dict) -> None:
    """Start one step. A job already running for that step is followed as
    if this press had started it."""
    try:
        started = await _call(app, tool, arguments)
    except Exception as error:
        logger.exception("%s could not be started", tool)
        run.error, run.finished = f"{STEP_LABELS[tool]} could not be started: {type(error).__name__}: {error}", True
        return
    run.steps.append({"tool": tool, "job_id": started.get("job_id"), "status": started.get("status"), "progress": None, "result": None})
    if started.get("ok") or (started.get("reason") == "already_running" and started.get("job_id")):
        logger.info("%s: following job %s", tool, started.get("job_id"))
        return
    detail = started.get("detail") or started.get("error") or ""
    run.error, run.finished = f"{STEP_LABELS[tool]} was not started: {started.get('reason')} {detail}".strip(), True


async def _next(app, run: Run, finished_step: dict) -> None:
    """After a step succeeded: start the chain's next step, or finish."""
    if finished_step["tool"] != "get_contacts":
        run.finished = True
        return
    stored = (finished_step["result"] or {}).get("stored_slugs") or []
    if not stored:
        # Never start `classify_contacts` without ids: it would take every
        # stored profile not classified yet, not the ones this press stored.
        run.note, run.finished = "No profile was stored, so there was nothing to classify.", True
        return
    await _start(app, run, "classify_contacts", {"doc_ids": stored, "max": len(stored), "dry_run": False})


async def advance(app) -> None:
    """Read the running step's job once and act on what it says."""
    routine: Routine = app.state.routine
    async with routine.lock:
        if not routine.running:
            return
        run = routine.run
        step = run.steps[-1]
        try:
            job = await _call(app, "get_job", {"job_id": step["job_id"], "wait_seconds": 0})
        except Exception as error:
            logger.warning("reading job %s failed: %r", step["job_id"], error)
            run.problem = f"Could not read the job just now: {type(error).__name__}. Trying again."
            return
        run.problem = None
        if not job.get("ok"):
            run.error, run.finished = f"{STEP_LABELS[step['tool']]}: the outreach service has no job {step['job_id']}.", True
            return
        step.update(status=job.get("status"), progress=job.get("progress"), result=job.get("result"))
        if job.get("lost"):
            run.error, run.finished = f"{STEP_LABELS[step['tool']]} stopped reporting for ten minutes: its worker died.", True
        elif job.get("status") == "failed":
            run.error, run.finished = f"{STEP_LABELS[step['tool']]} failed: {job.get('error')}", True
        elif job.get("status") == "succeeded":
            await _next(app, run, step)


@router.post("/routine/{action}")
async def press(request: Request, action: str) -> RedirectResponse:
    """Start a run, unless one is already going; back to Home either way."""
    if action not in ACTIONS:
        raise HTTPException(status_code=404, detail="No such action.")
    label, tool, arguments = ACTIONS[action]
    routine: Routine = request.app.state.routine
    async with routine.lock:
        if not routine.running:
            routine.run = Run(action=action, label=label, started_at=datetime.now(UTC))
            await _start(request.app, routine.run, tool, arguments)
    return RedirectResponse("/", status_code=303)


def result_summary(result: dict | None) -> tuple[list[tuple[str, str]], list[tuple[str, list[str], list[dict]]]]:
    """A step's result as the panel shows it: `(label, text)` for each count
    or short value, and `(label, columns, rows)` for each list of rows."""
    counts, tables = [], []
    for key, value in (result or {}).items():
        if key in HIDDEN or value is None:
            continue
        label = LABELS.get(key, key.replace("_", " "))
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            tables.append((label, list(dict.fromkeys(column for row in value for column in row)), value))
        elif isinstance(value, list):
            counts.append((label, ", ".join(map(str, value)) or "none"))
        elif isinstance(value, dict):
            counts.append((label, ", ".join(f"{name} {count}" for name, count in value.items())))
        elif isinstance(value, bool):
            counts.append((label, "yes" if value else "no"))
        else:
            counts.append((label, str(value)))
    return counts, tables


render.templates.env.globals["result_summary"] = result_summary
render.templates.env.globals["step_labels"] = STEP_LABELS
