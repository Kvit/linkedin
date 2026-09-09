"""Resource-layer behaviour: correct routes, correct models, budget enforced.

The ordering tests matter most. Every write must run check -> throttle -> send
-> record, in that order: checking after sending cannot refuse, and recording
before sending charges the budget for calls that failed.
"""

import re
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
import respx

from lib.unipile.budget import SendBudget
from lib.unipile.pacing import HumanCadence
from lib.unipile.errors import BudgetExhausted, ProfileIncomplete, ThrottleLockout
from lib.unipile.models import Attendee, Chat, Message, Profile, Relation
from lib.unipile.resources.accounts import AccountsResource
from lib.unipile.resources.messaging import MessagingResource
from lib.unipile.resources.search import SearchResource
from lib.unipile.resources.users import UsersResource
from lib.unipile.transport import Transport

BASE = "https://api.test"
ACCOUNT = "ACC1"


class RecordingBudget(SendBudget):
    """A real budget that also records the order of its calls."""

    def __init__(self, limits=None, **kwargs):
        super().__init__(
            account_id=ACCOUNT,
            limits=limits or {"invite": 5, "message": 5, "profile": 5},
            cadence=HumanCadence(
                0.0, 0.0, long_pause_every=0, long_pause_min=0.0, long_pause_max=0.0
            ),
            usage_warn_pct=75.0,
            usage_halt_pct=90.0,
            **kwargs,
        )
        self.calls: list[str] = []

    def check(self, kind):
        self.calls.append(f"check:{kind}")
        super().check(kind)

    def throttle(self):
        self.calls.append("throttle")
        super().throttle()

    def record(self, kind, count=1):
        self.calls.append(f"record:{kind}")
        super().record(kind, count)

    def back_off(self):
        self.calls.append("back_off")
        super().back_off()

    def recovered(self):
        self.calls.append("recovered")
        super().recovered()


@pytest.fixture
def budget(tmp_path):
    return RecordingBudget()


@pytest.fixture
def roomy_budget(tmp_path):
    """Enough daily budget that the lockout, not the cap, is what stops a run."""
    return RecordingBudget(limits={"invite": 5, "message": 5, "profile": 50})


@pytest.fixture
def transport():
    return Transport(httpx.Client(base_url=BASE), sleep=lambda _s: None)


@pytest.fixture
def users(transport, budget):
    return UsersResource(
        transport,
        account_id=lambda: ACCOUNT,
        budget=budget,
        throttle_retries=2,
        max_consecutive_throttled=5,
    )


@pytest.fixture
def messaging(transport, budget):
    return MessagingResource(transport, account_id=lambda: ACCOUNT, budget=budget)


# --- accounts ----------------------------------------------------------------


@respx.mock
def test_accounts_list_returns_models(transport):
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": ACCOUNT, "name": "V"}], "cursor": None})
    )

    accounts = AccountsResource(transport).list()

    assert [a.id for a in accounts] == [ACCOUNT]


# --- profiles ----------------------------------------------------------------


@respx.mock
def test_get_profile_requests_the_configured_sections(users, profile_body):
    route = respx.get(f"{BASE}/api/v1/users/khvatkov").mock(
        return_value=httpx.Response(200, json=profile_body)
    )

    profile = users.get_profile("khvatkov", sections=["about", "experience"])

    assert isinstance(profile, Profile)
    params = route.calls[0].request.url.params
    assert params.get_list("linkedin_sections") == ["about", "experience"]
    assert params["account_id"] == ACCOUNT


@respx.mock
def test_get_profile_does_not_notify_the_viewee_by_default(users, profile_body):
    route = respx.get(f"{BASE}/api/v1/users/khvatkov").mock(
        return_value=httpx.Response(200, json=profile_body)
    )

    users.get_profile("khvatkov")

    assert route.calls[0].request.url.params["notify"] == "false"


@respx.mock
def test_get_profile_charges_the_profile_budget_before_the_call(users, budget, profile_body):
    respx.get(f"{BASE}/api/v1/users/khvatkov").mock(
        return_value=httpx.Response(200, json=profile_body)
    )

    users.get_profile("khvatkov")

    assert budget.calls == ["check:profile",
                            "throttle", "record:profile", "recovered"]


@respx.mock
def test_a_throttled_profile_is_still_recorded(transport, budget, throttled_profile_body):
    """LinkedIn counted the fetch even though it withheld the sections.

    Retries are off here so this measures one fetch; `test_retries_are_charged_
    to_the_budget` covers what a retried fetch costs.
    """
    respx.get(f"{BASE}/api/v1/users/x").mock(
        return_value=httpx.Response(200, json=throttled_profile_body)
    )
    users = UsersResource(
        transport,
        account_id=lambda: ACCOUNT,
        budget=budget,
        throttle_retries=0,
        max_consecutive_throttled=5,
    )

    profile = users.get_profile("x")

    assert profile.is_complete is False
    assert budget.used("profile") == 1


@respx.mock
def test_a_throttled_profile_is_retried_after_backing_off(
    users, budget, throttled_profile_body, profile_body
):
    """Withheld sections mean LinkedIn is throttling: wait longer, then ask again."""
    route = respx.get(f"{BASE}/api/v1/users/x").mock(
        side_effect=[
            httpx.Response(200, json=throttled_profile_body),
            httpx.Response(200, json=profile_body),
        ]
    )

    profile = users.get_profile("x")

    assert route.call_count == 2
    assert profile.is_complete
    assert budget.calls == [
        "check:profile", "throttle", "record:profile", "back_off",
        "check:profile", "throttle", "record:profile", "recovered",
    ]


@respx.mock
def test_throttling_is_announced_with_what_was_withheld(users, caplog, throttled_profile_body):
    """The operator needs to know the run is waiting, and on whose behalf."""
    respx.get(f"{BASE}/api/v1/users/x").mock(
        return_value=httpx.Response(200, json=throttled_profile_body)
    )

    with caplog.at_level("WARNING", logger="lib.unipile.resources.users"):
        users.get_profile("x")

    assert "x" in caplog.text
    assert "skills" in caplog.text
    assert "throttl" in caplog.text.lower()


@respx.mock
def test_recovering_after_a_retry_is_announced(users, caplog, throttled_profile_body, profile_body):
    respx.get(f"{BASE}/api/v1/users/x").mock(
        side_effect=[
            httpx.Response(200, json=throttled_profile_body),
            httpx.Response(200, json=profile_body),
        ]
    )

    with caplog.at_level("WARNING", logger="lib.unipile.resources.users"):
        users.get_profile("x")

    assert "recovered" in caplog.text.lower() or "returned" in caplog.text.lower()


@respx.mock
def test_every_throttled_attempt_backs_off_again(users, budget, throttled_profile_body):
    """Still throttled after the pause: pause longer, up to the retry limit."""
    route = respx.get(f"{BASE}/api/v1/users/x").mock(
        return_value=httpx.Response(200, json=throttled_profile_body)
    )

    users.get_profile("x")

    assert route.call_count == 3, "one attempt plus the two configured retries"
    assert budget.calls.count("back_off") == 3
    assert "recovered" not in budget.calls


@respx.mock
def test_retries_are_charged_to_the_budget(users, budget, throttled_profile_body):
    """LinkedIn counted every one of those fetches, so the budget must too."""
    respx.get(f"{BASE}/api/v1/users/x").mock(
        return_value=httpx.Response(200, json=throttled_profile_body)
    )

    users.get_profile("x")

    assert budget.used("profile") == 3


@respx.mock
def test_retrying_stops_when_the_budget_runs_out(users, budget, throttled_profile_body):
    route = respx.get(f"{BASE}/api/v1/users/x").mock(
        return_value=httpx.Response(200, json=throttled_profile_body)
    )
    # limit is 5, so only one attempt is affordable
    budget.record("profile", 4)

    with pytest.raises(BudgetExhausted):
        users.get_profile("x")

    assert route.call_count == 1


@respx.mock
def test_a_complete_profile_is_not_retried(users, budget, profile_body):
    route = respx.get(f"{BASE}/api/v1/users/khvatkov").mock(
        return_value=httpx.Response(200, json=profile_body)
    )

    users.get_profile("khvatkov")

    assert route.call_count == 1
    assert "back_off" not in budget.calls


@respx.mock
def test_retries_can_be_switched_off(transport, budget, throttled_profile_body):
    route = respx.get(f"{BASE}/api/v1/users/x").mock(
        return_value=httpx.Response(200, json=throttled_profile_body)
    )
    users = UsersResource(
        transport,
        account_id=lambda: ACCOUNT,
        budget=budget,
        throttle_retries=0,
        max_consecutive_throttled=5,
    )

    users.get_profile("x")

    assert route.call_count == 1


@respx.mock
def test_require_complete_rejects_a_throttled_profile(users, throttled_profile_body):
    route = respx.get(f"{BASE}/api/v1/users/x").mock(
        return_value=httpx.Response(200, json=throttled_profile_body)
    )

    with pytest.raises(ProfileIncomplete) as excinfo:
        users.get_profile("x", require_complete=True)

    assert "skills" in str(excinfo.value)
    assert route.call_count == 3, "refusal comes only after the retries are spent"


@respx.mock
def test_sustained_throttling_stops_the_whole_run(
    transport, roomy_budget, throttled_profile_body
):
    """Retries bound one slug; only the lockout bounds the run.

    Without it a throttled account keeps fetching at the widened pace until the
    daily budget is gone, storing nothing.
    """
    route = respx.get(f"{BASE}/api/v1/users/x").mock(
        return_value=httpx.Response(200, json=throttled_profile_body)
    )
    users = UsersResource(
        transport,
        account_id=lambda: ACCOUNT,
        budget=roomy_budget,
        throttle_retries=0,
        max_consecutive_throttled=3,
    )

    users.get_profile("x")
    users.get_profile("x")
    with pytest.raises(ThrottleLockout):
        users.get_profile("x")

    assert route.call_count == 3
    assert roomy_budget.remaining(
        "profile") == 47, "the rest of the day is preserved"


@respx.mock
def test_one_complete_profile_resets_the_lockout_count(
    transport, roomy_budget, throttled_profile_body, profile_body
):
    """Scattered withheld profiles are normal; only an unbroken run means lockout."""
    respx.get(f"{BASE}/api/v1/users/x").mock(
        side_effect=[
            httpx.Response(200, json=throttled_profile_body),
            httpx.Response(200, json=throttled_profile_body),
            httpx.Response(200, json=profile_body),
            httpx.Response(200, json=throttled_profile_body),
            httpx.Response(200, json=throttled_profile_body),
        ]
    )
    users = UsersResource(
        transport,
        account_id=lambda: ACCOUNT,
        budget=roomy_budget,
        throttle_retries=0,
        max_consecutive_throttled=3,
    )

    for _ in range(5):
        users.get_profile("x")

    assert users.consecutive_throttled == 2


@respx.mock
def test_the_lockout_can_be_switched_off(transport, roomy_budget, throttled_profile_body):
    respx.get(f"{BASE}/api/v1/users/x").mock(
        return_value=httpx.Response(200, json=throttled_profile_body)
    )
    users = UsersResource(
        transport,
        account_id=lambda: ACCOUNT,
        budget=roomy_budget,
        throttle_retries=0,
        max_consecutive_throttled=0,
    )

    for _ in range(6):
        users.get_profile("x")

    assert users.consecutive_throttled == 6


@respx.mock
def test_exhausted_profile_budget_prevents_the_request(users, budget, profile_body):
    route = respx.get(f"{BASE}/api/v1/users/khvatkov").mock(
        return_value=httpx.Response(200, json=profile_body)
    )
    budget.record("profile", 5)

    with pytest.raises(BudgetExhausted):
        users.get_profile("khvatkov")

    assert route.call_count == 0


# --- relations & invitations --------------------------------------------------


@respx.mock
def test_iter_relations_walks_every_page(users, relations_body):
    second = {"items": [
        dict(relations_body["items"][0], public_identifier="second")], "cursor": None}
    respx.get(f"{BASE}/api/v1/users/relations").mock(
        side_effect=[httpx.Response(
            200, json=relations_body), httpx.Response(200, json=second)]
    )

    relations = list(users.iter_relations())

    assert [r.public_identifier for r in relations] == [
        "danareyesrn", "second"]
    assert isinstance(relations[0], Relation)


@respx.mock
def test_iter_invitations_sent_exposes_the_firestore_keys(users, invitations_sent_body):
    respx.get(f"{BASE}/api/v1/users/invite/sent").mock(
        return_value=httpx.Response(200, json=invitations_sent_body)
    )

    assert [i.public_identifier for i in users.iter_invitations_sent()] == [
        "alex-morgan-4070701b"
    ]


def _sent_invitations(*stamps):
    """An invitations page carrying the given `parsed_datetime` values."""
    return {
        "items": [
            {"object": "InvitationSent", "id": f"inv-{i}", "parsed_datetime": stamp}
            for i, stamp in enumerate(stamps)
        ],
        "cursor": None,
    }


@respx.mock
def test_count_invitations_since_counts_only_inside_the_window(users):
    """The rolling-24h budget count: older invitations must not be charged."""
    respx.get(f"{BASE}/api/v1/users/invite/sent").mock(
        return_value=httpx.Response(200, json=_sent_invitations(
            "2026-09-09T12:00:00.000Z",  # today
            "2026-09-02T10:00:00.000Z",  # a week back
            None,                        # undated
        ))
    )

    cutoff = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)

    assert users.count_invitations_since(cutoff) == 1


@respx.mock
def test_count_invitations_on_the_day_boundary_are_counted_not_coin_flipped(users):
    """`parsed_datetime` is a relative string ("Sent 1 day ago") resolved
    against the clock at request time, so yesterday's invitations land exactly
    on the 24h boundary. Comparing them to an exact cutoff makes the count
    depend on microseconds; the day of tolerance has to include them."""
    respx.get(f"{BASE}/api/v1/users/invite/sent").mock(
        return_value=httpx.Response(200, json=_sent_invitations(
            "2026-09-09T12:00:00.000Z",           # "Sent today"    -> now
            "2026-09-08T12:00:00.000Z",           # "Sent 1 day ago" -> now - 1d
            "2026-09-08T11:59:59.999Z",           # the same, a hair earlier
            "2026-09-03T12:00:00.000Z",           # "Sent 6 days ago"
        ))
    )

    cutoff = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    assert users.count_invitations_since(cutoff) == 3


@respx.mock
def test_count_invitations_since_accepts_a_naive_cutoff_as_utc(users, invitations_sent_body):
    """A naive cutoff must not blow up mid-generator on an aware/naive compare."""
    respx.get(f"{BASE}/api/v1/users/invite/sent").mock(
        return_value=httpx.Response(200, json=invitations_sent_body)
    )

    assert users.count_invitations_since(datetime(2026, 9, 8, 0, 0)) == 1


@respx.mock
def test_send_invitation_posts_json_and_charges_the_budget(users, budget):
    route = respx.post(f"{BASE}/api/v1/users/invite").mock(
        return_value=httpx.Response(
            200, json={"object": "UserInvitationSent", "invitation_id": "inv-9"})
    )

    result = users.send_invitation("ACoAAA1", message="hello")

    assert result.invitation_id == "inv-9"
    assert budget.calls == ["check:invite", "throttle", "record:invite"]
    import json as _json

    body = _json.loads(route.calls[0].request.content)
    assert body == {"provider_id": "ACoAAA1",
                    "account_id": ACCOUNT, "message": "hello"}


def test_send_invitation_rejects_a_note_over_the_linkedin_limit(users):
    with pytest.raises(ValueError, match="300"):
        users.send_invitation("ACoAAA1", message="x" * 301)


@respx.mock
def test_send_invitation_feeds_the_provider_usage_signal_into_the_budget(users, budget):
    respx.post(f"{BASE}/api/v1/users/invite").mock(
        return_value=httpx.Response(
            200, json={"invitation_id": "inv-9", "usage": 95})
    )

    with pytest.raises(BudgetExhausted):
        users.send_invitation("ACoAAA1")


@respx.mock
def test_cancel_invitation_deletes_by_id(users):
    route = respx.delete(f"{BASE}/api/v1/users/invite/sent/inv-9").mock(
        return_value=httpx.Response(200, json={})
    )

    users.cancel_invitation("inv-9")

    assert route.call_count == 1
    # account_id is required:true, in:query on this route.
    assert route.calls[0].request.url.params["account_id"] == ACCOUNT


# --- messaging ----------------------------------------------------------------


@respx.mock
def test_iter_chats_passes_unread_as_a_string(messaging, chats_body):
    route = respx.get(f"{BASE}/api/v1/chats").mock(
        return_value=httpx.Response(200, json=chats_body)
    )

    chats = list(messaging.iter_chats(unread=True))

    assert isinstance(chats[0], Chat)
    assert route.calls[0].request.url.params["unread"] == "true"


@respx.mock
def test_find_chat_with_matches_on_attendee_provider_id(messaging, chats_body):
    respx.get(
        f"{BASE}/api/v1/chats").mock(return_value=httpx.Response(200, json=chats_body))

    found = messaging.find_chat_with("ACoAAFIXTUREATTENDEE0000000000000000000")

    assert found is not None and found.id == "a7imlprjXGmSmywp0cuzoA"


@respx.mock
def test_find_chat_with_returns_none_when_absent(messaging, chats_body):
    respx.get(
        f"{BASE}/api/v1/chats").mock(return_value=httpx.Response(200, json=chats_body))

    assert messaging.find_chat_with("ACoAA-nobody") is None


def _messages(*specs):
    """A message page: (id, is_sender, timestamp) triples."""
    return {
        "items": [
            {"object": "Message", "id": mid, "is_sender": sender, "timestamp": ts}
            for mid, sender, ts in specs
        ],
        "cursor": None,
    }


@respx.mock
def test_count_messages_sent_since_counts_only_our_own_inside_the_window(
    messaging, chats_body
):
    """The rolling-24h budget recount: replies we received are not sends."""
    respx.get(f"{BASE}/api/v1/chats").mock(
        return_value=httpx.Response(200, json=chats_body))
    respx.get(f"{BASE}/api/v1/chats/a7imlprjXGmSmywp0cuzoA/messages").mock(
        return_value=httpx.Response(200, json=_messages(
            ("ours-inside", 1, "2026-09-04T12:46:04.000Z"),
            ("ours-older", 1, "2026-08-01T09:00:00.000Z"),
            ("theirs-inside", 0, "2026-09-04T12:40:00.000Z"),
            ("ours-undated", 1, None),
        ))
    )

    cutoff = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)

    assert messaging.count_messages_sent_since(cutoff) == 1


@respx.mock
def test_count_messages_sent_since_stops_at_the_first_stale_chat(messaging, chats_body):
    """Chats come back newest first, so a quiet account costs one request."""
    chats = respx.get(f"{BASE}/api/v1/chats").mock(
        return_value=httpx.Response(200, json=chats_body))
    messages = respx.get(
        f"{BASE}/api/v1/chats/a7imlprjXGmSmywp0cuzoA/messages").mock(
        return_value=httpx.Response(200, json=_messages()))

    # Every chat in the fixture last saw activity on 2026-09-04.
    assert messaging.count_messages_sent_since(
        datetime(2026, 9, 5, 0, 0, tzinfo=UTC)) == 0
    assert chats.call_count == 1
    assert messages.call_count == 0


@respx.mock
def test_send_message_posts_multipart_and_charges_the_budget(messaging, budget):
    route = respx.post(f"{BASE}/api/v1/chats/chat-1/messages").mock(
        return_value=httpx.Response(
            201, json={"object": "MessageSent", "message_id": "m-1"})
    )

    result = messaging.send_message("chat-1", "hi there")

    assert result.message_id == "m-1"
    assert budget.calls == ["check:message", "throttle", "record:message"]
    assert b'name="text"' in route.calls[0].request.content
    assert b"hi there" in route.calls[0].request.content


@respx.mock
def test_start_chat_repeats_attendee_ids_and_brackets_linkedin_options(messaging):
    route = respx.post(f"{BASE}/api/v1/chats").mock(
        return_value=httpx.Response(
            201, json={"chat_id": "c-1", "message_id": "m-1"})
    )

    messaging.start_chat(["ACoAA1", "ACoAA2"], "hello", inmail=True)

    body = route.calls[0].request.content
    assert body.count(b'name="attendees_ids"') == 2
    assert b'name="linkedin[inmail]"' in body
    assert b"true" in body


@respx.mock
def test_send_to_reuses_an_existing_chat(messaging, chats_body):
    respx.get(
        f"{BASE}/api/v1/chats").mock(return_value=httpx.Response(200, json=chats_body))
    send = respx.post(f"{BASE}/api/v1/chats/a7imlprjXGmSmywp0cuzoA/messages").mock(
        return_value=httpx.Response(201, json={"message_id": "m-2"})
    )
    start = respx.post(
        f"{BASE}/api/v1/chats").mock(return_value=httpx.Response(201, json={}))

    messaging.send_to("ACoAAFIXTUREATTENDEE0000000000000000000", "hello again")

    assert send.call_count == 1
    assert start.call_count == 0


@respx.mock
def test_send_to_starts_a_new_chat_when_none_exists(messaging, chats_body):
    respx.get(
        f"{BASE}/api/v1/chats").mock(return_value=httpx.Response(200, json=chats_body))
    start = respx.post(f"{BASE}/api/v1/chats").mock(
        return_value=httpx.Response(
            201, json={"chat_id": "c-9", "message_id": "m-9"})
    )

    messaging.send_to("ACoAA-stranger", "first contact")

    assert start.call_count == 1


@respx.mock
def test_mark_read_patches_with_a_json_body(messaging):
    """Chat actions are JSON, not multipart, unlike the send endpoints."""
    route = respx.patch(f"{BASE}/api/v1/chats/chat-1").mock(
        return_value=httpx.Response(200, json={"object": "ChatPatched"})
    )

    messaging.mark_read("chat-1")

    import json as _json

    assert _json.loads(route.calls[0].request.content) == {
        "action": "setReadStatus",
        "value": True,
    }


# --- search -------------------------------------------------------------------


@respx.mock
def test_search_posts_the_config_and_pages_results(transport):
    respx.post(f"{BASE}/api/v1/linkedin/search").mock(
        return_value=httpx.Response(
            200, json={"items": [{"public_identifier": "someone"}], "cursor": None}
        )
    )

    results = list(
        SearchResource(transport, account_id=lambda: ACCOUNT).search(
            {"keywords": "denials"})
    )

    assert [r.public_identifier for r in results] == ["someone"]


@respx.mock
def test_handle_invitation_sends_the_shared_secret_linkedin_requires(users):
    """LinkedIn rejects accept/decline without the shared_secret that came
    alongside the invitation, so the caller passes the invitation itself."""
    from lib.unipile.models import ReceivedInvitation

    route = respx.post(f"{BASE}/api/v1/users/invite/received/inv-3").mock(
        return_value=httpx.Response(
            200, json={"object": "UserInvitationHandled", "status": "ACCEPTED"})
    )
    invitation = ReceivedInvitation.model_validate(
        {"id": "inv-3", "shared_secret": "secret-xyz"}
    )

    users.handle_invitation(invitation, "accept")

    import json as _json

    assert _json.loads(route.calls[0].request.content) == {
        "provider": "LINKEDIN",
        "shared_secret": "secret-xyz",
        "account_id": ACCOUNT,
        "action": "accept",
    }


def test_handle_invitation_without_a_shared_secret_fails_fast(users):
    from lib.unipile.models import ReceivedInvitation

    with pytest.raises(ValueError, match="shared_secret"):
        users.handle_invitation(
            ReceivedInvitation.model_validate({"id": "inv-4"}), "accept")


@respx.mock
def test_search_sends_account_id_as_a_query_parameter_not_in_the_body(transport):
    """Verified live: the API rejects account_id in the JSON body with
    400 invalid_parameters, "path": "/account_id", "Required property"."""
    route = respx.post(f"{BASE}/api/v1/linkedin/search").mock(
        return_value=httpx.Response(200, json={"items": [], "cursor": None})
    )

    list(
        SearchResource(transport, account_id=lambda: ACCOUNT).search(
            {"category": "people", "keywords": "revenue cycle"}, page_size=2
        )
    )

    request = route.calls[0].request
    assert request.url.params["account_id"] == ACCOUNT
    assert request.url.params["limit"] == "2"

    import json as _json

    body = _json.loads(request.content)
    assert body == {"api": "classic",
                    "category": "people", "keywords": "revenue cycle"}


@respx.mock
def test_search_parameters_return_models_like_every_other_iterator(transport):
    respx.get(f"{BASE}/api/v1/linkedin/search/parameters").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"object": "LinkedinSearchParameter",
                        "title": "United States", "id": "103644278"}
                ],
                "paging": {"page_count": 1},
            },
        )
    )

    found = list(
        SearchResource(transport, account_id=lambda: ACCOUNT).iter_search_parameters(
            "LOCATION", "United States"
        )
    )

    assert [(p.id, p.title) for p in found] == [("103644278", "United States")]


@respx.mock
def test_a_none_valued_filter_is_not_sent_as_a_query_parameter(users, relations_body):
    """Guard for the shared listing helper: optional filters stay absent."""
    route = respx.get(f"{BASE}/api/v1/users/relations").mock(
        return_value=httpx.Response(
            200, json={"items": relations_body["items"], "cursor": None})
    )

    list(users.iter_relations(filter=None))

    assert "filter" not in route.calls[0].request.url.params


@respx.mock
def test_unread_is_omitted_when_not_requested(messaging, chats_body):
    route = respx.get(f"{BASE}/api/v1/chats").mock(
        return_value=httpx.Response(
            200, json={"items": chats_body["items"], "cursor": None})
    )

    list(messaging.iter_chats())

    assert "unread" not in route.calls[0].request.url.params


# --- account-wide messaging reads ---------------------------------------------
#
# `before`/`after` are validated by the API against a regex demanding exactly
# three fractional digits and a literal Z. `datetime.isoformat()` produces six
# digits and `+00:00` and is rejected, so these tests assert on the *request
# params* rather than on the parsed result: a live rejection is invisible to a
# test that only checks what came back.

API_TIMESTAMP = re.compile(r"^[1-2]\d{3}-[0-1]\d-[0-3]\dT\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _message_page(*specs, cursor=None):
    """A page of ``/messages``: (id, chat_id, is_sender, timestamp) tuples."""
    return {
        "object": "MessageList",
        "items": [
            {
                "object": "Message",
                "id": mid,
                "chat_id": chat_id,
                "is_sender": sender,
                "timestamp": ts,
            }
            for mid, chat_id, sender, ts in specs
        ],
        "cursor": cursor,
    }


@respx.mock
def test_iter_all_messages_encodes_the_timestamp_the_api_demands(messaging):
    route = respx.get(f"{BASE}/api/v1/messages").mock(
        return_value=httpx.Response(200, json=_message_page())
    )

    list(messaging.iter_all_messages(
        after=datetime(2026, 9, 4, 12, 46, 4, 123456, tzinfo=UTC)))

    sent = route.calls[0].request.url.params["after"]
    assert sent == "2026-09-04T12:46:04.123Z"
    assert API_TIMESTAMP.fullmatch(sent)


@respx.mock
def test_iter_all_messages_truncates_microseconds_rather_than_rounding(messaging):
    """Rounding up would step `after` past a message and skip it for good."""
    route = respx.get(f"{BASE}/api/v1/messages").mock(
        return_value=httpx.Response(200, json=_message_page())
    )

    list(messaging.iter_all_messages(
        after=datetime(2026, 9, 4, 12, 46, 4, 999999, tzinfo=UTC)))

    assert route.calls[0].request.url.params["after"] == "2026-09-04T12:46:04.999Z"


@respx.mock
def test_iter_all_messages_converts_a_non_utc_datetime(messaging):
    route = respx.get(f"{BASE}/api/v1/messages").mock(
        return_value=httpx.Response(200, json=_message_page())
    )
    berlin = timezone(timedelta(hours=2))

    list(messaging.iter_all_messages(
        after=datetime(2026, 9, 4, 14, 0, 0, 0, tzinfo=berlin)))

    assert route.calls[0].request.url.params["after"] == "2026-09-04T12:00:00.000Z"


@respx.mock
def test_iter_all_messages_treats_a_naive_datetime_as_utc(messaging):
    route = respx.get(f"{BASE}/api/v1/messages").mock(
        return_value=httpx.Response(200, json=_message_page())
    )

    list(messaging.iter_all_messages(after=datetime(2026, 9, 4, 12, 0, 0)))

    assert route.calls[0].request.url.params["after"] == "2026-09-04T12:00:00.000Z"


@respx.mock
def test_iter_all_messages_omits_bounds_that_were_not_given(messaging):
    route = respx.get(f"{BASE}/api/v1/messages").mock(
        return_value=httpx.Response(200, json=_message_page())
    )

    list(messaging.iter_all_messages())

    params = route.calls[0].request.url.params
    assert "before" not in params
    assert "after" not in params
    assert "sender_id" not in params
    assert params["account_id"] == ACCOUNT


@respx.mock
def test_iter_all_messages_sends_both_bounds_to_window_a_backfill(messaging):
    """The backfill terminates by asking for a window, not by paging to empty."""
    route = respx.get(f"{BASE}/api/v1/messages").mock(
        return_value=httpx.Response(200, json=_message_page())
    )

    list(messaging.iter_all_messages(
        before=datetime(2026, 9, 4, 0, 0, tzinfo=UTC),
        after=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
    ))

    params = route.calls[0].request.url.params
    assert params["before"] == "2026-09-04T00:00:00.000Z"
    assert params["after"] == "2024-01-01T00:00:00.000Z"


@respx.mock
def test_iter_all_messages_walks_every_page(messaging):
    route = respx.get(f"{BASE}/api/v1/messages").mock(
        side_effect=[
            httpx.Response(200, json=_message_page(
                ("m2", "c1", 1, "2026-09-04T12:00:00.000Z"), cursor="CUR")),
            httpx.Response(200, json=_message_page(
                ("m1", "c1", 0, "2026-09-03T12:00:00.000Z"))),
        ]
    )

    messages = list(messaging.iter_all_messages(page_size=250))

    assert [m.id for m in messages] == ["m2", "m1"]
    assert isinstance(messages[0], Message)
    assert route.calls[1].request.url.params["cursor"] == "CUR"
    assert route.calls[1].request.url.params["limit"] == "250"


@respx.mock
def test_iter_all_messages_is_not_charged_to_the_budget(messaging, budget):
    """Reads here are unbudgeted; a sync must not consume the daily send quota."""
    respx.get(f"{BASE}/api/v1/messages").mock(
        return_value=httpx.Response(200, json=_message_page(
            ("m1", "c1", 1, "2026-09-04T12:00:00.000Z")))
    )

    list(messaging.iter_all_messages())

    assert budget.calls == []


@respx.mock
def test_iter_all_attendees_uses_the_account_wide_route(messaging):
    """Not ``/chats/{id}/attendees`` -- that would be one request per chat."""
    route = respx.get(f"{BASE}/api/v1/chat_attendees").mock(
        return_value=httpx.Response(200, json={
            "object": "ChatAttendeeList",
            "items": [{
                "object": "ChatAttendee",
                "id": "att1",
                "provider_id": "ACoAA-someone",
                "name": "Ray Osborne",
                "is_self": 0,
                "specifics": {"provider": "LINKEDIN",
                              "member_urn": "urn:li:member:25323083"},
            }],
            "cursor": None,
        })
    )

    attendees = list(messaging.iter_all_attendees())

    assert route.calls[0].request.url.path == "/api/v1/chat_attendees"
    assert isinstance(attendees[0], Attendee)
    assert attendees[0].provider_id == "ACoAA-someone"


def test_attendee_reads_the_member_urn_nested_under_specifics():
    """The join to `extracted` needs the member id, and it arrives nested."""
    attendee = Attendee.model_validate({
        "id": "att1",
        "provider_id": "ACoAA-someone",
        "specifics": {"provider": "LINKEDIN", "member_urn": "urn:li:member:12345"},
    })

    assert attendee.member_urn == "urn:li:member:12345"


def test_message_defaults_attachments_when_the_payload_omits_them():
    """A declared field with a default, not an `extra` -- the sync indexes
    `attachments` on every message, and `extra="allow"` supplies no default."""
    message = Message.model_validate({"object": "Message", "id": "m1"})

    assert message.attachments == []


def test_message_declares_the_fields_the_sync_reads():
    """Typed, not merely tolerated: declared fields are validated and coerced,
    and the sync's contact join depends on `sender_id` being one of them."""
    declared = set(Message.model_fields)

    assert {"sender_id", "sender_attendee_id", "message_type", "account_id",
            "subject", "deleted", "edited", "seen", "hidden", "is_event",
            "attachments"} <= declared
