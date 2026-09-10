"""The ASGI app: what is authenticated, what is open, and what shape the URL is.

**This module imports `app` at the top of the file with no `OUTREACH_*`
variable set and `Python/` as the working directory. Collection succeeding is
itself an assertion** -- it is what proves nothing in `app.py` loads settings
at import time. That is why `create_app` is a factory and there is no
module-level `app` object, and it is why the `load_dotenv` inside it names
`.env` explicitly: a bare `load_dotenv()` calls `find_dotenv()`, which walks up
the tree and would load the notebooks' `Python/.env` into the service. Anyone
tempted to "simplify" this back to `app = create_app()` should note that the
whole module would then error out during collection.

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

import httpx
import pytest
from mcp.types import LATEST_PROTOCOL_VERSION

from linkedinmcp import app
from linkedinmcp.http_auth import ApiKeyMiddleware
from linkedinmcp.settings import OutreachSettings

API_KEY = "test-key-not-a-real-secret"

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
