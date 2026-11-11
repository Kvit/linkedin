"""Shared API-key authentication for the outreach service.

Two call sites, one comparison:

- :class:`ApiKeyMiddleware` -- pure ASGI, wraps the mounted FastMCP app::

      FastMCP.http_app(middleware=[Middleware(ApiKeyMiddleware, api_key=...)])

  (``Middleware`` is ``starlette.middleware.Middleware``.) Deliberately not
  ``starlette.middleware.base.BaseHTTPMiddleware`` -- that wrapper buffers
  the whole response and does not compose cleanly with a streaming MCP
  transport.

- :func:`require_api_key` -- a FastAPI dependency for the plain
  ``/jobs/*`` and ``/webhooks/unipile`` routes. Those sit outside the mounted
  MCP app, so they never pass through ``ApiKeyMiddleware`` at all and need
  their own guard.

Both funnel through the module-level :func:`_is_authorized`, so the two
checks cannot drift apart.

Exactly two header forms are accepted, and no others: ``x-api-key: <token>``,
and ``Authorization: Bearer <token>`` with the scheme matched
case-insensitively. Those are the only two the Claude connector platform
sends without additional Anthropic review -- a third accepted form would be
dead code that only widens the attack surface.
"""

import hmac

from fastapi import HTTPException, Request
from starlette.types import Receive, Scope, Send

from linkedinmcp import settings as cfg


def _presented_key(headers: list[tuple[bytes, bytes]]) -> bytes | None:
    """Pull the caller's key out of `x-api-key` or `Authorization: Bearer`.

    Returns `None` when neither header is present, or when `Authorization`
    is set to a scheme other than `Bearer`. ASGI servers are expected to
    lower-case header names already, but that is not relied on silently here
    -- both the header name and, for `Authorization`, the scheme are
    `.lower()`-ed explicitly so a reader sees the intent.
    """
    for name, value in headers:
        if name.lower() == b"x-api-key":
            return value
    for name, value in headers:
        if name.lower() == b"authorization":
            scheme, _, token = value.partition(b" ")
            if scheme.lower() == b"bearer":
                return token
    return None


def _is_authorized(headers: list[tuple[bytes, bytes]], expected: bytes) -> bool:
    """The one comparison both `ApiKeyMiddleware` and `require_api_key` call,
    so the two can never drift apart.

    Compares **bytes**, never `str`: `hmac.compare_digest` raises `TypeError`
    on a `str` containing non-ASCII, and a header is attacker-controlled
    input -- a `TypeError` here would surface as a 500, which tells a caller
    their key was interesting.
    """
    presented = _presented_key(headers)
    if not presented or not expected:
        # `hmac.compare_digest(b"", b"")` is True, so an empty expected key
        # would authenticate an empty presented one. `OutreachSettings` puts a
        # length floor on the key so this cannot arise from configuration, and
        # this stays anyway: `ApiKeyMiddleware` takes its key as a plain
        # argument, so some later caller could build one from a source that
        # never passed through settings. Failing open is the one outcome this
        # function must never have.
        return False
    return hmac.compare_digest(presented, expected)


async def _unauthorized(send: Send) -> None:
    """Write a 401 that never echoes the presented or the expected key."""
    body = b'{"detail":"Unauthorized"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class ApiKeyMiddleware:
    """Pure ASGI middleware guarding the mounted MCP app with one fixed key."""

    def __init__(self, app, *, api_key: str) -> None:
        self.app = app
        self._expected = api_key.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # `lifespan` and `websocket` pass straight through untouched.
            # Swallowing `lifespan` here breaks app startup, and that failure
            # is confusing to diagnose from the caller's side.
            await self.app(scope, receive, send)
            return
        if not _is_authorized(scope["headers"], self._expected):
            await _unauthorized(send)
            return
        await self.app(scope, receive, send)


def require_api_key(request: Request) -> None:
    """FastAPI dependency for the plain `/jobs/*` and `/webhooks/unipile`
    routes, which sit outside the mounted MCP app and so are never seen by
    `ApiKeyMiddleware`.

    Reads the expected key by calling `cfg.get_settings()` at request time
    (module import, not name import -- see `linkedinmcp/settings.py`), so a
    test can replace it with `monkeypatch.setattr(cfg, "get_settings", ...)`.
    No settings object is cached at module level here, for the same reason:
    the whole point of resolving at call time is that a test can replace it.
    """
    expected = cfg.get_settings().api_key.get_secret_value().encode()
    if not _is_authorized(request.scope["headers"], expected):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
