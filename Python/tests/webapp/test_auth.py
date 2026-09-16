"""`webapp.auth`: the IAP assertion is verified offline here with a key we
sign ourselves, and the middleware is driven as bare ASGI."""

import json
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from google.auth import jwt
from google.auth.crypt import es256

from tests.webapp.conftest import AUDIENCE, ME
from webapp import auth


def _keypair():
    private = ec.generate_private_key(ec.SECP256R1())
    private_pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private_pem, public_pem.decode()


def _token(private_pem, *, email=ME, audience=AUDIENCE, issuer=auth.IAP_ISSUER):
    signer = es256.ES256Signer.from_string(private_pem, key_id="k1")
    now = int(time.time())
    payload = {"iss": issuer, "aud": audience, "email": email, "sub": "1", "iat": now, "exp": now + 300}
    return jwt.encode(signer, payload).decode()


def _certs_request(public_pem):
    """What `google.oauth2.id_token._fetch_certs` calls: `request(url, method="GET")`
    returning `.status` and `.data`."""
    def request(url, method="GET", **_):
        assert url == auth.IAP_CERTS_URL
        return SimpleNamespace(status=200, data=json.dumps({"k1": public_pem}).encode())
    return request


def test_a_valid_assertion_yields_its_email():
    private_pem, public_pem = _keypair()
    email = auth.verify_iap_jwt(_token(private_pem), AUDIENCE, request=_certs_request(public_pem))
    assert email == ME


def test_wrong_audience_issuer_or_garbage_yields_none():
    private_pem, public_pem = _keypair()
    request = _certs_request(public_pem)
    assert auth.verify_iap_jwt(_token(private_pem, audience="/projects/2/x"), AUDIENCE, request=request) is None
    assert auth.verify_iap_jwt(_token(private_pem, issuer="https://evil"), AUDIENCE, request=request) is None
    assert auth.verify_iap_jwt("not.a.jwt", AUDIENCE, request=request) is None


async def _call(middleware, path, headers=(), method="GET"):
    sent = []
    scope = {"type": "http", "method": method, "path": path, "headers": list(headers), "state": {}}

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    return scope, sent


async def _inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.anyio
async def test_health_is_open_and_everything_else_needs_the_assertion():
    middleware = auth.IapMiddleware(_inner, audience=AUDIENCE, allowed_email=ME, dev_user=None)
    _scope, sent = await _call(middleware, "/health")
    assert sent[0]["status"] == 200
    _scope, sent = await _call(middleware, "/contacts")
    assert sent[0]["status"] == 403


@pytest.mark.anyio
async def test_the_assertion_email_must_match(monkeypatch):
    monkeypatch.setattr(auth, "verify_iap_jwt", lambda token, audience, request=None: "other@example.com")
    middleware = auth.IapMiddleware(_inner, audience=AUDIENCE, allowed_email=ME, dev_user=None)
    _scope, sent = await _call(middleware, "/contacts", [(auth.ASSERTION_HEADER, b"t")])
    assert sent[0]["status"] == 403

    monkeypatch.setattr(auth, "verify_iap_jwt", lambda token, audience, request=None: ME)
    scope, sent = await _call(middleware, "/contacts", [(auth.ASSERTION_HEADER, b"t")])
    assert sent[0]["status"] == 200
    assert scope["state"]["user"] == ME


@pytest.mark.anyio
async def test_dev_user_passes_without_a_header():
    middleware = auth.IapMiddleware(_inner, audience=None, allowed_email=ME, dev_user=ME)
    scope, sent = await _call(middleware, "/contacts")
    assert sent[0]["status"] == 200
    assert scope["state"]["user"] == ME


@pytest.mark.anyio
async def test_a_post_another_site_made_is_refused_but_following_a_link_is_not():
    middleware = auth.IapMiddleware(_inner, audience=None, allowed_email=ME, dev_user=ME)
    cross_site = [(b"sec-fetch-site", b"cross-site")]
    _scope, sent = await _call(middleware, "/contacts/ann/field", cross_site, method="POST")
    assert sent[0]["status"] == 403
    _scope, sent = await _call(middleware, "/contacts/ann/field", [(b"sec-fetch-site", b"same-origin")], method="POST")
    assert sent[0]["status"] == 200
    _scope, sent = await _call(middleware, "/contacts/ann", cross_site)
    assert sent[0]["status"] == 200


@pytest.mark.anyio
async def test_lifespan_passes_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    middleware = auth.IapMiddleware(inner, audience=AUDIENCE, allowed_email=ME, dev_user=None)
    await middleware({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]
