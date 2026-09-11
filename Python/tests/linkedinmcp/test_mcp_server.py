"""Every MCP tool over the real MCP protocol, in memory: `get_status`, the six
read-only tools, the five agent-side write tools and the eight human-side
tools.

Driven through `fastmcp.Client(mcp)` -- the in-memory transport, which speaks
the same JSON-RPC the deployed service does but opens no socket. So these
tests exercise tool registration, the generated schema and the serialised
result rather than a plain Python function call.

The write-tool tests (task 2g, at the bottom) use the `write_db` fixture: the
same environment as `env` but with `require_approval` off in the configuration
-- so a `runtime_state` override is what decides -- and the clock fixed at a
moment whose local date in `TZ` is a day after its UTC date. "Wrote nothing"
is checked by comparing `store_snapshot(db)` before and after.

Nothing here touches Firestore. `clients.firestore_client` is replaced by a
fake in every test that reaches it, which is the whole reason
`linkedinmcp/clients.py` exists and the whole reason `mcp_server` reaches it as
`clients.firestore_client()` through the module instead of binding the name:
`monkeypatch.setattr(clients, "firestore_client", ...)` cannot reach a name
that was imported directly.

Two families of fake stand in for Firestore here. `FakeDb`/`FakeQuery`,
defined in this file, are a MINIMAL hand-rolled double that only ever
supports `.collection().select().limit().stream()` -- just enough to drive
`get_status`'s own health probe and prove it stays cheap; used by every test
that predates task 2e. `tests/linkedinmcp/fake_firestore.FakeFirestore` is
the real in-memory Firestore -- seeded with actual documents -- and is what
every task-2e test (the six new tools, and `get_status`'s new fields) uses,
via the `fake_db` fixture below.

Settings arrive the way they do in production -- `cfg.get_settings()` reads
the environment -- with `monkeypatch.setenv` supplying deliberately unusual
values, so a tool that hard-coded a cap fails here. The fixture also `chdir`s
into a tmp directory, because `get_settings()` calls `from_env(".env")` and
`Python/.env` really does exist for the notebooks; a test must neither read it
nor depend on what is in it.

Async tests run under anyio's pytest plugin. `pytest-asyncio` is not installed
and must not be added.
"""

import inspect
import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from linkedinmcp import clients, clock, decisions, fetch_queue, guards, mcp_server, queue
from tests.linkedinmcp.fake_firestore import FakeFirestore
from tests.linkedinmcp.fake_unipile import FakeUnipile, relation, seed_contact, seed_item, seed_message, store_snapshot

#: Deliberately unlike every default in `OutreachSettings`, so a hard-coded
#: cap in the tool cannot accidentally match.
#: Deliberately odd values so the assertions cannot pass against a default.
#: Rate limits carry the `UNIPILE_` prefix because they belong to the Unipile
#: configuration, which is what `SendBudget` actually enforces on the call --
#: this service does not keep a second copy under a name of its own.
CAPS_ENV = {
    "UNIPILE_MAX_MESSAGES_PER_DAY": "7",
    "UNIPILE_MAX_PROFILE_FETCHES_PER_DAY": "23",
    "OUTREACH_INTRO_DAILY_CAP": "4",
    "OUTREACH_MAX_TOUCHES": "2",
    "OUTREACH_MIN_DAYS_BETWEEN_TOUCHES": "9",
}

#: The Unipile client refuses to start without these, so the status tool cannot
#: read a rate limit without them either.
UNIPILE_CREDENTIALS = {
    "UNIPILE_API_KEY": "unipile-key-not-a-real-secret",
    "UNIPILE_DNS": "api99.unipile.com:19999",
}

#: A zone with a half-hour offset and no DST, so the expected offset is one
#: fixed string all year. `Etc/GMT-5` would have been the trap: its sign is
#: inverted (that zone is UTC+05:00).
TZ = "Asia/Kolkata"
TZ_OFFSET = "+05:30"

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend():
    """Run the async tests on asyncio only. anyio would otherwise also try trio,
    which is not installed."""
    return "asyncio"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The environment `cfg.get_settings()` will read, and nothing else.

    `chdir` first: `get_settings()` -> `from_env(".env")` resolves that path
    against the working directory, and the repository's own `.env` sits there.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OUTREACH_API_KEY", "test-key-not-a-real-secret")
    monkeypatch.setenv("OUTREACH_TZ", TZ)
    monkeypatch.setenv("OUTREACH_REQUIRE_APPROVAL", "true")
    for name, value in {**UNIPILE_CREDENTIALS, **CAPS_ENV}.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def fake_db(monkeypatch):
    """A fresh, empty `FakeFirestore`, wired up as `clients.firestore_client()`.

    Every task-2e test seeds whatever documents it needs directly on the
    returned `db` before calling a tool.
    """
    db = FakeFirestore()
    monkeypatch.setattr(clients, "firestore_client", lambda: db)
    return db


class FakeQuery:
    """Records the probe so a test can assert it stayed cheap.

    Implements `.stream()`, not `.get()` -- matching `_firestore_probe`'s
    own call shape (see its docstring for why `.stream()` replaced `.get()`)
    and, deliberately, NOT the fuller API `tests/linkedinmcp/fake_firestore.
    FakeFirestore` implements: a test using this minimal fake proves the
    probe stays cheap; a test needing the extended `get_status` fields (or
    any of the six new tools) needs the `fake_db` fixture instead.
    """

    def __init__(self, calls: list, fail: Exception | None = None) -> None:
        self.calls = calls
        self._fail = fail

    def select(self, field_paths):
        self.calls.append(("select", list(field_paths)))
        return self

    def limit(self, count):
        self.calls.append(("limit", count))
        return self

    def stream(self):
        self.calls.append(("stream",))
        if self._fail is not None:
            raise self._fail
        return iter([])


class FakeDb:
    def __init__(self, calls: list, fail: Exception | None = None) -> None:
        self.calls = calls
        self._fail = fail

    def collection(self, name):
        self.calls.append(("collection", name))
        return FakeQuery(self.calls, self._fail)


class PermissionDenied(Exception):
    """Stands in for `google.api_core.exceptions.PermissionDenied`, whose
    message carries the project id -- which is why the tool reports the class
    name instead."""


async def call_status() -> dict:
    async with Client(mcp_server.mcp) as client:
        return (await client.call_tool("get_status", {})).data


async def call_tool(name: str, args: dict) -> dict:
    async with Client(mcp_server.mcp) as client:
        return (await client.call_tool(name, args)).data


#: `get_status` and the read-only tools (task 2e; `get_job`, MCP v2).
READ_TOOLS = frozenset({
    "get_status", "list_contacts", "get_contact", "get_conversation",
    "list_queue", "list_decisions", "get_run_report", "get_job",
})

#: The agent-side write tools (task 2g; `queue_message` split into
#: `send_follow_up` and `send_reply`, MCP v2).
AGENT_WRITE_TOOLS = frozenset({
    "send_follow_up", "send_reply", "cancel_queued", "set_handling", "ask_user", "mark_decision_applied",
})

#: The human-side tools (task 2g; `clear_handling` by ruling P2-25): only ever
#: called by a person, never by an unattended agent.
HUMAN_TOOLS = frozenset({
    "answer_decision", "approve_queued", "reject_queued", "pause", "resume",
    "clear_writes_block", "set_require_approval", "clear_handling",
})

#: The process steps (MCP v2): each starts a job. Human-side too, since the
#: scheduled Cloud Scheduler jobs are what run them.
PROCESS_TOOLS = frozenset({
    "sync_messages", "get_contacts", "classify_contacts", "classify_stages", "send_intro",
})


@pytest.mark.anyio
async def test_tool_set_is_exact(env, fake_db):
    """The service exposes exactly twenty-seven tools: `get_status` and seven
    read-only tools, six agent-side write tools, eight human-side tools and
    the five process steps (MCP v2). Ledger ruling P2-11: this pins the
    exact set, so a task adding a tool has to change this line and justify
    it.
    """
    async with Client(mcp_server.mcp) as client:
        tools = await client.list_tools()
    assert {tool.name for tool in tools} == READ_TOOLS | AGENT_WRITE_TOOLS | HUMAN_TOOLS | PROCESS_TOOLS
    assert len(tools) == 27
    assert all(tool.description for tool in tools), "every tool's docstring is what the agent reads"


def test_every_tool_is_a_sync_function():
    """FastMCP runs a sync tool in a worker thread, which is where the
    blocking Firestore calls belong; an `async def` tool would run them on
    the event loop."""
    for name in READ_TOOLS | AGENT_WRITE_TOOLS | HUMAN_TOOLS | PROCESS_TOOLS:
        function = getattr(mcp_server, name)
        assert callable(function) and not inspect.iscoroutinefunction(function), name


@pytest.mark.anyio
async def test_caps_come_from_settings(env, monkeypatch):
    """Breaks if the tool ever hard-codes a cap instead of reading settings."""
    monkeypatch.setattr(clients, "firestore_client", lambda: FakeDb([]))
    payload = await call_status()
    # The whole key set, pinned. The report is meant to stay small: the Claude
    # platform offloads tool output over 100k characters into a sandbox file
    # the agent then has to open before it can act, so a later task adding a
    # field has to change this line and justify it.
    assert set(payload) == {
        "service",
        "time",
        "timezone",
        "caps",
        "require_approval",
        "firestore",
        "unipile",
    }
    assert payload["service"] == "linkedin-outreach"
    assert payload["caps"] == {
        "messages_per_day": 7,
        "profile_fetches_per_day": 23,
        "intro_daily_cap": 4,
        "max_touches": 2,
        "min_days_between_touches": 9,
    }
    assert payload["unipile"] == "ok"
    assert payload["require_approval"] is True
    assert payload["timezone"] == TZ


@pytest.mark.anyio
async def test_time_is_in_the_configured_timezone(env, monkeypatch):
    """Breaks if the tool switches to `datetime.now()` or hard-codes UTC --
    every date the reports show is meant to be the user's local one."""
    monkeypatch.setattr(clients, "firestore_client", lambda: FakeDb([]))
    payload = await call_status()
    assert payload["time"].endswith(TZ_OFFSET)
    assert datetime.fromisoformat(payload["time"]).utcoffset() == timedelta(
        hours=5, minutes=30
    )


@pytest.mark.anyio
async def test_firestore_ok_and_the_probe_stays_cheap(env, monkeypatch):
    """`.select([]).limit(1)` is the point: a bare `.limit(1).stream()` pulls
    a whole document, and `analysis` summaries run to 35 KB. Breaks if a
    later edit drops the projection, or reads a different collection.

    Only the first four calls are pinned. Once the probe succeeds,
    `get_status` also attempts the extended fields (see
    `test_get_status_extended_fields_reflect_a_seeded_state` below), which
    reads `runtime_state` through the SAME `db` -- but this minimal `FakeDb`
    does not implement enough of the API for that read to succeed (no
    `.document()`), so it fails silently (caught -- see `_extended_status`'s
    docstring), leaving `calls` with one more entry than before task 2e.
    """
    calls: list = []
    monkeypatch.setattr(clients, "firestore_client", lambda: FakeDb(calls))
    payload = await call_status()
    assert payload["firestore"] == "ok"
    assert calls[:4] == [("collection", "analysis"), ("select", []), ("limit", 1), ("stream",)]


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["factory", "query"])
async def test_firestore_failure_is_reported_not_raised(env, monkeypatch, where):
    """The important one. A credentials problem must look like a credentials
    problem, not like a broken agent: an agent handed an exception learns
    nothing, while one handed `"firestore": "PermissionDenied"` can tell the
    user what to fix.

    Both failure sites are pinned. On Cloud Run missing Application Default
    Credentials raise at `firestore.Client(...)` *construction*, so a `try`
    that wrapped only the query would let that one escape.

    The four extended fields (task 2e) are never computed when the probe
    itself failed -- asserted here for both failure sites, since the probe
    failing is exactly the condition that must suppress them.
    """
    failure = PermissionDenied("caller lacks datastore.entities.get on vk-linkedin")

    def raising_factory():
        raise failure

    monkeypatch.setattr(
        clients,
        "firestore_client",
        raising_factory if where == "factory" else (lambda: FakeDb([], failure)),
    )
    payload = await call_status()
    assert payload["firestore"] == "PermissionDenied"
    assert "vk-linkedin" not in str(payload), "the message can carry identifiers"
    assert payload["caps"]["intro_daily_cap"] == 4, "the rest of the report survives"
    for key in ("sends_paused_until", "writes_blocked", "queue", "decisions"):
        assert key not in payload


@pytest.mark.anyio
async def test_missing_unipile_credentials_are_reported_not_raised(env, monkeypatch):
    """Same contract as the Firestore probe, for the same reason: a status call
    that explodes tells the agent nothing, while one that says
    `"unipile": "ConfigError"` tells it exactly which credential to go and fix.

    The two rate limits drop out of `caps` when this happens, rather than
    appearing as zero or as some default -- they could not be read, which is not
    the same as being unlimited or being nothing.
    """
    monkeypatch.setattr(clients, "firestore_client", lambda: FakeDb([]))
    monkeypatch.delenv("UNIPILE_API_KEY", raising=False)

    payload = await call_status()

    assert payload["unipile"] == "ConfigError"
    assert "messages_per_day" not in payload["caps"]
    assert "profile_fetches_per_day" not in payload["caps"]
    assert payload["caps"]["intro_daily_cap"] == 4
    assert payload["firestore"] == "ok"


# --- get_status, extended (task 2e) ------------------------------------------


@pytest.mark.anyio
async def test_get_status_require_approval_is_the_effective_value(env, fake_db):
    """`env` sets `OUTREACH_REQUIRE_APPROVAL=true`; the state document stores
    the opposite. A passing `False` here proves the override actually took
    effect rather than the tool merely echoing the configured setting.
    """
    fake_db.collection("runtime_state").document("linkedin").set({"require_approval": False})

    payload = await call_status()

    assert payload["firestore"] == "ok"
    assert payload["require_approval"] is False


@pytest.mark.anyio
async def test_get_status_extended_fields_reflect_a_seeded_state(env, fake_db):
    fake_db.collection("runtime_state").document("linkedin").set(
        {
            "sends_paused_until": datetime(2099, 1, 1, tzinfo=UTC),
            "pause_reason": "testing",
            "fetches_paused_until": datetime(2099, 1, 1, tzinfo=UTC),
            "fetch_pause_reason": "throttled",
            "writes_blocked_at": datetime(2026, 9, 1, tzinfo=UTC),
            "writes_blocked_reason": "restricted",
        }
    )
    queue.enqueue(
        fake_db, "agent:a:20260910", {"contact_doc_id": "a", "kind": "follow_up", "text": "hi"},
        require_approval=False, now=NOW,
    )
    decisions.ask(fake_db, "Send it?", ["yes", "no"], {}, NOW)
    fetch_queue.enqueue(fake_db, "b", provider_id="ACoAAB", name="B", connected_at=NOW, now=NOW)

    payload = await call_status()

    assert payload["firestore"] == "ok"
    assert payload["sends_paused_until"] is not None
    assert payload["sends_paused_until"].endswith(TZ_OFFSET), "settings.tz, like every other date reported"
    assert payload["fetches_paused_until"] is not None
    assert payload["fetches_paused_until"].endswith(TZ_OFFSET)
    assert payload["writes_blocked"] is True
    assert payload["queue"] == {"pending": 0, "approved": 1, "sending": 0, "unknown": 0}
    assert payload["decisions"] == {"pending": 1, "answered": 0}
    assert payload["fetch_queue"] == {"queued": 1, "stored": 0, "short": 0, "failed": 0}


@pytest.mark.anyio
async def test_get_status_extended_fields_have_empty_defaults_with_no_state_seeded(env, fake_db):
    """An empty-but-working `FakeFirestore`: `sends_paused_until` and
    `fetches_paused_until` are `None` (nothing paused), `writes_blocked` is
    `False`, and the three counts are all-zero dicts -- present, not absent,
    since these reads succeeded."""
    payload = await call_status()

    assert payload["firestore"] == "ok"
    assert payload["sends_paused_until"] is None
    assert payload["fetches_paused_until"] is None
    assert payload["writes_blocked"] is False
    assert payload["queue"] == {"pending": 0, "approved": 0, "sending": 0, "unknown": 0}
    assert payload["decisions"] == {"pending": 0, "answered": 0}
    assert payload["fetch_queue"] == {"queued": 0, "stored": 0, "short": 0, "failed": 0}


# --- list_contacts ------------------------------------------------------------


@pytest.mark.anyio
async def test_list_contacts_tool_returns_contacts_and_count(env, fake_db):
    fake_db.collection("analysis").document("a").set(
        {"pipeline_stage": "prospect", "last_reply_date": datetime(2026, 9, 1, tzinfo=UTC)}
    )

    payload = await call_tool("list_contacts", {})

    assert payload["count"] == 1
    assert payload["contacts"][0]["doc_id"] == "a"


@pytest.mark.anyio
async def test_list_contacts_tool_reports_an_unparseable_since_without_raising(env, fake_db):
    payload = await call_tool("list_contacts", {"since": "not-a-date"})

    assert payload == {"ok": False, "reason": "invalid_since"}


# --- get_contact ---------------------------------------------------------------


@pytest.mark.anyio
async def test_get_contact_tool_reports_not_found(env, fake_db):
    payload = await call_tool("get_contact", {"doc_id": "missing"})

    assert payload == {"ok": False, "reason": "not_found"}


@pytest.mark.anyio
async def test_get_contact_tool_returns_the_contact(env, fake_db):
    fake_db.collection("analysis").document("a").set({"pipeline_stage": "lead"})

    payload = await call_tool("get_contact", {"doc_id": "a"})

    assert payload["doc_id"] == "a"
    assert payload["stage"] == "lead"
    assert payload["queue"] == []


# --- get_conversation -----------------------------------------------------------


@pytest.mark.anyio
async def test_get_conversation_tool_reports_no_messages(env, fake_db):
    payload = await call_tool("get_conversation", {"doc_id": "a"})

    assert payload == {"ok": False, "reason": "no_messages"}


@pytest.mark.anyio
async def test_get_conversation_tool_returns_the_transcript(env, fake_db):
    fake_db.collection("messages").document("m1").set(
        {
            "contact_doc_id": "a", "chat_id": "c1", "is_sender": 0,
            "timestamp": datetime(2026, 9, 1, tzinfo=UTC), "text": "hello there",
        }
    )

    payload = await call_tool("get_conversation", {"doc_id": "a"})

    assert payload["doc_id"] == "a"
    assert "hello there" in payload["transcript"]
    assert payload["chat_ids"] == ["c1"]


# --- list_queue -----------------------------------------------------------------


@pytest.mark.anyio
async def test_list_queue_tool_returns_the_documented_row_shape(env, fake_db):
    queue.enqueue(
        fake_db, "agent:a:20260910",
        {"contact_doc_id": "a", "kind": "follow_up", "text": "hi", "name": "Jane"},
        require_approval=False, now=NOW,
    )

    payload = await call_tool("list_queue", {})

    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert set(item) == {
        "id", "contact_doc_id", "name", "kind", "status", "due_at",
        "created_by", "text", "error", "skip_reason", "cancel_reason", "tags",
    }
    assert item["id"] == "agent:a:20260910"
    assert item["text"] == "hi"
    assert item["status"] == queue.APPROVED


@pytest.mark.anyio
async def test_list_queue_tool_rejects_an_unknown_status(env, fake_db):
    payload = await call_tool("list_queue", {"status": "bogus"})

    assert payload["ok"] is False
    assert payload["reason"] == "unknown_status"
    assert "pending" in payload["allowed"]


# --- list_decisions --------------------------------------------------------------


@pytest.mark.anyio
async def test_list_decisions_tool_defaults_to_pending_only(env, fake_db):
    decisions.ask(fake_db, "Send it?", ["yes", "no"], {}, NOW)
    decision_id = decisions.ask(fake_db, "Second question?", ["yes", "no"], {}, NOW)
    decisions.answer(fake_db, decision_id, "yes", NOW)

    payload = await call_tool("list_decisions", {})

    assert len(payload["decisions"]) == 1
    item = payload["decisions"][0]
    assert set(item) == {
        "id", "question", "options", "context", "status", "answer",
        "asked_by", "asked_at", "answered_at",
    }
    assert item["status"] == "pending"
    assert item["question"] == "Send it?"


@pytest.mark.anyio
async def test_list_decisions_tool_rejects_an_unknown_status(env, fake_db):
    payload = await call_tool("list_decisions", {"status": "bogus"})

    assert payload == {"ok": False, "reason": "unknown_status", "allowed": ["pending", "answered", "applied"]}


# --- get_run_report --------------------------------------------------------------


@pytest.mark.anyio
async def test_get_run_report_tool_filters_by_job(env, fake_db):
    fake_db.collection("runs").document("tick:20260910T000000000000Z").set(
        {
            "job": "tick", "started_at": datetime(2026, 9, 10, tzinfo=UTC),
            "finished_at": datetime(2026, 9, 10, 0, 1, tzinfo=UTC), "ok": True,
            "summary": {"sent": 1}, "error": None,
        }
    )
    fake_db.collection("runs").document("daily:20260910T000000000000Z").set(
        {
            "job": "daily", "started_at": datetime(2026, 9, 10, tzinfo=UTC),
            "finished_at": None, "ok": False, "summary": {}, "error": "RuntimeError",
        }
    )

    payload = await call_tool("get_run_report", {"job": "tick"})

    assert len(payload["runs"]) == 1
    run = payload["runs"][0]
    assert run["job"] == "tick"
    assert run["ok"] is True
    assert run["summary"] == {"sent": 1}
    assert run["error"] is None


@pytest.mark.anyio
async def test_get_run_report_tool_with_no_runs_is_empty(env, fake_db):
    payload = await call_tool("get_run_report", {})

    assert payload == {"runs": []}


def seed_run(db, job: str, started_at: datetime, **fields) -> str:
    """One `runs` document under the id `jobs.record_run` gives it."""
    run_id = f"{job}:{started_at:%Y%m%dT%H%M%S%fZ}"
    db.collection("runs").document(run_id).set(
        {"job": job, "started_at": started_at, "finished_at": started_at, "ok": True, "summary": {},
         "error": None, **fields}
    )
    return run_id


@pytest.mark.anyio
async def test_get_run_report_for_a_job_finds_its_runs_however_many_other_runs_came_since(env, fake_db):
    """Final review FI4: two failed daily runs this morning, then sixty
    ticks. `job="daily"` reads the daily runs by their ids -- not the
    newest runs of every job, narrowed afterwards -- newest first."""
    morning = datetime(2026, 9, 10, 6, 30, tzinfo=UTC)
    older = seed_run(fake_db, "daily", morning - timedelta(days=1), ok=False, error="RuntimeError")
    newer = seed_run(fake_db, "daily", morning, ok=False, error="RuntimeError")
    for n in range(60):
        seed_run(fake_db, "tick", morning + timedelta(minutes=4 * (n + 1)))

    payload = await call_tool("get_run_report", {"job": "daily"})

    assert [run["id"] for run in payload["runs"]] == [newer, older]
    assert [(run["job"], run["ok"], run["error"]) for run in payload["runs"]] == [("daily", False, "RuntimeError")] * 2


@pytest.mark.anyio
async def test_get_run_report_for_a_job_is_capped_by_limit_and_holds_only_that_job(env, fake_db):
    """`limit` caps the newest runs of the job. A job whose name merely
    begins with the same letters is another job."""
    start = datetime(2026, 9, 10, 7, tzinfo=UTC)
    ticks = [seed_run(fake_db, "tick", start + timedelta(minutes=4 * n)) for n in range(25)]
    seed_run(fake_db, "tickle", start + timedelta(hours=9))

    three = await call_tool("get_run_report", {"job": "tick", "limit": 3})
    capped = await call_tool("get_run_report", {"job": "tick", "limit": 500})

    assert [run["id"] for run in three["runs"]] == ticks[::-1][:3]
    assert [run["id"] for run in capped["runs"]] == ticks[::-1][:20]


@pytest.mark.anyio
@pytest.mark.parametrize("job", ["a/b", "x" * 1500, ""], ids=["slash", "too-long", "empty"])
async def test_get_run_report_for_a_job_no_run_id_can_start_with_is_empty_without_reading_firestore(
    no_firestore, job
):
    payload = await call_tool("get_run_report", {"job": job})

    assert payload == {"runs": []}


# =============================================================================
# Write tools (task 2g)
# =============================================================================

#: 20:00 UTC on 10 September is 01:30 on 11 September in `TZ` (Asia/Kolkata):
#: a queue id built from the UTC date would end in 20260910, one built from the
#: local date ends in `LOCAL_DAY`.
WRITE_NOW = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)
LOCAL_DAY = "20260911"
AGENT_ITEM = f"agent:a:{LOCAL_DAY}"

FOLLOW_UP = "Checking back in on denial recovery -- is it still on your list this quarter?"


class LowRandom:
    """Stands in for `random.Random`: `uniform(low, high)` records the
    bounds it was asked for and returns `low`."""

    def __init__(self):
        self.calls: list[tuple] = []

    def uniform(self, low, high):
        self.calls.append((low, high))
        return low


class HighRandom(LowRandom):
    """`uniform(low, high)` returns `high`."""

    def uniform(self, low, high):
        super().uniform(low, high)
        return high


@pytest.fixture
def write_db(env, fake_db, monkeypatch):
    """`env` with `OUTREACH_REQUIRE_APPROVAL=false` -- so a stored runtime
    override is what decides -- the clock fixed at `WRITE_NOW`, the empty
    `FakeFirestore` every tool reaches, and `queue_message`'s random delay
    fixed at its low bound, five minutes."""
    monkeypatch.setenv("OUTREACH_REQUIRE_APPROVAL", "false")
    monkeypatch.setattr(clock, "utcnow", lambda: WRITE_NOW)
    monkeypatch.setattr(mcp_server, "_random", LowRandom())
    return fake_db


@pytest.fixture
def no_firestore(write_db, monkeypatch):
    """`write_db`, but reaching Firestore at all fails the tool call."""

    def tripwire():
        raise AssertionError("Firestore was reached")

    monkeypatch.setattr(clients, "firestore_client", tripwire)


def seed_prospect(db, doc_id="a", *, days_since_last_touch=20, chat_id="chat-a", **fields):
    """A prospect due a follow-up under `CAPS_ENV` (9 days apart, at most 2
    touches): one touch so far, `days_since_last_touch` days before
    `WRITE_NOW`, in `chat_id`."""
    seed_contact(
        db, doc_id,
        **{"firstName": "Jane", "lastName": "Doe", "pipeline_stage": "prospect", "sent_total": 1, **fields},
    )
    seed_message(
        db, f"{doc_id}-out-1", doc_id, is_sender=1,
        timestamp=WRITE_NOW - timedelta(days=days_since_last_touch), chat_id=chat_id,
    )


def stored(db, collection: str, doc_id: str) -> dict | None:
    snapshot = db.collection(collection).document(doc_id).get()
    return snapshot.to_dict() if snapshot.exists else None


#: The day of `seed_prospect`'s one outbound message, and the id an agent item
#: queued for contact "a" on that day would have.
EARLIER = WRITE_NOW - timedelta(days=20)
EARLIER_ITEM = "agent:a:20260822"


def seed_in_status(db, queue_id: str, status: str, *, now: datetime = EARLIER, **fields) -> None:
    """One queue item for contact "a", enqueued at `now` and moved to
    `status` through the real `queue` transitions."""
    seed_item(db, queue_id, "a", now=now, require_approval=status == queue.PENDING, **fields)
    if status in (queue.SENDING, queue.UNKNOWN, queue.SENT, queue.FAILED):
        assert queue.claim(db, queue_id, "tick-owner", now)
    if status == queue.UNKNOWN:
        assert queue.mark_unknown(db, queue_id, "claimed and never settled", now)
    elif status == queue.SENT:
        queue.settle(db, queue_id, queue.SENT, now=now, message_id="m-earlier")
    elif status == queue.FAILED:
        queue.settle(db, queue_id, queue.FAILED, now=now, error="UnprocessableError")
    elif status == queue.CANCELLED:
        assert queue.cancel(db, queue_id, "cancelled earlier", now)
    elif status == queue.SKIPPED:
        assert queue.mark_skipped(db, queue_id, "a guard refused it", now)
    assert stored(db, "outreach_queue", queue_id)["status"] == status


# --- queue_message -------------------------------------------------------------


@pytest.mark.anyio
async def test_queue_message_creates_the_agent_item_for_the_local_day(write_db):
    """Given no `due_at`, it is due after the random delay -- five minutes,
    its low bound, under `write_db` (ruling P5-3)."""
    seed_prospect(write_db, profileUrl="https://www.linkedin.com/in/jane-doe")

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    assert payload == {"queued": True, "id": AGENT_ITEM, "status": "approved", "due_at": "2026-09-11T01:35:00+05:30"}
    item = stored(write_db, "outreach_queue", AGENT_ITEM)
    assert item["contact_doc_id"] == "a"
    assert item["kind"] == "follow_up"
    assert item["text"] == FOLLOW_UP
    assert item["chat_id"] == "chat-a"
    assert item["name"] == "Jane Doe"
    assert item["profile_url"] == "https://www.linkedin.com/in/jane-doe"
    assert (item["campaign"], item["template_id"]) == (None, None)
    assert (item["created_by"], item["approved_by"]) == ("agent", "auto")
    assert item["due_at"] == WRITE_NOW + timedelta(minutes=5)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "source, delay", [(LowRandom, timedelta(minutes=5)), (HighRandom, timedelta(minutes=45))], ids=["low", "high"]
)
async def test_queue_message_without_due_at_is_due_five_to_forty_five_minutes_from_now(
    write_db, monkeypatch, source, delay
):
    """Ruling P5-3: with no `due_at`, the delay is drawn between 5 and 45
    minutes (300 and 2,700 seconds) from the module's random source, so a
    burst of queued messages does not go out at the tick's own rhythm."""
    seed_prospect(write_db)
    rng = source()
    monkeypatch.setattr(mcp_server, "_random", rng)

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    assert rng.calls == [(300.0, 2700.0)]
    assert stored(write_db, "outreach_queue", AGENT_ITEM)["due_at"] == WRITE_NOW + delay
    assert payload["due_at"] == (WRITE_NOW + delay).astimezone(ZoneInfo(TZ)).isoformat()


@pytest.mark.anyio
async def test_queue_message_with_a_seeded_random_source_is_due_within_the_range(write_db, monkeypatch):
    seed_prospect(write_db)
    monkeypatch.setattr(mcp_server, "_random", random.Random(20260910))

    await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    due = stored(write_db, "outreach_queue", AGENT_ITEM)["due_at"]
    assert WRITE_NOW + timedelta(minutes=5) <= due <= WRITE_NOW + timedelta(minutes=45)


@pytest.mark.anyio
async def test_send_follow_up_stores_the_campaign_and_template_labels(write_db):
    seed_prospect(write_db)

    await call_tool(
        "send_follow_up",
        {"doc_id": "a", "text": FOLLOW_UP, "campaign": "q3-labs", "template_id": "follow_up_2"},
    )

    item = stored(write_db, "outreach_queue", AGENT_ITEM)
    assert (item["kind"], item["campaign"], item["template_id"]) == ("follow_up", "q3-labs", "follow_up_2")


@pytest.mark.anyio
async def test_queue_message_twice_on_the_same_local_day_is_already_queued_today(write_db):
    seed_prospect(write_db)
    await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": "A second draft."})

    assert payload == {"queued": False, "reason": "already_queued_today", "id": AGENT_ITEM, "status": "approved"}
    assert stored(write_db, "outreach_queue", AGENT_ITEM)["text"] == FOLLOW_UP


@pytest.mark.anyio
async def test_a_guard_refusal_comes_back_as_the_verdict_and_writes_nothing(write_db, monkeypatch):
    """`guards.check_send` replaced by a recorder that refuses: the tool
    returns that verdict's reason and detail, the store is unchanged, and the
    guard was asked about `{kind, contact_doc_id, chat_id}` with
    `enqueueing=True` and the contact's stored data."""
    seed_prospect(write_db)
    write_db.collection("runtime_state").document("linkedin").set({"pause_reason": "seeded"})
    calls = []

    def refuse(item, contact, messages, queue_items, state, settings, now, *, enqueueing=False):
        calls.append(
            {"item": item, "contact": contact, "messages": messages, "queue_items": queue_items,
             "state": state, "now": now, "enqueueing": enqueueing}
        )
        return guards.Verdict(False, "test:refused", "A refusal made up for this test.")

    monkeypatch.setattr(guards, "check_send", refuse)
    before = store_snapshot(write_db)

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    assert payload == {"queued": False, "reason": "test:refused", "detail": "A refusal made up for this test."}
    assert store_snapshot(write_db) == before
    (call,) = calls
    assert call["item"] == {"kind": "follow_up", "contact_doc_id": "a", "chat_id": "chat-a"}
    assert call["enqueueing"] is True
    assert call["now"] == WRITE_NOW
    assert call["contact"]["pipeline_stage"] == "prospect"
    assert [message["chat_id"] for message in call["messages"]] == ["chat-a"]
    assert call["queue_items"] == []
    assert call["state"] == {"pause_reason": "seeded"}


@pytest.mark.anyio
async def test_a_follow_up_too_soon_is_refused_and_writes_nothing(write_db):
    seed_prospect(write_db, days_since_last_touch=2)
    before = store_snapshot(write_db)

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    assert (payload["queued"], payload["reason"]) == (False, "follow_up:too_soon")
    assert payload["detail"]
    assert store_snapshot(write_db) == before


@pytest.mark.anyio
async def test_text_linking_to_a_domain_nobody_allowed_is_refused_and_writes_nothing(write_db):
    seed_prospect(write_db)
    before = store_snapshot(write_db)

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": "See https://example.com for more."})

    assert (payload["queued"], payload["reason"]) == (False, "text:link_not_allowed")
    assert store_snapshot(write_db) == before


@pytest.mark.anyio
async def test_the_queueing_tools_take_no_kind_so_the_agent_cannot_queue_an_intro(env):
    """Intros are queued only by `send_intro` and the daily job: the agent's
    two queueing tools carry their kind in their name, not as an argument."""
    async with Client(mcp_server.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for name in ("send_follow_up", "send_reply"):
        assert "kind" not in tools[name].input_schema["properties"], name


@pytest.mark.anyio
async def test_a_reply_is_queued_pending_for_a_human(write_db):
    seed_prospect(write_db)
    seed_message(
        write_db, "a-in-1", "a", is_sender=0, timestamp=WRITE_NOW - timedelta(days=1),
        chat_id="chat-a", text="Tell me more.",
    )

    payload = await call_tool("send_reply", {"doc_id": "a", "text": "Happy to explain."})

    assert (payload["queued"], payload["status"]) == (True, "pending")
    item = stored(write_db, "outreach_queue", payload["id"])
    assert item["kind"] == "reply"
    assert "approved_by" not in item


@pytest.mark.anyio
async def test_require_approval_stored_in_runtime_state_makes_a_follow_up_pending(write_db):
    seed_prospect(write_db)
    write_db.collection("runtime_state").document("linkedin").set({"require_approval": True})

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    assert (payload["queued"], payload["status"]) == (True, "pending")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "due_at, stored_due, reported_due",
    [
        # Naive: the service's timezone.
        ("2026-09-12T09:00:00", datetime(2026, 9, 12, 3, 30, tzinfo=UTC), "2026-09-12T09:00:00+05:30"),
        # A date alone: local midnight.
        ("2026-09-12", datetime(2026, 9, 11, 18, 30, tzinfo=UTC), "2026-09-12T00:00:00+05:30"),
        # With an offset: taken as given.
        ("2026-09-12T09:00:00+00:00", datetime(2026, 9, 12, 9, 0, tzinfo=UTC), "2026-09-12T14:30:00+05:30"),
        # Before now: now.
        ("2026-09-01T09:00:00", WRITE_NOW, "2026-09-11T01:30:00+05:30"),
    ],
)
async def test_queue_message_due_at(write_db, due_at, stored_due, reported_due):
    seed_prospect(write_db)

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP, "due_at": due_at})

    assert payload["due_at"] == reported_due
    assert stored(write_db, "outreach_queue", AGENT_ITEM)["due_at"] == stored_due


@pytest.mark.anyio
@pytest.mark.parametrize("due_at", ["next tuesday", "2026-13-01T09:00:00", "9999-12-31T23:59:59-12:00"])
async def test_an_unreadable_due_at_is_refused_and_writes_nothing(write_db, due_at):
    """The last value parses, but converting it to UTC overflows the year."""
    seed_prospect(write_db)
    before = store_snapshot(write_db)

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP, "due_at": due_at})

    assert payload == {"queued": False, "reason": "bad_due_at"}
    assert store_snapshot(write_db) == before


@pytest.mark.anyio
async def test_a_guard_that_raises_is_a_refusal_and_writes_nothing(write_db, monkeypatch):
    """The same rule the tick applies: a guard raising on stored data is a
    `guard_error:<class>` refusal, and the exception's message is not
    returned."""
    seed_prospect(write_db)

    def broken(*args, **kwargs):
        raise RuntimeError("a stored datetime was naive")

    monkeypatch.setattr(guards, "check_send", broken)
    before = store_snapshot(write_db)

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    assert (payload["queued"], payload["reason"]) == (False, "guard_error:RuntimeError")
    assert "naive" not in payload["detail"]
    assert store_snapshot(write_db) == before


@pytest.mark.anyio
async def test_queue_message_for_a_contact_with_no_analysis_document_creates_nothing(write_db):
    before = store_snapshot(write_db)

    payload = await call_tool("send_follow_up", {"doc_id": "nobody", "text": FOLLOW_UP})

    assert (payload["queued"], payload["reason"]) == (False, "contact:not_found")
    assert store_snapshot(write_db) == before


@pytest.mark.anyio
@pytest.mark.parametrize(
    "doc_id",
    ["a/b", "", ".", "..", "__reserved__", "x" * 1501, "é" * 751, "x" * 1486],
    ids=["slash", "empty", "dot", "dotdot", "reserved", "1501-ascii", "1502-bytes-utf8", "item-id-1501"],
)
async def test_a_doc_id_no_document_can_have_is_not_found_before_any_firestore_call(no_firestore, doc_id):
    """The last value is a usable document id on its own, but the item's id,
    `agent:{doc_id}:{YYYYMMDD}`, would be 1,501 bytes."""
    payload = await call_tool("send_follow_up", {"doc_id": doc_id, "text": FOLLOW_UP})

    assert (payload["queued"], payload["reason"]) == (False, "contact:not_found")


@pytest.mark.anyio
async def test_the_longest_doc_id_whose_item_id_still_fits_reaches_firestore(write_db, monkeypatch):
    """1,485 bytes: `agent:{doc_id}:{YYYYMMDD}` is exactly 1,500, so the tool
    goes on to read Firestore -- here a tripwire, which records that it was
    reached and raises. The client sees only the masked error."""
    reached = []

    def tripwire():
        reached.append(True)
        raise AssertionError("Firestore was reached")

    monkeypatch.setattr(clients, "firestore_client", tripwire)

    with pytest.raises(ToolError, match="^Error calling tool 'send_follow_up'$"):
        await call_tool("send_follow_up", {"doc_id": "x" * 1485, "text": FOLLOW_UP})

    assert reached == [True]


@pytest.mark.anyio
async def test_an_unexpected_error_in_a_tool_reaches_the_client_without_its_message(env, monkeypatch):
    """Ruling P5-4 (`mask_error_details`): an exception a tool did not turn
    into a result reaches the client as `Error calling tool '<name>'` --
    never with the exception's own text, which can carry project ids, URLs
    or a contact's data."""

    def firestore_down():
        raise RuntimeError("403 caller lacks datastore.entities.get on project vk-linkedin")

    monkeypatch.setattr(clients, "firestore_client", firestore_down)

    with pytest.raises(ToolError) as raised:
        await call_tool("list_queue", {})

    assert str(raised.value) == "Error calling tool 'list_queue'"
    assert "vk-linkedin" not in str(raised.value)


@pytest.mark.anyio
async def test_queue_message_uses_the_chat_of_the_newest_stored_message(write_db, monkeypatch):
    monkeypatch.setenv("OUTREACH_MAX_TOUCHES", "5")
    seed_prospect(write_db, days_since_last_touch=30, chat_id="chat-old")
    seed_message(write_db, "a-out-2", "a", is_sender=1, timestamp=WRITE_NOW - timedelta(days=12), chat_id="chat-new")

    await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    assert stored(write_db, "outreach_queue", AGENT_ITEM)["chat_id"] == "chat-new"


@pytest.mark.anyio
@pytest.mark.parametrize("flag", ["is_event", "deleted"])
async def test_an_event_or_deleted_message_never_chooses_the_chat(write_db, flag):
    """Final review FI2(a), the review's probe: the contact's newest stored
    document is an event (or a deleted message) in a group chat, attributed
    to them. The chat is chosen from their USABLE messages only -- the ones
    the guards reason over -- so the item goes into their own conversation,
    `chat-a`."""
    seed_prospect(write_db)
    seed_message(
        write_db, "a-group-event", "a", is_sender=0, timestamp=WRITE_NOW - timedelta(days=1),
        chat_id="chat-GROUP", **{flag: 1},
    )

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    assert payload["queued"] is True
    assert stored(write_db, "outreach_queue", AGENT_ITEM)["chat_id"] == "chat-a"


@pytest.mark.anyio
async def test_a_reply_to_a_conversation_the_contact_started_is_queued_into_their_chat(write_db):
    """The choice is not narrowed to our own messages: a contact who wrote
    first, in a conversation holding nothing of ours, still gets a `reply`
    queued into their chat."""
    seed_contact(write_db, "a", firstName="Jane", pipeline_stage="prospect")
    seed_message(
        write_db, "a-in-1", "a", is_sender=0, timestamp=WRITE_NOW - timedelta(days=1), chat_id="chat-theirs",
        text="Saw your post -- how does the denial work start?",
    )

    payload = await call_tool("send_reply", {"doc_id": "a", "text": "Happy to explain."})

    assert (payload["queued"], payload["status"]) == (True, "pending")
    assert stored(write_db, "outreach_queue", payload["id"])["chat_id"] == "chat-theirs"


@pytest.mark.anyio
async def test_queue_message_takes_the_name_from_extracted_when_analysis_has_none(write_db):
    seed_prospect(write_db, firstName=None, lastName=None)
    write_db.collection("extracted").document("a").set({"fullName": "Pat Roe", "occupation": "Lab director"})

    await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    item = stored(write_db, "outreach_queue", AGENT_ITEM)
    assert item["name"] == "Pat Roe"
    assert item["profile_url"] == "https://www.linkedin.com/in/a"


@pytest.mark.anyio
async def test_a_malformed_stored_date_the_guards_never_read_does_not_stop_queue_message(write_db):
    """`last_reply_date` stored as a string: no guard reads that field, and
    neither does the name and profile URL lookup, so the item is queued --
    the call does not raise."""
    seed_prospect(write_db, last_reply_date="last Tuesday", last_sent_date="2026-08-21")

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    assert (payload["queued"], payload["id"]) == (True, AGENT_ITEM)
    assert stored(write_db, "outreach_queue", AGENT_ITEM)["name"] == "Jane Doe"


@pytest.mark.anyio
async def test_name_fields_of_the_wrong_type_count_as_missing(write_db):
    """A `firstName` that is a number, an `extracted` `fullName` that is a
    list and a `profileUrl` that is a map are each read as absent: the item
    is queued with what is left -- `lastName` alone, and the slug's public
    profile URL."""
    seed_prospect(write_db, firstName=42, profileUrl={"not": "a url"})
    write_db.collection("extracted").document("a").set({"fullName": ["Pat", "Roe"]})

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    assert payload["queued"] is True
    item = stored(write_db, "outreach_queue", AGENT_ITEM)
    assert (item["name"], item["profile_url"]) == ("Doe", "https://www.linkedin.com/in/a")


@pytest.mark.anyio
async def test_name_lookup_that_raises_is_a_refusal_and_writes_nothing(write_db, monkeypatch):
    """`_contact_identity` replaced by one that raises: the call returns a
    `guard_error:<class>` refusal without the exception's message, and the
    store is unchanged."""
    seed_prospect(write_db)

    def broken(*args, **kwargs):
        raise TypeError("a stored name held something unexpected")

    monkeypatch.setattr(mcp_server, "_contact_identity", broken)
    before = store_snapshot(write_db)

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    assert (payload["queued"], payload["reason"]) == (False, "guard_error:TypeError")
    assert "unexpected" not in payload["detail"]
    assert store_snapshot(write_db) == before


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["pending", "approved", "sending", "unknown"])
async def test_an_open_item_from_an_earlier_day_refuses_a_new_one_and_writes_nothing(write_db, status):
    """Ruling P2-24: an item of the contact's from an earlier day that is
    still pending, approved, sending or unknown refuses the new message with
    `contact:open_item`, naming that item; the store is unchanged."""
    seed_prospect(write_db)
    seed_in_status(write_db, EARLIER_ITEM, status)
    before = store_snapshot(write_db)

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    assert (payload["queued"], payload["reason"]) == (False, "contact:open_item")
    assert (payload["id"], payload["status"]) == (EARLIER_ITEM, status)
    assert EARLIER_ITEM in payload["detail"]
    assert store_snapshot(write_db) == before


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["sent", "failed", "cancelled", "skipped"])
async def test_an_item_from_an_earlier_day_that_has_settled_does_not_block(write_db, status):
    """A sent, failed, cancelled or skipped item from an earlier day is no
    open item: the new message is queued under today's id."""
    seed_prospect(write_db)
    seed_in_status(write_db, EARLIER_ITEM, status)

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    assert (payload["queued"], payload["id"]) == (True, AGENT_ITEM)


@pytest.mark.anyio
async def test_an_open_intro_refuses_an_agent_message_and_writes_nothing(write_db):
    """The daily job's `intro:{doc_id}`, its send unknown until a sync
    resolves it, is an open item too."""
    seed_prospect(write_db)
    seed_in_status(
        write_db, "intro:a", queue.UNKNOWN, kind="intro", chat_id=None, provider_id="p-a", created_by="daily"
    )
    before = store_snapshot(write_db)

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP})

    assert (payload["queued"], payload["reason"]) == (False, "contact:open_item")
    assert (payload["id"], payload["status"]) == ("intro:a", "unknown")
    assert store_snapshot(write_db) == before


@pytest.mark.anyio
async def test_todays_own_item_is_already_queued_today_even_beside_an_older_open_one(write_db):
    """Today's `agent:{doc_id}:{YYYYMMDD}` is reported as itself --
    `already_queued_today`, with its id and status -- rather than as
    `contact:open_item`, even when an older open item exists too; nothing is
    written."""
    seed_prospect(write_db)
    seed_in_status(write_db, EARLIER_ITEM, queue.UNKNOWN)
    seed_in_status(write_db, AGENT_ITEM, queue.PENDING, now=WRITE_NOW)
    before = store_snapshot(write_db)

    payload = await call_tool("send_follow_up", {"doc_id": "a", "text": "A second draft."})

    assert payload == {"queued": False, "reason": "already_queued_today", "id": AGENT_ITEM, "status": "pending"}
    assert store_snapshot(write_db) == before


@pytest.mark.anyio
async def test_the_descriptions_state_the_open_item_rule_and_who_lifts_a_hold(env):
    """The queueing tools name `contact:open_item`; `set_handling` names
    `value_not_allowed` and the human-side `clear_handling`; `clear_handling`
    says it is human-side."""
    async with Client(mcp_server.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert "contact:open_item" in tools["send_follow_up"].description
    assert "contact:open_item" in tools["send_reply"].description
    assert "value_not_allowed" in tools["set_handling"].description
    assert "clear_handling" in tools["set_handling"].description
    assert tools["clear_handling"].description.startswith("HUMAN-SIDE")


# --- cancel_queued -------------------------------------------------------------


@pytest.mark.anyio
async def test_cancel_queued_cancels_an_item_the_agent_queued(write_db):
    seed_item(write_db, AGENT_ITEM, "a", now=WRITE_NOW)

    payload = await call_tool("cancel_queued", {"queue_id": AGENT_ITEM})

    assert payload == {"ok": True}
    item = stored(write_db, "outreach_queue", AGENT_ITEM)
    assert (item["status"], item["cancel_reason"]) == ("cancelled", "cancelled by agent")


@pytest.mark.anyio
async def test_cancel_queued_refuses_an_intro_the_daily_job_queued(write_db):
    seed_item(
        write_db, "intro:a", "a", now=WRITE_NOW, kind="intro", chat_id=None, provider_id="p-a", created_by="daily"
    )

    payload = await call_tool("cancel_queued", {"queue_id": "intro:a"})

    assert payload == {"ok": False}
    assert stored(write_db, "outreach_queue", "intro:a")["status"] == "approved"


# --- set_handling ----------------------------------------------------------------


@pytest.mark.anyio
async def test_set_handling_on_a_missing_contact_creates_no_document(write_db):
    before = store_snapshot(write_db)

    payload = await call_tool("set_handling", {"doc_id": "ghost", "value": "exclude"})

    assert payload == {"ok": False, "reason": "not_found"}
    assert store_snapshot(write_db) == before
    assert stored(write_db, "analysis", "ghost") is None


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["exclude", "manual"])
async def test_set_handling_that_holds_a_contact_cancels_their_open_items(write_db, value):
    seed_contact(write_db, "a", firstName="Jane", pipeline_stage="prospect")
    seed_item(write_db, "intro:a", "a", now=WRITE_NOW, kind="intro", chat_id=None, provider_id="p-a", created_by="daily")
    seed_item(write_db, "agent:a:20260910", "a", now=WRITE_NOW)
    seed_item(write_db, AGENT_ITEM, "a", now=WRITE_NOW, require_approval=True)
    seed_item(write_db, f"agent:b:{LOCAL_DAY}", "b", now=WRITE_NOW)

    payload = await call_tool("set_handling", {"doc_id": "a", "value": value})

    assert payload == {"ok": True, "handling": value, "cancelled": 3}
    for queue_id in ("intro:a", "agent:a:20260910", AGENT_ITEM):
        item = stored(write_db, "outreach_queue", queue_id)
        assert (item["status"], item["cancel_reason"]) == ("cancelled", f"handling set to {value}")
    assert stored(write_db, "outreach_queue", f"agent:b:{LOCAL_DAY}")["status"] == "approved"
    assert stored(write_db, "analysis", "a") == {"firstName": "Jane", "pipeline_stage": "prospect", "handling": value}


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["none", " NONE "])
async def test_set_handling_may_not_lift_a_hold_and_writes_nothing(write_db, value):
    """Ruling P2-25: `none` -- in any case, with any spacing -- is refused
    with the two values the agent may set, and the hold a person set stays."""
    seed_contact(write_db, "a", handling="manual", pipeline_stage="prospect")
    seed_item(write_db, AGENT_ITEM, "a", now=WRITE_NOW)
    before = store_snapshot(write_db)

    payload = await call_tool("set_handling", {"doc_id": "a", "value": value})

    assert payload == {"ok": False, "reason": "value_not_allowed", "allowed": ["exclude", "manual"]}
    assert store_snapshot(write_db) == before


@pytest.mark.anyio
async def test_set_handling_reads_the_value_case_insensitively(write_db):
    seed_contact(write_db, "a", pipeline_stage="prospect")

    payload = await call_tool("set_handling", {"doc_id": "a", "value": " Manual "})

    assert payload["handling"] == "manual"
    assert stored(write_db, "analysis", "a")["handling"] == "manual"


@pytest.mark.anyio
async def test_set_handling_refuses_an_unknown_value_and_writes_nothing(write_db):
    seed_contact(write_db, "a", pipeline_stage="prospect")
    before = store_snapshot(write_db)

    payload = await call_tool("set_handling", {"doc_id": "a", "value": "archive"})

    assert (payload["ok"], payload["reason"]) == (False, "invalid")
    assert payload["detail"] == "value must be exclude or manual."
    assert store_snapshot(write_db) == before


# --- ask_user / mark_decision_applied ------------------------------------------------


@pytest.mark.anyio
async def test_ask_user_stores_a_pending_question_from_the_agent(write_db):
    payload = await call_tool(
        "ask_user",
        {"question": "Start the Q3 drip for pathology labs?", "options": ["yes", "no"],
         "context": {"campaign": "q3-labs", "contacts": 12}},
    )

    assert set(payload) == {"ok", "id"} and payload["ok"] is True
    decision = stored(write_db, "decisions", payload["id"])
    assert decision["question"] == "Start the Q3 drip for pathology labs?"
    assert decision["options"] == ["yes", "no"]
    assert decision["context"] == {"campaign": "q3-labs", "contacts": 12}
    assert (decision["status"], decision["asked_by"], decision["asked_at"]) == ("pending", "agent", WRITE_NOW)


@pytest.mark.anyio
async def test_ask_user_without_options_or_context(write_db):
    payload = await call_tool("ask_user", {"question": "Anything to hold back today?"})

    decision = stored(write_db, "decisions", payload["id"])
    assert (decision["options"], decision["context"]) == ([], {})


@pytest.mark.anyio
@pytest.mark.parametrize(
    "args", [{"question": "   "}, {"question": "Which one?", "context": {"nested": {"too": "deep"}}}]
)
async def test_ask_user_refuses_invalid_input_and_writes_nothing(write_db, args):
    before = store_snapshot(write_db)

    payload = await call_tool("ask_user", args)

    assert (payload["ok"], payload["reason"]) == (False, "invalid")
    assert payload["detail"]
    assert store_snapshot(write_db) == before


@pytest.mark.anyio
async def test_ask_user_description_says_where_the_answer_turns_up(env):
    async with Client(mcp_server.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert "list_decisions" in tools["ask_user"].description


@pytest.mark.anyio
async def test_mark_decision_applied_moves_an_answered_decision_to_applied(write_db):
    decision_id = decisions.ask(write_db, "Send it?", ["yes", "no"], {}, WRITE_NOW)
    decisions.answer(write_db, decision_id, "yes", WRITE_NOW)

    payload = await call_tool("mark_decision_applied", {"decision_id": decision_id})

    assert payload == {"ok": True}
    decision = stored(write_db, "decisions", decision_id)
    assert (decision["status"], decision["applied_at"]) == ("applied", WRITE_NOW)


@pytest.mark.anyio
async def test_mark_decision_applied_refuses_a_decision_nobody_answered(write_db):
    decision_id = decisions.ask(write_db, "Send it?", ["yes", "no"], {}, WRITE_NOW)

    payload = await call_tool("mark_decision_applied", {"decision_id": decision_id})

    assert payload == {"ok": False}
    assert stored(write_db, "decisions", decision_id)["status"] == "pending"


# --- human-side tools --------------------------------------------------------------


@pytest.mark.anyio
async def test_answer_decision_answers_a_pending_decision(write_db):
    decision_id = decisions.ask(write_db, "Send it?", ["yes", "no"], {}, WRITE_NOW)

    payload = await call_tool("answer_decision", {"decision_id": decision_id, "answer": "yes"})

    assert payload == {"ok": True, "id": decision_id, "status": "answered"}
    decision = stored(write_db, "decisions", decision_id)
    assert (decision["answer"], decision["answered_at"]) == ("yes", WRITE_NOW)


@pytest.mark.anyio
async def test_answer_decision_refuses_a_decision_that_is_no_longer_pending(write_db):
    decision_id = decisions.ask(write_db, "Send it?", ["yes", "no"], {}, WRITE_NOW)
    decisions.answer(write_db, decision_id, "yes", WRITE_NOW)

    payload = await call_tool("answer_decision", {"decision_id": decision_id, "answer": "no"})

    assert (payload["ok"], payload["reason"]) == (False, "wrong_status")
    assert stored(write_db, "decisions", decision_id)["answer"] == "yes"


@pytest.mark.anyio
async def test_answer_decision_refuses_a_missing_decision_and_a_blank_answer(write_db):
    decision_id = decisions.ask(write_db, "Send it?", ["yes", "no"], {}, WRITE_NOW)

    missing = await call_tool("answer_decision", {"decision_id": "no-such-decision", "answer": "yes"})
    blank = await call_tool("answer_decision", {"decision_id": decision_id, "answer": "   "})

    assert (missing["ok"], missing["reason"]) == (False, "not_found")
    assert (blank["ok"], blank["reason"]) == (False, "invalid")
    assert stored(write_db, "decisions", decision_id)["status"] == "pending"


@pytest.mark.anyio
async def test_approve_queued_approves_a_pending_item_as_a_human(write_db):
    seed_item(write_db, AGENT_ITEM, "a", now=WRITE_NOW, kind="reply")

    payload = await call_tool("approve_queued", {"queue_id": AGENT_ITEM})

    assert payload == {"ok": True, "id": AGENT_ITEM, "status": "approved"}
    item = stored(write_db, "outreach_queue", AGENT_ITEM)
    assert (item["status"], item["approved_by"], item["approved_at"]) == ("approved", "human", WRITE_NOW)


@pytest.mark.anyio
async def test_approve_queued_refuses_an_item_that_is_not_pending(write_db):
    seed_item(write_db, AGENT_ITEM, "a", now=WRITE_NOW)

    payload = await call_tool("approve_queued", {"queue_id": AGENT_ITEM})

    assert (payload["ok"], payload["reason"]) == (False, "wrong_status")
    assert stored(write_db, "outreach_queue", AGENT_ITEM)["approved_by"] == "auto"


@pytest.mark.anyio
async def test_a_queued_reply_becomes_approved_only_through_approve_queued(write_db):
    """`queue_message` leaves a reply `pending`; `approve_queued` is what moves
    it to `approved`, marked as a human's approval -- which the tick checks
    before it sends a reply."""
    seed_prospect(write_db)
    seed_message(
        write_db, "a-in-1", "a", is_sender=0, timestamp=WRITE_NOW - timedelta(days=1), chat_id="chat-a",
    )

    queued = await call_tool("send_reply", {"doc_id": "a", "text": "Happy to explain."})
    approved = await call_tool("approve_queued", {"queue_id": queued["id"]})

    assert (queued["status"], approved["status"]) == ("pending", "approved")
    assert stored(write_db, "outreach_queue", queued["id"])["approved_by"] == "human"


@pytest.mark.anyio
async def test_reject_queued_cancels_an_open_item_whoever_queued_it(write_db):
    seed_item(write_db, "intro:a", "a", now=WRITE_NOW, kind="intro", chat_id=None, provider_id="p-a", created_by="daily")
    seed_item(write_db, AGENT_ITEM, "a", now=WRITE_NOW)

    custom = await call_tool("reject_queued", {"queue_id": "intro:a", "reason": "not this week"})
    default = await call_tool("reject_queued", {"queue_id": AGENT_ITEM})

    assert custom == {"ok": True, "id": "intro:a", "status": "cancelled"}
    assert default == {"ok": True, "id": AGENT_ITEM, "status": "cancelled"}
    assert stored(write_db, "outreach_queue", "intro:a")["cancel_reason"] == "not this week"
    assert stored(write_db, "outreach_queue", AGENT_ITEM)["cancel_reason"] == "rejected by user"


@pytest.mark.anyio
async def test_reject_queued_refuses_an_item_that_is_no_longer_open(write_db):
    seed_item(write_db, AGENT_ITEM, "a", now=WRITE_NOW)
    queue.cancel(write_db, AGENT_ITEM, "cancelled earlier", WRITE_NOW)

    payload = await call_tool("reject_queued", {"queue_id": AGENT_ITEM})

    assert (payload["ok"], payload["reason"]) == (False, "wrong_status")
    assert stored(write_db, "outreach_queue", AGENT_ITEM)["cancel_reason"] == "cancelled earlier"


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["approve_queued", "reject_queued"])
async def test_a_missing_queue_item_is_not_found(write_db, tool):
    payload = await call_tool(tool, {"queue_id": "agent:nobody:20260911"})

    assert (payload["ok"], payload["reason"]) == (False, "not_found")


#: 1,501 bytes of UTF-8 -- one over Firestore's document-id limit -- in
#: ASCII, and in 751 two-byte characters.
TOO_LONG_ASCII = "x" * 1501
TOO_LONG_UTF8 = "é" * 751


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool, args, reason",
    [
        ("set_handling", {"doc_id": "a/b", "value": "exclude"}, "not_found"),
        ("clear_handling", {"doc_id": "a/b"}, "not_found"),
        ("cancel_queued", {"queue_id": "a/b"}, None),
        ("mark_decision_applied", {"decision_id": "a/b"}, None),
        ("answer_decision", {"decision_id": "a/b", "answer": "yes"}, "not_found"),
        ("approve_queued", {"queue_id": "a/b"}, "not_found"),
        ("reject_queued", {"queue_id": ".."}, "not_found"),
        ("set_handling", {"doc_id": TOO_LONG_ASCII, "value": "exclude"}, "not_found"),
        ("clear_handling", {"doc_id": TOO_LONG_UTF8}, "not_found"),
        ("cancel_queued", {"queue_id": TOO_LONG_ASCII}, None),
        ("mark_decision_applied", {"decision_id": TOO_LONG_UTF8}, None),
        ("answer_decision", {"decision_id": TOO_LONG_ASCII, "answer": "yes"}, "not_found"),
        ("approve_queued", {"queue_id": TOO_LONG_UTF8}, "not_found"),
        ("reject_queued", {"queue_id": TOO_LONG_ASCII}, "not_found"),
    ],
    ids=[
        "set_handling-slash", "clear_handling-slash", "cancel_queued-slash", "mark_decision_applied-slash",
        "answer_decision-slash", "approve_queued-slash", "reject_queued-dotdot",
        "set_handling-too-long", "clear_handling-too-long", "cancel_queued-too-long",
        "mark_decision_applied-too-long", "answer_decision-too-long", "approve_queued-too-long",
        "reject_queued-too-long",
    ],
)
async def test_an_id_no_document_can_have_is_refused_before_any_firestore_call(no_firestore, tool, args, reason):
    payload = await call_tool(tool, args)

    assert payload["ok"] is False
    assert payload.get("reason") == reason


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["get_contact", "get_conversation"])
@pytest.mark.parametrize(
    "doc_id",
    ["a/b", "", "..", "__reserved__", TOO_LONG_ASCII, TOO_LONG_UTF8],
    ids=["slash", "empty", "dotdot", "reserved", "1501-ascii", "1502-bytes-utf8"],
)
async def test_a_read_tool_given_an_id_no_document_can_have_says_not_found_without_reading_firestore(
    no_firestore, tool, doc_id
):
    """Ruling P5-4: the read tools apply the write tools' id rule too, and
    answer `not_found` rather than letting Firestore raise on an id it
    cannot hold."""
    payload = await call_tool(tool, {"doc_id": doc_id})

    assert payload == {"ok": False, "reason": "not_found"}


@pytest.mark.parametrize(
    "value, usable",
    [
        ("x" * 1500, True),
        ("é" * 750, True),
        ("x" * 1501, False),
        ("é" * 751, False),
        ("\ud800", False),
        ("chat\ud800x", False),
        ("", False),
        ("a/b", False),
        ("..", False),
        ("__reserved__", False),
        ("jane-doe", True),
    ],
    ids=[
        "1500-ascii", "1500-bytes-utf8", "1501-ascii", "1502-bytes-utf8", "lone-surrogate",
        "surrogate-inside", "empty", "slash", "dotdot", "reserved", "slug",
    ],
)
def test_usable_id_follows_firestores_document_id_rules(value, usable):
    """At most 1,500 bytes once encoded as UTF-8 (so 750 two-byte characters
    fit and 751 do not); a string UTF-8 cannot encode -- a lone surrogate --
    is unusable, and the check returns rather than raising."""
    assert mcp_server._usable_id(value) is usable


@pytest.mark.anyio
async def test_pause_stores_a_future_pause_given_in_the_service_timezone(write_db):
    payload = await call_tool("pause", {"until": "2026-09-12T09:00:00"})

    assert payload == {"ok": True, "sends_paused_until": "2026-09-12T09:00:00+05:30", "pause_reason": "paused by user"}
    assert stored(write_db, "runtime_state", "linkedin") == {
        "sends_paused_until": datetime(2026, 9, 12, 3, 30, tzinfo=UTC),
        "pause_reason": "paused by user",
    }


@pytest.mark.anyio
async def test_pause_stores_the_reason_given_and_the_default_for_a_blank_one(write_db):
    given = await call_tool("pause", {"until": "2026-09-12T09:00:00+05:30", "reason": "conference week"})
    stored_given = stored(write_db, "runtime_state", "linkedin")["pause_reason"]
    blank = await call_tool("pause", {"until": "2026-09-12T09:00:00+05:30", "reason": "  "})

    assert (given["pause_reason"], stored_given) == ("conference week", "conference week")
    assert blank["pause_reason"] == "paused by user"
    assert stored(write_db, "runtime_state", "linkedin")["pause_reason"] == "paused by user"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "args, reason",
    [
        ({"until": "2026-09-10T19:00:00+00:00"}, "until_not_in_future"),
        ({"until": "2026-09-10T20:00:00+00:00"}, "until_not_in_future"),
        ({"until": "whenever"}, "bad_until"),
    ],
)
async def test_pause_refusals_write_nothing(write_db, args, reason):
    before = store_snapshot(write_db)

    payload = await call_tool("pause", args)

    assert (payload["ok"], payload["reason"]) == (False, reason)
    assert payload["detail"]
    assert store_snapshot(write_db) == before


@pytest.mark.anyio
async def test_pause_refuses_an_unknown_kind_and_writes_nothing(write_db):
    before = store_snapshot(write_db)

    payload = await call_tool("pause", {"until": "2026-09-12T09:00:00", "kind": "profiles"})

    assert payload == {"ok": False, "reason": "unknown_kind", "allowed": ["sends", "fetches"]}
    assert store_snapshot(write_db) == before


@pytest.mark.anyio
async def test_the_pause_description_says_a_sends_pause_leaves_profile_fetches_running(env):
    """Ruling P5-4: under a sends pause the idle tick still fetches
    profiles (ruling P3-6); the description says so, and that pausing
    `fetches` is what stops them."""
    async with Client(mcp_server.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    description = " ".join(tools["pause"].description.split())
    assert "profile fetches continue" in description
    assert 'kind="fetches"' in description


@pytest.mark.anyio
async def test_pause_kind_fetches_stores_the_fetch_pause_and_not_the_send_pause(write_db):
    payload = await call_tool("pause", {"until": "2026-09-12T09:00:00", "kind": "fetches"})

    assert payload == {
        "ok": True,
        "fetches_paused_until": "2026-09-12T09:00:00+05:30",
        "fetch_pause_reason": "paused by user",
    }
    assert stored(write_db, "runtime_state", "linkedin") == {
        "fetches_paused_until": datetime(2026, 9, 12, 3, 30, tzinfo=UTC),
        "fetch_pause_reason": "paused by user",
    }


@pytest.mark.anyio
async def test_resume_clears_the_pause_and_nothing_else(write_db):
    write_db.collection("runtime_state").document("linkedin").set(
        {"sends_paused_until": datetime(2099, 1, 1, tzinfo=UTC), "pause_reason": "held", "require_approval": True}
    )

    payload = await call_tool("resume", {})

    assert payload == {"ok": True, "sends_paused_until": None}
    assert stored(write_db, "runtime_state", "linkedin") == {
        "sends_paused_until": None, "pause_reason": None, "require_approval": True,
    }


@pytest.mark.anyio
async def test_resume_refuses_an_unknown_kind_and_writes_nothing(write_db):
    write_db.collection("runtime_state").document("linkedin").set(
        {"sends_paused_until": datetime(2099, 1, 1, tzinfo=UTC), "pause_reason": "held"}
    )
    before = store_snapshot(write_db)

    payload = await call_tool("resume", {"kind": "profiles"})

    assert payload == {"ok": False, "reason": "unknown_kind", "allowed": ["sends", "fetches"]}
    assert store_snapshot(write_db) == before


@pytest.mark.anyio
async def test_resume_kind_fetches_clears_only_the_fetch_pause(write_db):
    write_db.collection("runtime_state").document("linkedin").set(
        {
            "fetches_paused_until": datetime(2099, 1, 1, tzinfo=UTC),
            "fetch_pause_reason": "throttled",
            "sends_paused_until": datetime(2099, 1, 1, tzinfo=UTC),
            "pause_reason": "held",
        }
    )

    payload = await call_tool("resume", {"kind": "fetches"})

    assert payload == {"ok": True, "fetches_paused_until": None}
    assert stored(write_db, "runtime_state", "linkedin") == {
        "fetches_paused_until": None,
        "fetch_pause_reason": None,
        "sends_paused_until": datetime(2099, 1, 1, tzinfo=UTC),
        "pause_reason": "held",
    }


@pytest.mark.anyio
async def test_clear_writes_block_clears_the_block(write_db):
    """The tool takes no input, so it has no refusal of its own."""
    write_db.collection("runtime_state").document("linkedin").set(
        {"writes_blocked_at": WRITE_NOW - timedelta(hours=2), "writes_blocked_reason": "restricted"}
    )

    payload = await call_tool("clear_writes_block", {})

    assert payload == {"ok": True, "writes_blocked": False}
    assert stored(write_db, "runtime_state", "linkedin") == {"writes_blocked_at": None, "writes_blocked_reason": None}


@pytest.mark.anyio
async def test_set_require_approval_stores_the_override_both_ways(write_db):
    turned_on = await call_tool("set_require_approval", {"value": True})
    stored_on = stored(write_db, "runtime_state", "linkedin")["require_approval"]
    turned_off = await call_tool("set_require_approval", {"value": False})

    assert turned_on == {"ok": True, "require_approval": True}
    assert stored_on is True
    assert turned_off == {"ok": True, "require_approval": False}
    assert stored(write_db, "runtime_state", "linkedin")["require_approval"] is False


@pytest.mark.anyio
async def test_set_require_approval_refuses_a_value_that_is_not_a_boolean(write_db):
    """The protocol layer refuses it -- the parameter is typed `bool` -- before
    the tool runs, so nothing is stored."""
    before = store_snapshot(write_db)

    with pytest.raises(ToolError, match="valid boolean"):
        await call_tool("set_require_approval", {"value": "sometimes"})

    assert store_snapshot(write_db) == before


@pytest.mark.anyio
@pytest.mark.parametrize("hold", ["exclude", "manual"])
async def test_clear_handling_lifts_a_hold_and_touches_nothing_else(write_db, hold):
    """Ruling P2-25's human-side tool: `handling` is merged to null into the
    existing `analysis` document -- every other field kept -- and the
    contact's queue items are left as they are."""
    seed_contact(write_db, "a", firstName="Jane", handling=hold, pipeline_stage="prospect")
    seed_item(write_db, AGENT_ITEM, "a", now=WRITE_NOW, require_approval=True)

    payload = await call_tool("clear_handling", {"doc_id": "a"})

    assert payload == {"ok": True, "handling": None}
    assert stored(write_db, "analysis", "a") == {"firstName": "Jane", "handling": None, "pipeline_stage": "prospect"}
    assert stored(write_db, "outreach_queue", AGENT_ITEM)["status"] == "pending"


@pytest.mark.anyio
async def test_clear_handling_on_a_missing_contact_creates_no_document(write_db):
    before = store_snapshot(write_db)

    payload = await call_tool("clear_handling", {"doc_id": "ghost"})

    assert payload == {"ok": False, "reason": "not_found"}
    assert store_snapshot(write_db) == before
    assert stored(write_db, "analysis", "ghost") is None


# --- process steps and get_job (MCP v2) -------------------------------------------


@pytest.mark.anyio
async def test_send_intro_starts_a_job_that_get_job_follows_to_its_result(env, fake_db, monkeypatch, tmp_path):
    """The inline executor runs the job before `send_intro` returns; the
    dry run's result names who would get the intro, and queues nothing."""
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "intro.md").write_text("Thanks for connecting! Happy to stay in touch.", encoding="utf-8")
    seed_contact(fake_db, "new1", industry="RCM", seniority="Director")
    client = FakeUnipile(relations=[relation("new1", "ACoAANew1", datetime.now(UTC) - timedelta(days=1))])
    monkeypatch.setattr(clients, "unipile_client", lambda: client)

    started = await call_tool("send_intro", {})

    assert (started["ok"], started["status"]) == (True, "succeeded")
    job = await call_tool("get_job", {"job_id": started["job_id"]})
    assert (job["step"], job["params"]["dry_run"]) == ("send_intro", True)
    assert job["result"]["would_queue"] == 1
    assert [row["doc_id"] for row in job["result"]["intros"]] == ["new1"]
    assert list(fake_db.collection("outreach_queue").stream()) == []


@pytest.mark.anyio
async def test_get_contacts_defaults_to_ten_profiles_from_every_connection(env, fake_db, monkeypatch):
    """No date limit unless `days` narrows it: a connection made a year ago
    whose profile is not stored yet is listed."""
    client = FakeUnipile(relations=[relation("old1", "ACoAAOld1", datetime.now(UTC) - timedelta(days=365))])
    monkeypatch.setattr(clients, "unipile_client", lambda: client)

    started = await call_tool("get_contacts", {})

    job = await call_tool("get_job", {"job_id": started["job_id"]})
    assert job["params"] == {"days": 0, "max_profiles": 10, "dry_run": True}
    assert [row["doc_id"] for row in job["result"]["connections"]] == ["old1"]


@pytest.mark.anyio
async def test_send_follow_up_stores_its_tags_and_list_queue_finds_them(write_db):
    seed_prospect(write_db)

    refused = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP, "tags": ["has space"]})
    queued = await call_tool("send_follow_up", {"doc_id": "a", "text": FOLLOW_UP, "tags": ["Recovr", "stage-2"]})

    assert (refused["queued"], refused["reason"]) == (False, "tags:invalid")
    assert queued["queued"] is True
    assert stored(write_db, "outreach_queue", AGENT_ITEM)["tags"] == ["recovr", "stage-2"]
    rows = (await call_tool("list_queue", {"tag": "stage-2"}))["items"]
    assert [(row["id"], row["tags"]) for row in rows] == [(AGENT_ITEM, ["recovr", "stage-2"])]
    assert (await call_tool("list_queue", {"tag": "stage-1"}))["items"] == []


@pytest.mark.anyio
async def test_list_contacts_finds_who_has_not_replied_to_a_tagged_message(write_db):
    """The drip's question: who got campaign `recovr`, `stage-1` and has not
    answered it? `b` replied after it; `c` got a different stage."""
    for doc_id, replied_at in (("a", None), ("b", EARLIER + timedelta(days=1)), ("c", None)):
        seed_contact(write_db, doc_id, pipeline_stage="prospect", last_sent_date=EARLIER, last_reply_date=replied_at)
        tags = ["recovr", "stage-1"] if doc_id != "c" else ["recovr", "stage-2"]
        seed_item(write_db, f"agent:{doc_id}:20260822", doc_id, now=EARLIER, tags=tags)
        queue.claim(write_db, f"agent:{doc_id}:20260822", "tick-owner", EARLIER)
        queue.settle(write_db, f"agent:{doc_id}:20260822", queue.SENT, now=EARLIER, message_id=f"m-{doc_id}")

    silent = await call_tool("list_contacts", {"tags": ["recovr", "stage-1"], "replied": False})
    everyone = await call_tool("list_contacts", {"tags": ["recovr", "stage-1"]})

    assert [(row["doc_id"], row["tagged"]["replied"]) for row in silent["contacts"]] == [("a", False)]
    assert sorted(row["doc_id"] for row in everyone["contacts"]) == ["a", "b"]
    assert (await call_tool("list_contacts", {"replied": False}))["reason"] == "invalid"


@pytest.mark.anyio
async def test_send_intro_puts_its_tags_on_every_intro_it_queues(env, fake_db, monkeypatch, tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "intro.md").write_text("Thanks for connecting! Happy to stay in touch.", encoding="utf-8")
    seed_contact(fake_db, "new1", industry="RCM", seniority="Director")
    client = FakeUnipile(relations=[relation("new1", "ACoAANew1", datetime.now(UTC) - timedelta(days=1))])
    monkeypatch.setattr(clients, "unipile_client", lambda: client)

    await call_tool("send_intro", {"dry_run": False, "tags": ["recovr", "stage-1"]})

    assert stored(fake_db, "outreach_queue", "intro:new1")["tags"] == ["recovr", "stage-1"]


@pytest.mark.anyio
async def test_classify_contacts_and_send_intro_default_to_every_contact(env, fake_db, monkeypatch, tmp_path):
    """The limit is a count of contacts; `days` only narrows. A connection
    made a year ago gets the intro unless `days` says otherwise."""
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "intro.md").write_text("Thanks for connecting! Happy to stay in touch.", encoding="utf-8")
    seed_contact(fake_db, "old1", industry="RCM", seniority="Director")
    client = FakeUnipile(relations=[relation("old1", "ACoAAOld1", datetime.now(UTC) - timedelta(days=365))])
    monkeypatch.setattr(clients, "unipile_client", lambda: client)

    intro = await call_tool("get_job", {"job_id": (await call_tool("send_intro", {}))["job_id"]})
    classify = await call_tool("get_job", {"job_id": (await call_tool("classify_contacts", {}))["job_id"]})

    assert (intro["params"]["days"], classify["params"]["days"]) == (0, 0)
    assert [row["doc_id"] for row in intro["result"]["intros"]] == ["old1"]


@pytest.mark.anyio
async def test_a_process_step_refuses_settings_outside_its_range_and_starts_nothing(env, fake_db):
    refused = await call_tool("send_intro", {"industries": ["Hospital"]})
    assert (refused["reason"], refused["refused"]) == ("industry_not_allowed", ["Hospital"])
    assert (await call_tool("get_contacts", {"max_profiles": 11}))["reason"] == "invalid"
    assert (await call_tool("classify_contacts", {"doc_ids": ["a/b"]}))["reason"] == "invalid"
    assert list(fake_db.collection("runs").stream()) == []


@pytest.mark.anyio
async def test_get_job_answers_not_found_for_an_unknown_id(env, fake_db):
    assert await call_tool("get_job", {"job_id": "send_intro:20990101T000000000000Z"}) == {
        "ok": False, "reason": "not_found",
    }
