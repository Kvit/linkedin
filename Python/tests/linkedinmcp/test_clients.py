"""Tests for `linkedinmcp/clients.py`'s three factories, executed for real --
carried forward from phase 1, which had no test that ran their bodies at all.

`clients.py`'s own docstring is explicit about why none of this may construct
a real client: `unipile_client()` reads `UNIPILE_API_KEY` from the
environment via `UnipileSettings.from_env()`, and no test may depend on the
developer's local `.env`. So every test here monkeypatches the function each
factory delegates to -- `UnipileClient.from_env`, `pipeline.gemini_client`,
`lib.firestore.client` -- with a counting fake that returns a bare
`object()`, then checks the one thing `clients.py`'s docstring calls out
explicitly: a factory must return a NEW object on every call, never a cached
one, because `unipile_client()`'s docstring explains a shared client would
let two concurrent jobs spend each other's send budget. A counter on the
patched function also proves the factory actually delegates, rather than
returning something else without ever calling it.
"""

import lib.firestore
import lib.unipile
import pipeline
from linkedinmcp import clients


def test_unipile_client_returns_a_new_object_on_every_call(monkeypatch):
    calls = []

    def fake_from_env():
        calls.append(1)
        return object()

    monkeypatch.setattr(lib.unipile.UnipileClient, "from_env", fake_from_env)

    first = clients.unipile_client()
    second = clients.unipile_client()

    assert first is not second
    assert len(calls) == 2


def test_gemini_client_returns_a_new_object_on_every_call(monkeypatch):
    calls = []

    def fake_gemini_client():
        calls.append(1)
        return object()

    monkeypatch.setattr(pipeline, "gemini_client", fake_gemini_client)

    first = clients.gemini_client()
    second = clients.gemini_client()

    assert first is not second
    assert len(calls) == 2


def test_firestore_client_returns_a_new_object_on_every_call(monkeypatch):
    calls = []

    def fake_client():
        calls.append(1)
        return object()

    monkeypatch.setattr(lib.firestore, "client", fake_client)

    first = clients.firestore_client()
    second = clients.firestore_client()

    assert first is not second
    assert len(calls) == 2
