"""Read-only smoke tests against the real Unipile account.

Deselected by default; run with `pytest -m live`. These exist to catch drift
between the mocked fixtures and the live API.

No test here may ever call a write endpoint. Sending an invitation or a message
is not something a test suite gets to do on someone's real LinkedIn account, so
this file uses reads exclusively, and the profile it fetches is the account's
own -- viewing your own profile notifies nobody.
"""

import itertools
from datetime import timedelta

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


# --- account-wide message reads, as the sync uses them -------------------------
#
# The mocked tests pin the request the client builds. These pin what the server
# does with it -- which is the half a fixture can never tell you, and the half
# the sync's correctness rests on.


def _newest(client):
    return next(iter(client.messaging.iter_all_messages(page_size=1)), None)


def test_all_messages_returns_the_mailbox_not_one_chat(client):
    messages = list(itertools.islice(client.messaging.iter_all_messages(page_size=100), 100))

    assert len(messages) > 1
    assert len({m.chat_id for m in messages}) > 1, "expected several chats in one page"
    assert all(m.id and m.timestamp for m in messages)


def test_all_messages_come_back_newest_first(client):
    """The sync's forward pass assumes this ordering; a change would silently
    strand messages rather than fail loudly."""
    stamps = [m.timestamp for m in
              itertools.islice(client.messaging.iter_all_messages(page_size=100), 250)]

    assert stamps == sorted(stamps, reverse=True)


def test_the_after_bound_is_exclusive_and_the_format_is_accepted(client):
    """The discriminating test.

    It pins three things at once: that the timestamp encoding is accepted at
    all, that `after` is exclusive, and therefore that the sync's one-millisecond
    nudge is necessary. Asserting merely that items came back would pass against
    a server that ignored the parameter entirely.
    """
    newest = _newest(client)
    assert newest is not None

    excluded = list(itertools.islice(
        client.messaging.iter_all_messages(after=newest.timestamp, page_size=10), 10))
    included = list(itertools.islice(
        client.messaging.iter_all_messages(
            after=newest.timestamp - timedelta(milliseconds=1), page_size=10), 10))

    assert newest.id not in {m.id for m in excluded}, "after is inclusive; nudge is wrong"
    assert newest.id in {m.id for m in included}


def test_the_before_bound_is_exclusive_too(client):
    newest = _newest(client)
    assert newest is not None

    excluded = list(itertools.islice(
        client.messaging.iter_all_messages(before=newest.timestamp, page_size=10), 10))

    assert newest.id not in {m.id for m in excluded}


def test_before_and_after_together_bound_a_window(client):
    """The backfill terminates by running out of window rather than by paging to
    exhaustion, so a server that honoured only one bound would loop forever."""
    newest = _newest(client)
    assert newest is not None
    floor = newest.timestamp - timedelta(days=30)

    windowed = list(itertools.islice(
        client.messaging.iter_all_messages(
            before=newest.timestamp, after=floor, page_size=100), 500))

    assert windowed, "expected some messages in the last 30 days"
    assert all(floor < m.timestamp < newest.timestamp for m in windowed)


def test_a_bound_survives_pagination(client):
    """Cursor and filter have to compose: page two must still respect `after`."""
    newest = _newest(client)
    assert newest is not None
    floor = newest.timestamp - timedelta(days=365)

    page_size = 100
    walked = list(itertools.islice(
        client.messaging.iter_all_messages(after=floor, page_size=page_size),
        page_size * 2 + 10))

    assert len(walked) > page_size, "need more than one page to test this"
    assert all(m.timestamp > floor for m in walked)


def test_chat_attendees_carry_the_ids_the_contact_join_needs(client):
    attendees = list(itertools.islice(
        client.messaging.iter_all_attendees(page_size=100), 100))

    assert attendees
    assert all(a.provider_id for a in attendees), "provider_id is the primary join key"
    assert sum(1 for a in attendees if a.member_urn) > len(attendees) * 0.8


def test_a_single_chat_carries_the_attendee_provider_id(client):
    """Gates the sync's per-chat fast path: with this, a small delta resolves its
    contacts one chat at a time instead of listing every conversation."""
    first = next(iter(client.messaging.iter_chats(page_size=1)), None)
    assert first is not None

    fetched = client.messaging.get_chat(first.id)

    assert fetched.id == first.id
    assert fetched.attendee_provider_id
