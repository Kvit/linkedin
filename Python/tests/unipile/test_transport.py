"""Transport rules.

The single most important behaviour in this library: reads retry, writes never.
POST /users/invite and POST /chats are not idempotent — retrying one after a
timeout sends a second real invitation or message to a real person.
"""

import httpx
import pytest
import respx

from lib.unipile.errors import (
    AccountRestricted,
    CircuitOpen,
    RateLimited,
    ServerError,
)
from lib.unipile.transport import Transport, encode_form

BASE = "https://api.test"


def build(slept=None, **kwargs):
    client = httpx.Client(base_url=BASE)
    sleep = slept.append if slept is not None else (lambda _seconds: None)
    return Transport(client, sleep=sleep, **kwargs)


@respx.mock
def test_write_is_never_retried_on_server_error():
    route = respx.post(f"{BASE}/api/v1/users/invite").mock(
        return_value=httpx.Response(500, json={"status": 500, "type": "errors/provider_error", "title": "boom"})
    )

    with pytest.raises(ServerError):
        build().post_json("/api/v1/users/invite", json={"provider_id": "x"})

    assert route.call_count == 1, "a retried write would double-send a real invitation"


@respx.mock
def test_read_retries_server_errors_then_succeeds():
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        side_effect=[
            httpx.Response(503, json={"status": 503, "type": "errors/network_down", "title": "down"}),
            httpx.Response(500, json={"status": 500, "type": "errors/provider_error", "title": "boom"}),
            httpx.Response(200, json={"object": "AccountList", "items": []}),
        ]
    )

    body = build().get("/api/v1/accounts")

    assert body == {"object": "AccountList", "items": []}
    assert route.call_count == 3


@respx.mock
def test_a_read_outlasts_a_gateway_blip():
    """Unipile's gateway returns 502 for a few seconds at a time.

    A run that has already spent budget must not die because of it, so a read
    keeps trying across a window measured in tens of seconds, not one second.
    """
    slept = []
    boom = {"status": 502, "type": "errors/provider_error", "title": "bad gateway"}
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        side_effect=[
            httpx.Response(502, json=boom),
            httpx.Response(502, json=boom),
            httpx.Response(502, json=boom),
            httpx.Response(200, json={"object": "AccountList", "items": []}),
        ]
    )

    body = build(slept).get("/api/v1/accounts")

    assert body == {"object": "AccountList", "items": []}
    assert route.call_count == 4
    assert sum(slept) > 30, f"gave up waiting after only {sum(slept):.1f}s"
    assert slept == sorted(slept), "each wait should be longer than the last"


@respx.mock
def test_read_gives_up_after_max_attempts():
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(500, json={"status": 500, "type": "errors/provider_error", "title": "boom"})
    )

    with pytest.raises(ServerError):
        build().get("/api/v1/accounts")

    assert route.call_count == 4


@respx.mock
def test_rate_limit_is_never_retried_even_on_a_read():
    """429 is the provider saying stop, not a transient failure."""
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "120"},
            json={"status": 429, "type": "errors/too_many_requests", "title": "slow down"},
        )
    )

    with pytest.raises(RateLimited) as excinfo:
        build().get("/api/v1/accounts")

    assert route.call_count == 1
    assert excinfo.value.retry_after == 120.0


@respx.mock
def test_account_restricted_opens_the_circuit_and_blocks_later_writes():
    respx.post(f"{BASE}/api/v1/users/invite").mock(
        return_value=httpx.Response(403, json={"status": 403, "type": "errors/account_restricted", "title": "restricted"})
    )
    chat_route = respx.post(f"{BASE}/api/v1/chats").mock(return_value=httpx.Response(201, json={}))
    transport = build()

    with pytest.raises(AccountRestricted):
        transport.post_json("/api/v1/users/invite", json={})

    with pytest.raises(CircuitOpen):
        transport.post_form("/api/v1/chats", data={"text": "hi"})

    assert chat_route.call_count == 0, "no further writes may reach a restricted account"


@respx.mock
def test_reads_still_work_after_the_circuit_opens():
    respx.post(f"{BASE}/api/v1/users/invite").mock(
        return_value=httpx.Response(403, json={"status": 403, "type": "errors/account_restricted", "title": "restricted"})
    )
    respx.get(f"{BASE}/api/v1/accounts").mock(return_value=httpx.Response(200, json={"ok": True}))
    transport = build()

    with pytest.raises(AccountRestricted):
        transport.post_json("/api/v1/users/invite", json={})

    assert transport.get("/api/v1/accounts") == {"ok": True}


@respx.mock
def test_api_key_header_is_sent_on_every_request():
    route = respx.get(f"{BASE}/api/v1/accounts").mock(return_value=httpx.Response(200, json={}))
    client = httpx.Client(base_url=BASE, headers={"X-API-KEY": "k-123"})

    Transport(client, sleep=lambda _s: None).get("/api/v1/accounts")

    assert route.calls[0].request.headers["X-API-KEY"] == "k-123"


# --- form encoding, verified against unipile-node-sdk startNewChat -----------

def test_arrays_encode_as_repeated_keys():
    assert encode_form({"attendees_ids": ["ACo1", "ACo2"]}) == [
        ("attendees_ids", "ACo1"),
        ("attendees_ids", "ACo2"),
    ]


def test_nested_objects_encode_with_bracket_notation():
    assert encode_form({"linkedin": {"api": "classic", "inmail": True}}) == [
        ("linkedin[api]", "classic"),
        ("linkedin[inmail]", "true"),
    ]


def test_none_values_are_omitted():
    assert encode_form({"text": "hi", "subject": None}) == [("text", "hi")]


@respx.mock
def test_withdrawal_still_works_after_the_circuit_opens():
    """Withdrawing invitations reduces exposure; blocking it is backwards.

    After a restriction, the operator's most likely next move is pulling back
    pending invitations to calm the account down.
    """
    respx.post(f"{BASE}/api/v1/users/invite").mock(
        return_value=httpx.Response(403, json={"status": 403, "type": "errors/account_restricted", "title": "restricted"})
    )
    cancel = respx.delete(f"{BASE}/api/v1/users/invite/sent/inv-1").mock(
        return_value=httpx.Response(200, json={"object": "InvitationCancelled"})
    )
    transport = build()

    with pytest.raises(AccountRestricted):
        transport.post_json("/api/v1/users/invite", json={})

    assert transport.delete("/api/v1/users/invite/sent/inv-1") == {"object": "InvitationCancelled"}
    assert cancel.call_count == 1
