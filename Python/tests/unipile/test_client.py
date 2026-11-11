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
    """Client settings with the long break switched off.

    `HumanCadence` draws its breaks at random -- roughly one call in
    `long_pause_every` -- and these tests sleep for real, so leaving it on gives
    any budgeted call a 1-in-10 chance of pausing the suite for two to five
    minutes. Tests about pacing itself inject a fake clock instead.
    """
    return UnipileSettings(
        _env_file=None,
        api_key="k-123",
        dns=BASE_HOST,
        **{"long_pause_every": 0, **overrides},
    )


@respx.mock
def test_requests_carry_the_api_key_and_resolved_base_url():
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=ACCOUNTS_BODY)
    )

    with UnipileClient(settings()) as client:
        client.accounts.list()

    request = route.calls[0].request
    assert request.headers["X-API-KEY"] == "k-123"
    assert str(request.url).startswith(BASE)


@respx.mock
def test_account_id_comes_from_settings_without_a_lookup():
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=ACCOUNTS_BODY)
    )

    with UnipileClient(
        settings(account_id="ACC-FROM-ENV")
    ) as client:
        assert client.account_id == "ACC-FROM-ENV"

    assert route.call_count == 0


@respx.mock
def test_account_id_is_resolved_once_and_cached():
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=ACCOUNTS_BODY)
    )

    with UnipileClient(settings()) as client:
        assert client.account_id == "ACC-RESOLVED"
        assert client.account_id == "ACC-RESOLVED"

    assert route.call_count == 1


@respx.mock
def test_no_connected_account_is_a_clear_error():
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"items": [], "cursor": None})
    )

    from lib.unipile.errors import ConfigError

    with UnipileClient(settings()) as client:
        with pytest.raises(ConfigError):
            _ = client.account_id


@respx.mock
def test_profile_fetches_use_the_configured_default_sections(profile_body):
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=ACCOUNTS_BODY)
    )
    route = respx.get(f"{BASE}/api/v1/users/khvatkov").mock(
        return_value=httpx.Response(200, json=profile_body)
    )

    with UnipileClient(
        settings(
            profile_sections=["about", "experience"],
            min_delay_seconds=0,
            max_delay_seconds=0,
        )
    ) as client:
        client.users.get_profile("khvatkov")

    assert route.calls[0].request.url.params.get_list("linkedin_sections") == [
        "about",
        "experience",
    ]


def test_closing_the_client_closes_the_http_connection():
    client = UnipileClient(settings())

    client.close()

    assert client.http.is_closed


@respx.mock
def test_budget_is_rekeyed_once_the_account_is_resolved(profile_body):
    """Counters must land under the real account, not the placeholder.

    Without this, a tenant that does not set UNIPILE_ACCOUNT_ID would keep every
    counter under "pending" and never reconcile against real send history.
    """
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=ACCOUNTS_BODY)
    )
    respx.get(f"{BASE}/api/v1/users/khvatkov").mock(
        return_value=httpx.Response(200, json=profile_body)
    )
    with UnipileClient(
        settings(min_delay_seconds=0, max_delay_seconds=0)
    ) as client:
        client.users.get_profile("khvatkov")

        # `used` reads the counter for whatever account_id resolves to now. A
        # fetch charged to a "pending" placeholder would leave this at zero.
        assert client.budget.account_id == "ACC-RESOLVED"
        assert client.budget.used("profile") == 1


@respx.mock
def test_an_exhausted_budget_blocks_the_first_send_of_a_new_client():
    """The budget must be keyed to the real account before the first check.

    Previously the client started on a "pending" placeholder and only rekeyed
    after resolution, so the first operation of every client instance was
    checked against an empty counter -- one silent over-send per process, which
    for daily scripts and notebook restarts means one per run.

    The recount that seeds the budget is keyed to the resolved account, so it
    only protects the first send if resolution happens first.
    """
    from lib.unipile.errors import BudgetExhausted

    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=ACCOUNTS_BODY)
    )
    invite = respx.post(f"{BASE}/api/v1/users/invite").mock(
        return_value=httpx.Response(200, json={"invitation_id": "inv-1"})
    )

    with UnipileClient(
        settings(
            max_invites_per_day=1,
            min_delay_seconds=0,
            max_delay_seconds=0,
        )
    ) as client:
        # What Phase A2 does at the start of a run: today's cap is already spent.
        client.budget.reconcile(invite=1)

        with pytest.raises(BudgetExhausted):
            client.users.send_invitation("ACoAA-target")

    assert invite.call_count == 0, "the cap was already spent; nothing may go out"
