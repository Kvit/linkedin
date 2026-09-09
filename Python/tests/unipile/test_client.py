"""The UnipileClient facade: wiring, account resolution, lifecycle."""

import httpx
import pytest
import respx

from lib.unipile import UnipileClient
from lib.unipile.config import UnipileSettings

BASE_HOST = "api62.unipile.com:19262"
BASE = f"https://{BASE_HOST}"

ACCOUNTS_BODY = {
    "object": "AccountList",
    "items": [{"id": "ACC-RESOLVED", "name": "Vitali", "type": "LINKEDIN"}],
    "cursor": None,
}


def settings(**overrides) -> UnipileSettings:
    return UnipileSettings(
        _env_file=None,
        api_key="k-123",
        dns=BASE_HOST,
        **overrides,
    )


@respx.mock
def test_requests_carry_the_api_key_and_resolved_base_url(tmp_path):
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=ACCOUNTS_BODY)
    )

    with UnipileClient(settings(budget_state_path=tmp_path / "b.json")) as client:
        client.accounts.list()

    request = route.calls[0].request
    assert request.headers["X-API-KEY"] == "k-123"
    assert str(request.url).startswith(BASE)


@respx.mock
def test_account_id_comes_from_settings_without_a_lookup(tmp_path):
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=ACCOUNTS_BODY)
    )

    with UnipileClient(
        settings(account_id="ACC-FROM-ENV", budget_state_path=tmp_path / "b.json")
    ) as client:
        assert client.account_id == "ACC-FROM-ENV"

    assert route.call_count == 0


@respx.mock
def test_account_id_is_resolved_once_and_cached(tmp_path):
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=ACCOUNTS_BODY)
    )

    with UnipileClient(settings(budget_state_path=tmp_path / "b.json")) as client:
        assert client.account_id == "ACC-RESOLVED"
        assert client.account_id == "ACC-RESOLVED"

    assert route.call_count == 1


@respx.mock
def test_no_connected_account_is_a_clear_error(tmp_path):
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"items": [], "cursor": None})
    )

    from lib.unipile.errors import ConfigError

    with UnipileClient(settings(budget_state_path=tmp_path / "b.json")) as client:
        with pytest.raises(ConfigError):
            _ = client.account_id


@respx.mock
def test_profile_fetches_use_the_configured_default_sections(tmp_path, profile_body):
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=ACCOUNTS_BODY)
    )
    route = respx.get(f"{BASE}/api/v1/users/khvatkov").mock(
        return_value=httpx.Response(200, json=profile_body)
    )

    with UnipileClient(
        settings(
            profile_sections=["about", "experience"],
            budget_state_path=tmp_path / "b.json",
            min_delay_seconds=0,
            max_delay_seconds=0,
        )
    ) as client:
        client.users.get_profile("khvatkov")

    assert route.calls[0].request.url.params.get_list("linkedin_sections") == [
        "about",
        "experience",
    ]


def test_closing_the_client_closes_the_http_connection(tmp_path):
    client = UnipileClient(settings(budget_state_path=tmp_path / "b.json"))

    client.close()

    assert client.http.is_closed


@respx.mock
def test_budget_is_rekeyed_once_the_account_is_resolved(tmp_path, profile_body):
    """Counters must land under the real account, not the placeholder.

    Without this, a tenant that does not set UNIPILE_ACCOUNT_ID would keep every
    counter under "pending" and never reconcile against real send history.
    """
    import json

    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=ACCOUNTS_BODY)
    )
    respx.get(f"{BASE}/api/v1/users/khvatkov").mock(
        return_value=httpx.Response(200, json=profile_body)
    )
    state = tmp_path / "b.json"

    with UnipileClient(
        settings(budget_state_path=state, min_delay_seconds=0, max_delay_seconds=0)
    ) as client:
        client.users.get_profile("khvatkov")

    counters = json.loads(state.read_text())
    by_account = next(iter(counters.values()))
    assert "ACC-RESOLVED" in by_account
    assert "pending" not in by_account


@respx.mock
def test_an_exhausted_budget_blocks_the_first_send_of_a_new_client(tmp_path):
    """The budget must be keyed to the real account before the first check.

    Previously the client started on a "pending" placeholder and only rekeyed
    after resolution, so the first operation of every client instance was
    checked against an empty counter -- one silent over-send per process, which
    for daily scripts and notebook restarts means one per run.
    """
    import json
    from datetime import UTC, datetime

    from lib.unipile.errors import BudgetExhausted

    today = datetime.now(UTC).date().isoformat()
    state = tmp_path / "b.json"
    state.write_text(json.dumps({today: {"ACC-RESOLVED": {"invite": 1}}}))

    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=ACCOUNTS_BODY)
    )
    invite = respx.post(f"{BASE}/api/v1/users/invite").mock(
        return_value=httpx.Response(200, json={"invitation_id": "inv-1"})
    )

    with UnipileClient(
        settings(
            budget_state_path=state,
            max_invites_per_day=1,
            min_delay_seconds=0,
            max_delay_seconds=0,
        )
    ) as client:
        with pytest.raises(BudgetExhausted):
            client.users.send_invitation("ACoAA-target")

    assert invite.call_count == 0, "the cap was already spent; nothing may go out"
