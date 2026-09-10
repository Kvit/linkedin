"""`get_status` over the real MCP protocol, in memory.

Driven through `fastmcp.Client(mcp)` -- the in-memory transport, which speaks
the same JSON-RPC the deployed service does but opens no socket. So these
tests exercise tool registration, the generated schema and the serialised
result rather than a plain Python function call.

Nothing here touches Firestore. `clients.firestore_client` is replaced by a
fake in every test that reaches it, which is the whole reason
`linkedinmcp/clients.py` exists and the whole reason `mcp_server` reaches it as
`clients.firestore_client()` through the module instead of binding the name:
`monkeypatch.setattr(clients, "firestore_client", ...)` cannot reach a name
that was imported directly.

Settings arrive the way they do in production -- `cfg.get_settings()` reads
the environment -- with `monkeypatch.setenv` supplying deliberately unusual
values, so a tool that hard-coded a cap fails here. The fixture also `chdir`s
into a tmp directory, because `get_settings()` calls `from_env(".env")` and
`Python/.env` really does exist for the notebooks; a test must neither read it
nor depend on what is in it.

Async tests run under anyio's pytest plugin. `pytest-asyncio` is not installed
and must not be added.
"""

from datetime import datetime, timedelta

import pytest
from fastmcp import Client

from linkedinmcp import clients, mcp_server

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


class FakeQuery:
    """Records the probe so a test can assert it stayed cheap."""

    def __init__(self, calls: list, fail: Exception | None = None) -> None:
        self.calls = calls
        self._fail = fail

    def select(self, field_paths):
        self.calls.append(("select", list(field_paths)))
        return self

    def limit(self, count):
        self.calls.append(("limit", count))
        return self

    def get(self):
        self.calls.append(("get",))
        if self._fail is not None:
            raise self._fail
        return []


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


@pytest.mark.anyio
async def test_get_status_is_the_only_tool(env, monkeypatch):
    """One tool, on purpose: this is the walking skeleton.

    A later task adding a tool has to change this line, which is where it gets
    asked whether the new tool has a test.
    """
    monkeypatch.setattr(clients, "firestore_client", lambda: FakeDb([]))
    async with Client(mcp_server.mcp) as client:
        tools = await client.list_tools()
    assert [tool.name for tool in tools] == ["get_status"]
    assert tools[0].description, "the docstring is what the agent reads"


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
    """`.select([])` is the point: a bare `.limit(1).get()` pulls a whole
    document, and `analysis` summaries run to 35 KB. Breaks if a later edit
    drops the projection, or reads a different collection."""
    calls: list = []
    monkeypatch.setattr(clients, "firestore_client", lambda: FakeDb(calls))
    payload = await call_status()
    assert payload["firestore"] == "ok"
    assert calls == [("collection", "analysis"), ("select", []), ("limit", 1), ("get",)]


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
