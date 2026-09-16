"""IAP in front, and the app checks the assertion itself too.

Cloud Run's built-in IAP signs in the user and adds `x-goog-iap-jwt-assertion`
to every request. The unsigned `x-goog-authenticated-user-email` header is
never read: it can be forged by anything that reaches the container
without passing IAP. `IapMiddleware` is pure ASGI, the shape of
`linkedinmcp.http_auth.ApiKeyMiddleware`, with one open path: `GET /health`.
`google.oauth2.id_token.verify_token` checks the signature, the expiry and
the audience but not the issuer, so the issuer and the email are checked
here.

`verify_iap_jwt` is called through this module's namespace, so a test can
replace it with `monkeypatch.setattr(auth, "verify_iap_jwt", ...)`.
"""

import logging
import time
from types import SimpleNamespace

import httpx
from google.auth import jwt
from google.oauth2 import id_token
from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = ["ASSERTION_HEADER", "IAP_CERTS_URL", "IAP_ISSUER", "IapMiddleware", "verify_iap_jwt"]

IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
ASSERTION_HEADER = b"x-goog-iap-jwt-assertion"
OPEN_PATH = "/health"
CERTS_TTL_SECONDS = 3600

logger = logging.getLogger(__name__)


class _CertsRequest:
    """`google.auth`'s request protocol -- `request(url, method="GET")`
    returning `.status` and `.data` -- on httpx, caching a successful certs
    body for an hour: `verify_token` fetches the certs on every call, and a
    page making several requests must not make a gstatic request each time."""

    def __init__(self) -> None:
        self._cached: SimpleNamespace | None = None
        self._at = 0.0

    def __call__(self, url: str, method: str = "GET", **_) -> SimpleNamespace:
        if self._cached is not None and time.monotonic() - self._at < CERTS_TTL_SECONDS:
            return self._cached
        response = httpx.get(url, timeout=10)
        fetched = SimpleNamespace(status=response.status_code, data=response.content)
        if response.status_code == 200:
            self._cached, self._at = fetched, time.monotonic()
        return fetched


_certs = _CertsRequest()


def verify_iap_jwt(token: str, audience: str, *, request=None) -> str | None:
    """The `email` claim of a valid IAP assertion for `audience`, or `None`
    for anything else: a bad signature, an expired token, another audience,
    another issuer. Never raises on a bad token. A rejected token's `aud`
    claim is logged (never the token), so a wrong audience form is a
    one-look fix at the first sign-in."""
    try:
        claims = id_token.verify_token(token, request or _certs, audience=audience, certs_url=IAP_CERTS_URL)
    except Exception as error:
        try:
            presented = jwt.decode(token, verify=False).get("aud")
        except Exception:
            presented = None
        logger.warning("IAP assertion rejected: %s (aud=%r, expected %r)", type(error).__name__, presented, audience)
        return None
    if claims.get("iss") != IAP_ISSUER:
        logger.warning("IAP assertion rejected: issuer %r", claims.get("iss"))
        return None
    return claims.get("email") or None


async def _forbidden(send: Send) -> None:
    body = b"Forbidden"
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class IapMiddleware:
    """Every http request but `GET /health` must be `allowed_email`, proven
    by the IAP assertion -- or, for a local run, assumed by `dev_user`."""

    def __init__(self, app: ASGIApp, *, audience: str | None, allowed_email: str, dev_user: str | None) -> None:
        self.app = app
        self._audience = audience
        self._allowed = allowed_email
        self._dev_user = dev_user

    def _email_from(self, headers: list[tuple[bytes, bytes]]) -> str | None:
        if self._dev_user is not None:
            return self._dev_user
        if self._audience is None:
            return None
        for name, value in headers:
            if name.lower() == ASSERTION_HEADER:
                return verify_iap_jwt(value.decode("ascii", "replace"), self._audience)
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # `lifespan` passes straight through; swallowing it breaks startup.
            await self.app(scope, receive, send)
            return
        if scope["path"] == OPEN_PATH:
            await self.app(scope, receive, send)
            return
        email = self._email_from(scope["headers"])
        if email is None or email != self._allowed or _cross_site_write(scope):
            await _forbidden(send)
            return
        scope.setdefault("state", {})["user"] = email
        await self.app(scope, receive, send)


def _cross_site_write(scope: Scope) -> bool:
    """A POST another site's page made the browser send. The IAP sign-in
    cookie travels with it, so the assertion alone would let it change a
    contact. Browsers mark every request with `Sec-Fetch-Site`; only the
    app's own pages (`same-origin`) and a typed address (`none`) may write.
    A request without the header, such as a test client's, is not a browser's."""
    if scope["method"] in ("GET", "HEAD"):
        return False
    site = dict(scope["headers"]).get(b"sec-fetch-site")
    return site is not None and site not in (b"same-origin", b"none")
