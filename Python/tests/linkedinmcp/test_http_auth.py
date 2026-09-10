"""`ApiKeyMiddleware` and `require_api_key` share one comparison helper.

Driven through a real ASGI stack -- `httpx.ASGITransport` wrapping the actual
middleware around a tiny echo app -- rather than by calling internals
directly, so these tests catch a broken wire-up (wrong header read, wrong
status, wrong scope check) as readily as a broken comparison.

No `pytest-asyncio` is installed (nor may one be added), so each async
request is driven with a bare `asyncio.run(...)` inside an ordinary sync test
function instead of an `async def test_...` -- stdlib only.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from linkedinmcp import settings as cfg
from linkedinmcp.http_auth import ApiKeyMiddleware, require_api_key

API_KEY = "test-secret-key-999"


async def _echo_app(scope, receive, send):
    """A two-line HTTP echo, plus a lifespan branch -- `httpx.ASGITransport`
    never sends a `lifespan` scope, so the one test that needs it drives this
    app directly instead; it still has to survive that scope arriving.
    """
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _wrapped_app():
    return ApiKeyMiddleware(_echo_app, api_key=API_KEY)


def _run(coro):
    return asyncio.run(coro)


async def _send(headers) -> httpx.Response:
    """Send one GET through the real middleware-wrapped app.

    Takes ``headers`` in whatever form `httpx.Request` accepts, including a
    list of raw `(bytes, bytes)` tuples -- needed for the non-ASCII test,
    since a plain `dict[str, str]` would force httpx to encode the value as
    ASCII on the way out, testing httpx instead of the middleware.
    """
    transport = httpx.ASGITransport(app=_wrapped_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        request = httpx.Request("GET", "http://test/", headers=headers)
        return await client.send(request)


def test_no_key_is_rejected():
    """Fails if the "no header present" branch of `_is_authorized` (or its
    caller) ever defaulted to authorized instead of denying."""
    response = _run(_send(headers=[]))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_wrong_key_is_rejected():
    """Fails if the comparison ever degraded to "a key was present" instead
    of "the presented key matches the configured one"."""
    response = _run(_send(headers=[(b"x-api-key", b"wrong-key")]))

    assert response.status_code == 401


def test_correct_key_as_x_api_key_header_is_accepted():
    """Fails if the `x-api-key` header form were dropped or misspelled."""
    response = _run(_send(headers=[(b"x-api-key", API_KEY.encode())]))

    assert response.status_code == 200


def test_correct_key_as_authorization_bearer_is_accepted():
    """Fails if the `Authorization: Bearer` header form were dropped, or if
    the scheme/token split (`partition(b" ")`) were done wrong."""
    response = _run(
        _send(headers=[(b"authorization", f"Bearer {API_KEY}".encode())])
    )

    assert response.status_code == 200


def test_authorization_bearer_scheme_is_case_insensitive():
    """Fails if the scheme comparison ever became case-sensitive (e.g. a
    plain `== b"Bearer"` instead of a `.lower()` comparison)."""
    response = _run(
        _send(headers=[(b"authorization", f"bearer {API_KEY}".encode())])
    )

    assert response.status_code == 200


def test_authorization_basic_scheme_is_rejected():
    """Fails if any scheme were accepted rather than exactly `Bearer` -- the
    only other form the Claude connector platform can send without
    additional Anthropic review is `x-api-key`, tested separately."""
    response = _run(
        _send(headers=[(b"authorization", f"Basic {API_KEY}".encode())])
    )

    assert response.status_code == 401


def test_non_ascii_key_is_rejected_not_a_server_error():
    """Fails if the comparison ever decoded a header to `str` before
    comparing -- `hmac.compare_digest` raises `TypeError` on a `str`
    containing non-ASCII, which would surface here as a 500, not a 401. A
    header is attacker-controlled, so this has to hold for arbitrary bytes.
    """
    response = _run(_send(headers=[(b"x-api-key", b"bad-\xff\xfe-key")]))

    assert response.status_code == 401


def test_lifespan_scope_passes_through_untouched():
    """Fails if the `scope["type"] != "http"` guard were ever removed or
    narrowed -- a middleware that swallows `lifespan` breaks app startup in a
    way that is very confusing to diagnose, since the symptom shows up as a
    hang or a missing startup event far from this file.
    """
    app = _wrapped_app()
    sent = []
    inbound = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def receive():
        return inbound.pop(0)

    async def send(message):
        sent.append(message)

    async def drive():
        await asyncio.wait_for(app({"type": "lifespan"}, receive, send), timeout=2)

    try:
        _run(drive())
    except asyncio.TimeoutError:
        pytest.fail("lifespan never completed -- the middleware is hanging it")

    assert {"type": "lifespan.startup.complete"} in sent
    assert {"type": "lifespan.shutdown.complete"} in sent


def test_401_body_never_echoes_either_key():
    """Fails if `_unauthorized` (or whatever calls it) ever interpolated the
    presented or the expected key into the response body or headers."""
    response = _run(_send(headers=[(b"x-api-key", b"wrong-key-xyz")]))

    assert response.status_code == 401
    exposed = response.text + str(response.headers)
    assert API_KEY not in exposed
    assert "wrong-key-xyz" not in exposed


def test_require_api_key_dependency_reads_settings_at_request_time(monkeypatch):
    """The FastAPI-dependency half of the same check, for `/jobs/*` and
    `/webhooks/unipile` routes that sit outside the mounted MCP app and so
    never see `ApiKeyMiddleware`.

    Fails if `http_auth.py` ever imported the name (`from linkedinmcp.settings
    import get_settings`) instead of the module: that binds a local
    reference the `monkeypatch.setattr(cfg, "get_settings", ...)` below
    cannot reach, so the dependency would fall through to the real
    environment -- almost certainly missing `OUTREACH_API_KEY` in a test
    process -- and raise `ConfigError` instead of resolving the fake key.
    """
    fake_settings = SimpleNamespace(api_key=SecretStr("dep-secret-key-not-a-real"))
    monkeypatch.setattr(cfg, "get_settings", lambda: fake_settings)

    app = FastAPI()

    @app.get("/jobs/ping")
    def ping(_: None = Depends(require_api_key)):
        return {"ok": True}

    client = TestClient(app)

    assert client.get("/jobs/ping").status_code == 401
    assert client.get(
        "/jobs/ping", headers={"x-api-key": "dep-secret-key-not-a-real"}
    ).status_code == 200
    assert client.get(
        "/jobs/ping", headers={"x-api-key": "wrong"}
    ).status_code == 401


def test_an_empty_expected_key_authenticates_nobody():
    """`hmac.compare_digest(b"", b"")` is True, so a middleware built with an
    empty key would let an empty `x-api-key` header through. `OutreachSettings`
    puts a length floor on the key, but this middleware takes its key as a plain
    argument and some later caller may not go through settings at all."""
    app = ApiKeyMiddleware(_echo_app, api_key="")

    async def _probe():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            return (
                await client.get("/", headers={"x-api-key": ""}),
                await client.get("/"),
            )

    empty_header, no_header = asyncio.run(_probe())

    assert empty_header.status_code == 401
    assert no_header.status_code == 401
