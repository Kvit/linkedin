"""The ASGI app: what is authenticated, what is open, and what shape the URL is.

**This module imports `app` at the top of the file with no `OUTREACH_*`
variable set and `Python/` as the working directory. Collection succeeding is
itself an assertion** -- it is what proves nothing in `app.py` loads settings
at import time. That is why `create_app` is a factory and there is no
module-level `app` object, and it is why `settings.load_environment`, which
`create_app` calls, names `.env` explicitly: a bare `load_dotenv()` calls
`find_dotenv()`, which walks up the tree and would pick up whatever it found
first. Anyone tempted to "simplify" this back to `app = create_app()` should
note that the whole module would then error out during collection.

Each app is built with its own settings object, so no middleware instance
leaks between tests, and requests are driven over `httpx.ASGITransport` with
`follow_redirects=False`. The Claude app's connector does not follow redirects
either, so a test that passed only by following one would hide exactly the
failure it exists to catch.

Every request runs inside `app.router.lifespan_context(...)`: the MCP session
manager starts there, and without it the server accepts the connection and
then fails the call.

Async tests run under anyio's pytest plugin. `pytest-asyncio` is not installed
and must not be added.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from mcp.types import LATEST_PROTOCOL_VERSION

from linkedinmcp import app, clients, clock, jobs, monitor, settings as cfg, state
from linkedinmcp.http_auth import ApiKeyMiddleware
from linkedinmcp.settings import OutreachSettings
from tests.linkedinmcp.fake_firestore import FakeFirestore

API_KEY = "test-key-not-a-real-secret"

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

#: One JSON-RPC `initialize`, built once. `protocolVersion` comes from the
#: installed SDK rather than a pasted date string, which the SDK would be
#: entitled to reject after an upgrade.
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "test-app", "version": "0"},
    },
}

#: The MCP HTTP binding requires *both* media types in `Accept` and answers
#: 406 without them, which is a confusing way to fail an auth test.
MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


@pytest.fixture
def anyio_backend():
    """Run the async tests on asyncio only. anyio would otherwise also try trio,
    which is not installed."""
    return "asyncio"


@pytest.fixture
def service():
    """A fresh app per test, with settings passed in.

    Passing `settings` skips `load_dotenv` and `get_settings()` entirely, so
    the test neither reads the repository's `.env` nor depends on the
    environment.
    """
    return app.create_app(OutreachSettings(api_key=API_KEY, _env_file=None))


@asynccontextmanager
async def driving(asgi_app, **transport_kwargs):
    """An `httpx.AsyncClient` wired to `asgi_app`, inside its lifespan.

    Entering the lifespan is what starts FastMCP's session manager, so every
    test that reaches `/mcp/` through this helper is also an assertion that
    `create_app` passed `lifespan=mcp_app.lifespan` to `FastAPI(...)`. Drop that
    argument and these tests stop working, which is the only place that wiring
    is actually pinned -- see `test_lifespan_must_actually_run` for why it is
    not pinned there.
    """
    async with asgi_app.router.lifespan_context(asgi_app):
        transport = httpx.ASGITransport(app=asgi_app, **transport_kwargs)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            yield client


def test_create_app_without_settings_loads_the_environment_first(monkeypatch, tmp_path):
    """With no settings passed, `create_app` calls `settings.load_environment()`
    once and only then `get_settings()` -- both replaced here. The working
    directory is an empty `tmp_path`, so no version of `create_app` can reach
    `Python/.env` from this test."""
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(cfg, "load_environment", lambda: calls.append("load_environment"))

    def fake_get_settings():
        calls.append("get_settings")
        return OutreachSettings(api_key=API_KEY, _env_file=None)

    monkeypatch.setattr(cfg, "get_settings", fake_get_settings)

    app.create_app()

    assert calls == ["load_environment", "get_settings"]


def test_create_app_with_settings_loads_nothing(monkeypatch, tmp_path):
    """Passing settings skips environment loading entirely, as before."""
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(cfg, "load_environment", lambda: calls.append("load_environment"))
    monkeypatch.setattr(cfg, "get_settings", lambda: calls.append("get_settings"))

    app.create_app(OutreachSettings(api_key=API_KEY, _env_file=None))

    assert calls == []


def test_no_route_ends_in_z(service):
    """No path this app serves may end in `z`, because on Cloud Run such a path
    may never reach the container at all.

    Google's front end reserves some paths ending in `z` and answers them
    itself; Cloud Run's known-issues page recommends "avoiding all paths that
    end in z" (https://docs.cloud.google.com/run/docs/known-issues). This is
    not hypothetical: v1.0.0 served its liveness check at `/healthz`, which
    passed every test in this file and, once deployed, returned Google's own
    HTML 404 without the request ever reaching uvicorn. No local test can
    reproduce the front end, so the rule itself is pinned instead.
    """
    paths = [route.path for route in service.routes]
    assert [path for path in paths if path.rstrip("/").endswith("z")] == []


@pytest.mark.anyio
async def test_health_is_open(service):
    """A person or an uptime monitor checking the service holds no key, so
    `/health` must answer without one. It does because the auth middleware is
    attached to the mounted MCP app and not to `app` -- adding an app-wide
    auth middleware would break this, and so would moving `/health` under
    `/mcp`.
    """
    async with driving(service) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.anyio
async def test_auth_is_scoped_by_where_it_is_mounted_not_by_path(service):
    """`/health` is open because of *where* the middleware sits, never
    because a path is exempt.

    A second trivial app is mounted here under the same middleware and must be
    gated too. Without this, an `if path == "/health"` exemption inside the
    middleware would pass the test above while leaving every future
    unauthenticated route one typo away from being exposed -- so this is the
    test that tells "mounted under auth" apart from "path happens to be
    allowed".
    """

    async def trivial(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"gated"})

    service.mount("/gated", ApiKeyMiddleware(trivial, api_key=API_KEY))

    async with driving(service) as client:
        open_route = await client.get("/health")
        without_key = await client.get("/gated/probe")
        with_key = await client.get("/gated/probe", headers={"x-api-key": API_KEY})

    assert open_route.status_code == 200, "not mounted under the middleware"
    assert without_key.status_code == 401, "the middleware is path-aware; it must not be"
    assert with_key.status_code == 200


@pytest.mark.anyio
async def test_mcp_without_trailing_slash_is_served_not_redirected(service):
    """`/mcp` answers exactly like `/mcp/`, with no redirect in between.

    The Claude app's connector does not follow redirects. Up to v1.0.1 this
    file pinned a 307 from `/mcp` to `/mcp/` as intended behaviour, and in
    production every connector configured without the slash died on that 307
    with "Couldn't reach". People type `/mcp`, and Claude's own connector
    documentation writes its example URLs that way.
    """
    async with driving(service) as client:
        response = await client.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "x-api-key": API_KEY}
        )
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "linkedin-outreach"


@pytest.mark.anyio
async def test_mcp_without_trailing_slash_still_requires_the_key(service):
    """The slashless spelling passes the same key check as `/mcp/`.

    **Sent deliberately without a key.** Serving `/mcp` through anything but
    the mounted MCP app -- a second mount, a route pointed straight at
    FastMCP's handler -- could skip `ApiKeyMiddleware` and put every tool on
    the open internet. This is the test that would notice.
    """
    async with driving(service) as client:
        response = await client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert response.status_code == 401


@pytest.mark.anyio
async def test_initialize_with_x_api_key(service):
    """The happy path the Claude connector platform takes. Breaks if the
    middleware stops reading `x-api-key`, or if `lifespan=mcp_app.lifespan`
    is dropped from the `FastAPI(...)` call.
    """
    async with driving(service) as client:
        response = await client.post(
            "/mcp/", json=INITIALIZE, headers={**MCP_HEADERS, "x-api-key": API_KEY}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert body["result"]["serverInfo"]["name"] == "linkedin-outreach"
    assert "error" not in body


@pytest.mark.anyio
async def test_initialize_with_bearer(service):
    """Cloud Scheduler and `claude mcp add` send the same token as
    `Authorization: Bearer`. Breaks if that header form is dropped."""
    async with driving(service) as client:
        response = await client.post(
            "/mcp/",
            json=INITIALIZE,
            headers={**MCP_HEADERS, "authorization": f"Bearer {API_KEY}"},
        )
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "linkedin-outreach"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers, case",
    [({"x-api-key": "wrong-key"}, "wrong key"), ({}, "no key at all")],
)
async def test_unauthenticated_requests_are_rejected(service, headers, case):
    """401, not 200 and not 500. Breaks the moment the middleware stops being
    passed to `http_app(...)`, which is the failure that would silently expose
    every tool on the internet."""
    async with driving(service) as client:
        response = await client.post(
            "/mcp/", json=INITIALIZE, headers={**MCP_HEADERS, **headers}
        )
    assert response.status_code == 401, case
    assert API_KEY not in response.text


@pytest.mark.anyio
async def test_get_on_mcp_is_405(service):
    """Stateless mode serves POST only; there is no SSE stream to open.
    Breaks if `stateless_http=True` is ever dropped -- which would also make
    the service depend on sticky sessions it will not get across Cloud Run
    instances.
    """
    async with driving(service) as client:
        response = await client.get(
            "/mcp/", headers={**MCP_HEADERS, "x-api-key": API_KEY}
        )
    assert response.status_code == 405


@pytest.mark.anyio
async def test_cloud_run_host_header_is_accepted(service):
    """A Cloud Run hostname in `Host` is answered, not rejected.

    Be precise about what this proves. In the installed fastmcp 4.0.3,
    `http_app`'s `host_origin_protection`, `allowed_hosts` and
    `allowed_origins` all default to `None` and the resolved
    `fastmcp.settings.http_host_origin_protection` is `False` -- **protection
    is off**. So this does not show that a `*.run.app` host would pass a
    protection layer. It pins that the protection stays off: switching it on
    later without adding the deployed hostname would break the service in
    production, and this test is what would catch that here instead of there.
    """
    async with driving(service) as client:
        response = await client.post(
            "/mcp/",
            json=INITIALIZE,
            headers={
                **MCP_HEADERS,
                "x-api-key": API_KEY,
                "host": "linkedin-outreach-123456.us-central1.run.app",
            },
        )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_lifespan_must_actually_run(service):
    """Outside the lifespan the MCP session manager was never started, and the
    request fails hard rather than answering wrongly.

    **This test does not pin `lifespan=mcp_app.lifespan`, and an earlier version
    of this docstring wrongly claimed it did.** Reaching the app without
    entering any lifespan raises the identical error whether that argument is
    present or absent, because neither app got the chance to start. The failure
    observed here is caused by this test's own transport setup.

    What it does pin is worth having: the session manager really is
    lifespan-gated, so a sibling test that forgets `driving()` fails loudly
    instead of passing against a half-started server.

    The wiring itself is pinned by every `driving()`-based test in this file --
    they enter the lifespan and then succeed, which they could not do if
    `create_app` stopped passing it. Do not prune those as redundant; they are
    the guarantee.

    Of the two options here: `pytest.raises`, not `raise_app_exceptions=False`
    and a 500. `httpx.ASGITransport` re-raises application exceptions by
    default, so this never comes back as a status code anyway, and swallowing
    it into a bare 500 would let any unrelated breakage satisfy the assertion.
    FastMCP names the cause in the message, so `match` pins the reason.
    """
    transport = httpx.ASGITransport(app=service)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as client:
        with pytest.raises(RuntimeError, match="lifespan"):
            await client.post(
                "/mcp/", json=INITIALIZE, headers={**MCP_HEADERS, "x-api-key": API_KEY}
            )


# --- POST /jobs/{job} and POST /webhooks/unipile --------------------------------
#
# Both routes sit on the FastAPI app, outside the `/mcp` mount, behind
# `http_auth.require_api_key` -- which reads the expected key from
# `cfg.get_settings()` at request time, not from the settings `create_app`
# was given. The `backend` fixture therefore replaces `cfg.get_settings` with
# the same settings object, alongside both client factories and every job.

KEY = {"x-api-key": API_KEY}


class FakeLinkedIn:
    """Stands in for `lib.unipile.UnipileClient`: records `close()`."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class Recorder:
    """A replacement for one `jobs` function: records every call's
    arguments, then returns `result` or raises `error`."""

    def __init__(self, result: dict) -> None:
        self.result = result
        self.error: Exception | None = None
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


class MessageBearingError(Exception):
    """An exception whose message must never reach an HTTP response."""


@pytest.fixture
def backend(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = OutreachSettings(api_key=API_KEY, allow_http_dry_run=True, _env_file=None)
    monkeypatch.setattr(cfg, "get_settings", lambda: settings)
    built: list[str] = []
    db = FakeFirestore()
    linkedin = FakeLinkedIn()

    def firestore_client():
        built.append("firestore")
        return db

    def unipile_client():
        built.append("unipile")
        return linkedin

    monkeypatch.setattr(clients, "firestore_client", firestore_client)
    monkeypatch.setattr(clients, "unipile_client", unipile_client)
    monkeypatch.setattr(clock, "utcnow", lambda: NOW)
    recorders = {name: Recorder({"job": name, "idle": True}) for name in ("tick", "sync", "daily")}
    for name, recorder in recorders.items():
        monkeypatch.setattr(jobs, name, recorder)
    webhook = Recorder({"accepted": True, "reason": "sync_requested"})
    monkeypatch.setattr(jobs, "handle_unipile_webhook", webhook)
    return SimpleNamespace(
        settings=settings, app=app.create_app(settings), db=db, linkedin=linkedin,
        built=built, jobs=recorders, webhook=webhook,
    )


def _nothing_ran(backend) -> bool:
    return backend.built == [] and all(not r.calls for r in (*backend.jobs.values(), backend.webhook))


@pytest.mark.anyio
async def test_jobs_endpoint_without_a_key_is_401(backend):
    async with driving(backend.app) as client:
        response = await client.post("/jobs/tick")

    assert response.status_code == 401
    assert _nothing_ran(backend)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [{"x-api-key": "not-the-key-at-all-0123"}, {"authorization": "Bearer not-the-key-at-all-0123"}],
    ids=["x-api-key", "bearer"],
)
async def test_jobs_endpoint_with_a_wrong_key_is_401(backend, headers):
    """A key that is present but wrong, in either accepted header: 401, the
    expected key is not in the response, and no client is built and no job
    runs."""
    async with driving(backend.app) as client:
        response = await client.post("/jobs/tick", headers=headers)

    assert response.status_code == 401
    assert API_KEY not in response.text
    assert _nothing_ran(backend)


@pytest.mark.anyio
async def test_an_unknown_job_without_a_key_is_401_not_404(backend):
    """The key is checked before the job name: a caller without it gets 401
    for a job that does not exist, the same answer as for one that does."""
    async with driving(backend.app) as client:
        response = await client.post("/jobs/send")

    assert response.status_code == 401
    assert _nothing_ran(backend)


@pytest.mark.anyio
@pytest.mark.parametrize("injected_allows", [True, False], ids=["injected-allows", "injected-refuses"])
async def test_the_job_gets_the_settings_create_app_was_given(backend, monkeypatch, injected_allows):
    """Two settings objects that disagree on `allow_http_dry_run`: one given
    to `create_app`, the other returned by `cfg.get_settings()` at request
    time (which `require_api_key` reads for the key alone). The given one
    decides the dry run -- 200 when it allows one, 400 when it does not --
    and it is the very object every job call receives."""
    injected = OutreachSettings(api_key=API_KEY, allow_http_dry_run=injected_allows, _env_file=None)
    at_request_time = OutreachSettings(api_key=API_KEY, allow_http_dry_run=not injected_allows, _env_file=None)
    monkeypatch.setattr(cfg, "get_settings", lambda: at_request_time)
    service = app.create_app(injected)

    async with driving(service) as client:
        dry = await client.post("/jobs/tick?dry_run=1", headers=KEY)
        real = await client.post("/jobs/tick", headers=KEY)

    assert dry.status_code == (200 if injected_allows else 400)
    assert real.status_code == 200
    calls = backend.jobs["tick"].calls
    assert [kwargs["dry_run"] for _args, kwargs in calls] == ([True, False] if injected_allows else [False])
    assert all(args[2] is injected for args, _kwargs in calls)


@pytest.mark.anyio
async def test_jobs_endpoint_runs_the_job_and_returns_its_summary(backend):
    async with driving(backend.app) as client:
        response = await client.post("/jobs/tick", headers=KEY)

    assert response.status_code == 200
    assert response.json() == {"job": "tick", "idle": True}
    (args, kwargs), = backend.jobs["tick"].calls
    assert args == (backend.db, backend.linkedin, backend.settings, NOW)
    assert kwargs["dry_run"] is False
    assert isinstance(kwargs["state"], state.RuntimeState)
    assert backend.linkedin.closed == 1


@pytest.mark.anyio
async def test_jobs_endpoint_passes_dry_run_through(backend):
    async with driving(backend.app) as client:
        response = await client.post("/jobs/daily?dry_run=1", headers=KEY)

    assert response.status_code == 200
    assert backend.jobs["daily"].calls[0][1]["dry_run"] is True


@pytest.mark.anyio
async def test_sync_endpoint_classifies_except_under_a_dry_run(backend):
    async with driving(backend.app) as client:
        await client.post("/jobs/sync", headers=KEY)
        await client.post("/jobs/sync?dry_run=1", headers=KEY)

    first, second = (kwargs for _args, kwargs in backend.jobs["sync"].calls)
    assert first["classify"] is jobs.default_classify
    assert (second["classify"], second["dry_run"]) == (None, True)


@pytest.mark.anyio
async def test_an_unknown_job_is_404(backend):
    async with driving(backend.app) as client:
        response = await client.post("/jobs/send", headers=KEY)

    assert response.status_code == 404
    assert _nothing_ran(backend)


@pytest.mark.anyio
async def test_dry_run_is_400_when_the_injected_settings_disallow_it(backend):
    """`create_app`'s own settings decide: dry runs are off in them, even
    though the settings `cfg.get_settings()` returns would allow one."""
    service = app.create_app(OutreachSettings(api_key=API_KEY, allow_http_dry_run=False, _env_file=None))

    async with driving(service) as client:
        refused = await client.post("/jobs/tick?dry_run=1", headers=KEY)
        assert _nothing_ran(backend)
        real = await client.post("/jobs/tick", headers=KEY)

    assert refused.status_code == 400
    assert real.status_code == 200


@pytest.mark.anyio
async def test_a_failing_job_is_500_naming_only_the_exception_class(backend):
    backend.jobs["tick"].error = MessageBearingError("projects/vk-linkedin detail for nobody")

    async with driving(backend.app) as client:
        response = await client.post("/jobs/tick", headers=KEY)

    assert response.status_code == 500
    assert response.json() == {"ok": False, "error": "MessageBearingError"}
    assert "detail for nobody" not in response.text
    assert "vk-linkedin" not in response.text


@pytest.mark.anyio
async def test_the_linkedin_client_is_closed_even_when_the_job_raises(backend):
    backend.jobs["daily"].error = RuntimeError("boom")

    async with driving(backend.app) as client:
        response = await client.post("/jobs/daily", headers=KEY)

    assert response.status_code == 500
    assert backend.linkedin.closed == 1


@pytest.mark.anyio
async def test_a_client_factory_failure_is_500_naming_only_the_exception_class(backend, monkeypatch):
    def broken_unipile_client():
        raise MessageBearingError("UNIPILE_API_KEY is missing from /app/.env")

    monkeypatch.setattr(clients, "unipile_client", broken_unipile_client)

    async with driving(backend.app) as client:
        response = await client.post("/jobs/sync", headers=KEY)

    assert response.status_code == 500
    assert response.json() == {"ok": False, "error": "MessageBearingError"}
    assert backend.jobs["sync"].calls == []


@pytest.mark.anyio
async def test_webhook_without_a_key_is_401_before_the_body_is_read(backend):
    async with driving(backend.app) as client:
        response = await client.post(
            "/webhooks/unipile", content=b"not json", headers={"content-type": "application/json"}
        )

    assert response.status_code == 401
    assert _nothing_ran(backend)


@pytest.mark.anyio
async def test_webhook_hands_the_payload_to_the_handler_and_returns_its_result(backend):
    payload = {"event": "message_received", "message_id": "m-1", "chat_id": "c-1"}

    async with driving(backend.app) as client:
        response = await client.post("/webhooks/unipile", json=payload, headers=KEY)

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "reason": "sync_requested"}
    (args, kwargs), = backend.webhook.calls
    assert args == (backend.db, payload, NOW)
    assert isinstance(kwargs["state"], state.RuntimeState)
    assert "unipile" not in backend.built, "a webhook never builds a LinkedIn client"


@pytest.mark.anyio
async def test_webhook_answers_200_for_an_ignored_event(backend):
    """Unipile retries anything but a 200, so an ignored delivery is a 200."""
    backend.webhook.result = {"accepted": False, "reason": "event_ignored"}

    async with driving(backend.app) as client:
        response = await client.post("/webhooks/unipile", json={"event": "account_status"}, headers=KEY)

    assert response.status_code == 200
    assert response.json() == {"accepted": False, "reason": "event_ignored"}


@pytest.mark.anyio
@pytest.mark.parametrize("body", [b"not json", b"", b"{\"event\": "])
async def test_webhook_body_that_is_not_json_is_400(backend, body):
    async with driving(backend.app) as client:
        response = await client.post(
            "/webhooks/unipile", content=body, headers={**KEY, "content-type": "application/json"}
        )

    assert response.status_code == 400
    assert backend.webhook.calls == []


@pytest.mark.anyio
async def test_webhook_failure_is_500_naming_only_the_exception_class(backend):
    backend.webhook.error = MessageBearingError("projects/vk-linkedin detail for nobody")

    async with driving(backend.app) as client:
        response = await client.post("/webhooks/unipile", json={"event": "message_received"}, headers=KEY)

    assert response.status_code == 500
    assert response.json() == {"ok": False, "error": "MessageBearingError"}
    assert "detail for nobody" not in response.text


@pytest.mark.anyio
async def test_webhook_state_runs_on_the_clock(backend, monkeypatch):
    """The `RuntimeState` the handler receives reads the clock when it writes,
    rather than reusing the request's `now`: a sync requested 30 s after `now`
    is stored at that later time."""
    moments = [NOW]
    monkeypatch.setattr(clock, "utcnow", lambda: moments[0])

    def handler(db, payload, now, *, state):
        moments[0] = NOW + timedelta(seconds=30)
        state.request_sync()
        return {"accepted": True, "reason": "sync_requested"}

    monkeypatch.setattr(jobs, "handle_unipile_webhook", handler)

    async with driving(backend.app) as client:
        await client.post("/webhooks/unipile", json={"event": "message_received"}, headers=KEY)

    stored = backend.db.collection("runtime_state").document("linkedin").get().to_dict()
    assert stored["sync_requested_at"] == NOW + timedelta(seconds=30)


# =============================================================================
# POST /jobs/run/{job_id} -- where a Cloud Task delivers a job (MCP v2)
# =============================================================================


@pytest.mark.anyio
async def test_the_job_worker_needs_the_key_and_runs_the_job_it_is_given(backend, monkeypatch):
    runs = Recorder({"job_id": "send_intro:20260911T140000000000Z", "ran": True, "status": "succeeded"})
    monkeypatch.setattr(monitor, "run", runs)

    async with driving(backend.app) as client:
        refused = await client.post("/jobs/run/send_intro%3A20260911T140000000000Z")
        response = await client.post("/jobs/run/send_intro%3A20260911T140000000000Z", headers=KEY)

    assert refused.status_code == 401
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    (args, _kwargs), = runs.calls
    assert args == ("send_intro:20260911T140000000000Z", backend.settings)
