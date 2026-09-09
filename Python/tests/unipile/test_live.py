"""Read-only smoke tests against the real Unipile account.

Deselected by default; run with `pytest -m live`. These exist to catch drift
between the mocked fixtures and the live API.

No test here may ever call a write endpoint. Sending an invitation or a message
is not something a test suite gets to do on someone's real LinkedIn account, so
this file uses reads exclusively, and the profile it fetches is the account's
own -- viewing your own profile notifies nobody.
"""

import pytest

from lib.unipile import UnipileClient
from lib.unipile.compat import SUMMARY_KEYS, to_lh_document

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def client():
    live = UnipileClient.from_env()
    live.budget = type(live.budget)(
        account_id=live.settings.account_id or "live",
        limits={"invite": 0, "message": 0, "profile": 25},
        min_delay=0.0,
        max_delay=0.0,
    )
    live.users._budget = live.budget
    live.messaging._budget = live.budget
    with live:
        yield live


def test_the_configured_account_is_connected(client):
    accounts = client.accounts.list()

    assert accounts, "no account connected; check UNIPILE_API_KEY and UNIPILE_DNS"
    assert client.account_id


def test_relations_return_the_firestore_key(client):
    first = next(iter(client.users.iter_relations(page_size=2)), None)

    assert first is not None
    assert first.public_identifier
    assert first.provider_id.startswith("ACo")


def test_chats_expose_the_attendee_provider_id_send_to_relies_on(client):
    first = next(iter(client.messaging.iter_chats(page_size=2)), None)

    assert first is not None
    assert first.id
    assert first.attendee_provider_id


def test_pending_invitations_parse(client):
    for invitation in client.users.iter_invitations_sent(page_size=2):
        assert invitation.id
        assert invitation.public_identifier
        break


def test_own_profile_round_trips_through_the_compat_mapper(client):
    """Full pipeline shape check: fetch -> map -> the keys join_keys reads."""
    profile = client.users.get_profile("khvatkov")

    assert profile.public_identifier == "khvatkov"
    document = to_lh_document(profile)
    assert [key for key in SUMMARY_KEYS if key not in document] == []


def test_default_sections_actually_return_content(client):
    """The default selector must produce data worth classifying.

    With no `linkedin_sections` the API returns no experience, education,
    skills or About text at all, so this guards against a regression that would
    silently feed empty summaries to Gemini.

    Only what `DEFAULT_PROFILE_SECTIONS` actually asks for is asserted. That
    list is deliberately `about,experience` because LinkedIn stalls on a wider
    one, so education and skills are never requested here, and nothing is
    assumed about what LinkedIn returns for a section that was not asked for.
    """
    profile = client.users.get_profile("khvatkov")

    assert profile.work_experience, "no work experience returned"
    assert profile.summary, "no About text returned"
    if not profile.is_complete:
        pytest.skip(f"LinkedIn throttled sections: {profile.throttled_sections}")


def test_classic_search_returns_people(client):
    """Search was the last surface verified only against mocks.

    It shipped broken: account_id was sent in the JSON body and the API rejects
    that with 400 invalid_parameters. Only a live call catches it.
    """
    results = []
    for item in client.search.search({"category": "people", "keywords": "revenue cycle"}, page_size=2):
        results.append(item)
        if len(results) >= 2:
            break

    assert results, "classic search returned nothing"
    assert all(r.public_identifier for r in results)
