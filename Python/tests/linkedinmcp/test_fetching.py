"""Tests for `linkedinmcp.fetching`: when a tick sends nothing, fetch ONE
queued new connection's LinkedIn profile, store it in `extracted`, classify
it and write the classification to `analysis` -- `new-contacts.ipynb`
Phases C-E, one contact per tick.

The outcome table in task 3b's brief, as rulings P3-3 (a withheld profile
goes to the back of the queue) and P3-5 (an unavailable API pauses fetches)
amend it, is walked row by row in "the outcome table": each test asserts the
row's four columns together -- the ledger rows (present only when LinkedIn
charged the fetch), the fetch-queue item, the runtime state, and the
returned summary. Ruling P3-4's `already_stored` case (the profile is in
`extracted` before any LinkedIn call) and minors M2-M4 are in "storing and
classifying"; review finding I1 (a step after a charged fetch fails) has a
section of its own.

No test reaches Gemini: the autouse `gemini` fixture replaces
`clients.gemini_client` and `profiles.classify_profile` for every test in
this module. LinkedIn is the stub client (`fake_unipile.FakeUnipile`), whose
`get_profile` charges the budget the way the real client does -- a returned
profile always, a configured error only when `charge_error` is set.
"""

import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import functions
import profiles
from lib.unipile import compat
from lib.unipile import errors as unipile_errors
from linkedinmcp import clients, decisions, fetch_queue, fetching, jobs, ledger, state
from tests.linkedinmcp.fake_firestore import FakeDocumentReference, FakeFirestore
from tests.linkedinmcp.fake_unipile import (
    FakeUnipile,
    make_settings,
    profile,
    seed_contact,
    seed_fetch,
    store_snapshot,
)

NOW = datetime(2026, 9, 10, 14, 0, 0, tzinfo=UTC)
SLUG = "pat-doe"
QUEUED_AT = NOW - timedelta(hours=1)

#: What the replaced `clients.gemini_client` returns.
GEMINI = object()


class MutableClock:
    """A `clock` callable a test advances by hand (the `test_state.py`
    pattern)."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class Classifier:
    """Stands in for `profiles.classify_profile`: records `(client,
    summary)` per call and returns `result` -- a real `ProfileAnalysis`
    unless a test sets it to `None`."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.result = profiles.ProfileAnalysis(industry="Pathology", function="Operations", seniority="Director")

    def __call__(self, client, summary):
        self.calls.append((client, summary))
        return self.result


@pytest.fixture(autouse=True)
def gemini(monkeypatch) -> Classifier:
    classifier = Classifier()
    monkeypatch.setattr(clients, "gemini_client", lambda: GEMINI)
    monkeypatch.setattr(profiles, "classify_profile", classifier)
    return classifier


class Tick:
    """Everything one `fetch_one` call takes: a `FakeFirestore` whose
    `SERVER_TIMESTAMP` is the clock's time, the stub client, settings, and
    a `RuntimeState` on the same clock holding the tick lease.
    """

    def __init__(self, tmp_path, *, profile_limit: int = 250):
        self.clock = MutableClock(NOW)
        self.db = FakeFirestore(clock=self.clock)
        self.client = FakeUnipile(profile_limit=profile_limit)
        self.settings = make_settings(tmp_path)
        self.state = state.RuntimeState(self.db, clock=self.clock)
        self.owner = self.state.acquire_tick_lease()

    def fetch(self) -> dict:
        return fetching.fetch_one(
            self.db, self.client, self.settings, self.clock.now, state=self.state, owner=self.owner
        )

    def next_tick(self, at: datetime) -> None:
        """Move the clock to `at` and hold a fresh lease, as the next
        scheduled tick would."""
        self.state.release_tick_lease(self.owner)
        self.clock.now = at
        self.owner = self.state.acquire_tick_lease()

    def fail_with(self, error: BaseException, *, charged: bool) -> None:
        self.client.users.profile_error = error
        self.client.users.charge_error = charged


@pytest.fixture
def tick(tmp_path) -> Tick:
    return Tick(tmp_path)


def full_profile(slug=SLUG, provider_id="ACoAAPat"):
    """A complete profile whose summary is well over `SUMMARY_MIN_LEN`."""
    return profile(
        slug,
        provider_id,
        first_name="Pat",
        last_name="Doe",
        headline="Director of Revenue Cycle at Coastal Pathology Associates",
        summary="I run billing, coding and denial management for a regional pathology group.",
        network_distance="FIRST_DEGREE",
        work_experience=[
            {"position": "Director of Revenue Cycle", "company": "Coastal Pathology Associates", "start": "3/1/2019"},
        ],
    )


def summary_of_length(length: int, slug=SLUG):
    """A complete profile whose only non-empty summary field is a first
    name, so `join_keys(to_lh_document(p), SUMMARY_KEYS)` is exactly
    `length` characters."""
    return profile(slug, "ACoAAShort", first_name="x" * length)


def notebook_document(fetched) -> dict:
    """The `extracted` document `new-contacts.ipynb` Phase D stores for
    `fetched`, with its server `created_at` resolved to `NOW`."""
    document = compat.to_lh_document(fetched)
    document["summary"] = functions.join_keys(document, compat.SUMMARY_KEYS)
    document["created_at"] = NOW
    return document


def ledger_rows(db) -> list[dict]:
    return [document.to_dict() for document in db.collection("action_log").stream()]


def runtime(db) -> dict:
    return state.RuntimeState(db, clock=lambda: NOW).read()


def alerts(db) -> list[str]:
    return sorted(decision["id"] for decision in decisions.list_decisions(db, limit=100))


def document(db, collection, doc_id) -> dict | None:
    return db.collection(collection).document(doc_id).get().to_dict()


def profile_row(result, contact_doc_id=SLUG, *, at=NOW) -> dict:
    return {"kind": "profile", "contact_doc_id": contact_doc_id, "result": result, "queue_id": SLUG, "at": at}


def fetch_state(db, slug=SLUG) -> tuple:
    """`(status, attempts, last_error)` of the slug's fetch-queue item."""
    item = fetch_queue.get(db, slug)
    return (item["status"], item["attempts"], item["last_error"])


#: A fetch-queue item nothing has touched since the daily job queued it.
UNTOUCHED = (fetch_queue.QUEUED, 0, None)


def set_consecutive_throttled(db, n: int) -> None:
    """`n` throttled fetches happened earlier and their pause has passed."""
    db.collection(state.STATE_COLLECTION).document(state.STATE_DOCUMENT).set({"consecutive_throttled": n}, merge=True)


def incomplete() -> unipile_errors.ProfileIncomplete:
    return unipile_errors.ProfileIncomplete(type="local/profile_incomplete", title="withheld: skills")


def lockout() -> unipile_errors.ThrottleLockout:
    return unipile_errors.ThrottleLockout(type="local/throttle_lockout", title="5 profiles in a row")


def restricted() -> unipile_errors.AccountRestricted:
    return unipile_errors.AccountRestricted(type="errors/account_restricted", status=403, title="Account restricted")


def raises_firestore_unavailable(*args, **kwargs):
    """Stands in for a Firestore write that fails."""
    raise RuntimeError("firestore unavailable")


# =============================================================================
# the profile budget's 24 h count
# =============================================================================


def test_profiles_last_24h_adds_stored_profiles_to_the_ledger_rows_that_stored_nothing():
    """`extracted` documents created at or after `now - 24 h` (the boundary
    counts), plus `profile` ledger rows `short`, `incomplete` and `failed`
    in the same window: 2 + 3. A `stored` row is not added -- its profile is
    one of the `extracted` documents -- and neither is a `message` row, a
    row older than 24 h, or a legacy document with no `created_at`.
    """
    db = FakeFirestore()
    extracted = db.collection("extracted")
    extracted.document("recent").set({"created_at": NOW - timedelta(hours=1)})
    extracted.document("boundary").set({"created_at": NOW - timedelta(hours=24)})
    extracted.document("older").set({"created_at": NOW - timedelta(hours=24, seconds=1)})
    extracted.document("legacy").set({"fullName": "stored before created_at existed"})
    for result in ("stored", "short", "incomplete", "failed"):
        ledger.record(db, "profile", f"p-{result}", result, NOW - timedelta(hours=2))
    ledger.record(db, "profile", "p-old", "failed", NOW - timedelta(hours=25))
    ledger.record(db, "message", "m-1", "failed", NOW - timedelta(hours=2))

    assert fetching.profiles_last_24h(db, NOW) == 5


# =============================================================================
# before any LinkedIn call
# =============================================================================


@pytest.mark.parametrize("where", ["state", "client"])
def test_blocked_writes_stop_the_fetch_before_anything_else(tick, where):
    """Ruling P3-8: with writes blocked -- in the runtime state, or on the
    client's own breaker (some code caught an `AccountRestricted`) --
    `fetch_one` returns `{"fetch": "writes_blocked"}` at once: no LinkedIn
    call, no budget reconcile and nothing written, though a slug is queued
    and fetches are not paused."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile()
    if where == "state":
        tick.state.block_writes("restricted earlier")
    else:
        tick.client.writes_blocked = True
    before = store_snapshot(tick.db)

    result = tick.fetch()

    assert result == {"fetch": "writes_blocked"}
    assert tick.client.users.profile_calls == []
    assert tick.client.budget.reconcile_calls == []
    assert store_snapshot(tick.db) == before


def test_blocked_writes_are_reported_ahead_of_a_fetch_pause(tick):
    """Both hold: the check for blocked writes comes first, so the summary
    names the restriction rather than the pause."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.state.pause_fetches(NOW + timedelta(minutes=20), "a human paused fetches")
    tick.state.block_writes("restricted earlier")

    assert tick.fetch() == {"fetch": "writes_blocked"}


def test_a_fetch_pause_returns_paused_without_counting_or_asking_linkedin(tick):
    """The pause's end is `fetch_until`, not `until`: a tick stopped by a
    sends pause reports that pause's `until` in the same summary (ruling
    P3-6)."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.state.pause_fetches(NOW + timedelta(minutes=20), "a human paused fetches")

    result = tick.fetch()

    assert result == {"fetch": "paused", "fetch_until": (NOW + timedelta(minutes=20)).isoformat()}
    assert tick.client.users.profile_calls == []
    assert tick.client.budget.reconcile_calls == []
    assert fetch_state(tick.db) == UNTOUCHED


@pytest.mark.parametrize("limit, fetched", [(3, False), (4, True)], ids=["spent", "one-left"])
def test_the_budget_is_reconciled_with_the_24h_count_and_a_spent_one_stops_before_linkedin(tmp_path, limit, fetched):
    """Two profiles stored in the window and one `short` row: three, which
    `reconcile` makes the budget's `profile` count. A limit of three leaves
    nothing -- no fetch, the slug stays queued; a limit of four leaves one.
    """
    tick = Tick(tmp_path, profile_limit=limit)
    for doc_id in ("one", "two"):
        tick.db.collection("extracted").document(doc_id).set({"created_at": NOW - timedelta(hours=3)})
    ledger.record(tick.db, "profile", "three", "short", NOW - timedelta(hours=3))
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile()

    result = tick.fetch()

    assert tick.client.budget.reconcile_calls == [{"profile": 3}]
    if fetched:
        assert result["fetch"] == "stored"
        assert tick.client.users.profile_calls == [(SLUG, True)]
    else:
        assert result == {"fetch": "budget"}
        assert tick.client.users.profile_calls == []
        assert fetch_state(tick.db) == UNTOUCHED


def test_nothing_queued_is_idle(tick):
    assert tick.fetch() == {"fetch": "idle"}
    assert tick.client.users.profile_calls == []


@pytest.mark.parametrize(
    "floor, left, fetched",
    [
        pytest.param(None, 25, False, id="floor-30-25s-left"),
        pytest.param(None, 30, True, id="floor-30-30s-left"),
        pytest.param(100, 99, False, id="floor-100-99s-left"),
        pytest.param(100, 100, True, id="floor-100-100s-left"),
    ],
)
def test_a_lease_with_less_than_the_floor_left_stops_before_fetching(tick, monkeypatch, floor, left, fetched):
    """The lease is `state.LEASE_SECONDS` and `jobs.LEASE_FLOOR_SECONDS` is
    30: with 25 s left the fetch is not made and the slug stays queued; with
    exactly 30 s left it is. With the constant monkeypatched to 100, the
    boundary moves to 100 s -- `fetch_one` reads the floor through `jobs`
    when it runs.
    """
    if floor is not None:
        monkeypatch.setattr(jobs, "LEASE_FLOOR_SECONDS", floor)
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile()
    tick.clock.now = NOW + timedelta(seconds=state.LEASE_SECONDS - left)

    result = tick.fetch()

    assert jobs.LEASE_FLOOR_SECONDS == (floor or 30)
    if fetched:
        assert result["fetch"] == "stored"
    else:
        assert result == {"fetch": "lease_short"}
        assert tick.client.users.profile_calls == []
        assert fetch_state(tick.db) == UNTOUCHED


def test_the_newest_connection_is_fetched_first_with_require_complete(tick):
    seed_fetch(tick.db, "older", now=NOW - timedelta(hours=2))
    seed_fetch(tick.db, SLUG, now=NOW - timedelta(minutes=10))
    tick.client.users.profiles[SLUG] = full_profile()

    tick.fetch()

    assert tick.client.users.profile_calls == [(SLUG, True)]
    assert fetch_state(tick.db, "older") == UNTOUCHED


# =============================================================================
# the outcome table, one row at a time
# =============================================================================


def test_a_full_profile_is_stored_classified_marked_stored_and_resets_the_throttle_count(tick, gemini):
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile()
    set_consecutive_throttled(tick.db, 2)

    result = tick.fetch()

    assert result == {"fetch": "stored", "classified": True}
    assert ledger_rows(tick.db) == [profile_row("stored")]
    item = fetch_queue.get(tick.db, SLUG)
    assert (item["status"], item["classified"], item["last_error"], item["updated_at"]) == (
        fetch_queue.STORED, True, None, NOW,
    )
    assert runtime(tick.db)["consecutive_throttled"] == 0
    assert document(tick.db, "extracted", SLUG) is not None
    assert document(tick.db, "analysis", SLUG)["industry"] == "Pathology"


def test_a_profile_with_a_short_summary_is_marked_short_and_neither_stored_nor_classified(tick, gemini):
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = summary_of_length(4)
    set_consecutive_throttled(tick.db, 2)

    result = tick.fetch()

    assert result == {"fetch": "short"}
    assert ledger_rows(tick.db) == [profile_row("short")]
    item = fetch_queue.get(tick.db, SLUG)
    assert (item["status"], item["classified"]) == (fetch_queue.SHORT, None)
    assert runtime(tick.db)["consecutive_throttled"] == 0
    assert document(tick.db, "extracted", SLUG) is None
    assert document(tick.db, "analysis", SLUG) is None
    assert gemini.calls == []


@pytest.mark.parametrize("length, outcome", [(50, "short"), (51, "stored")])
def test_a_summary_must_be_longer_than_summary_min_len_to_be_stored(tick, length, outcome):
    """`profiles.SUMMARY_MIN_LEN` is 50: a 50-character summary is short and
    a 51-character one is stored -- the notebook's own comparison."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = summary_of_length(length)

    result = tick.fetch()

    assert profiles.SUMMARY_MIN_LEN == 50
    assert result["fetch"] == outcome
    assert fetch_queue.get(tick.db, SLUG)["status"] == outcome


@pytest.mark.parametrize("make_error", [incomplete, lockout], ids=["ProfileIncomplete", "ThrottleLockout"])
def test_a_throttled_fetch_backs_off_and_moves_the_slug_to_the_back_of_the_queue(tick, make_error):
    """LinkedIn answered 200 with sections withheld, so the fetch was
    charged: one `incomplete` row. Ruling P3-3: the slug stays queued but
    goes to the back (`queued_at` is now), with one incomplete counted and
    no attempt -- it is LinkedIn throttling, not a failed try. Fetches
    pause 30 minutes, the first step of the back-off.
    """
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    error = make_error()
    tick.fail_with(error, charged=True)

    result = tick.fetch()

    assert result == {"fetch": "throttled", "fetch_until": (NOW + timedelta(minutes=30)).isoformat()}
    assert ledger_rows(tick.db) == [profile_row("incomplete")]
    item = fetch_queue.get(tick.db, SLUG)
    assert (item["status"], item["attempts"], item["incomplete_count"], item["queued_at"], item["last_error"]) == (
        fetch_queue.QUEUED, 0, 1, NOW, type(error).__name__,
    )
    assert runtime(tick.db)["consecutive_throttled"] == 1
    assert runtime(tick.db)["fetches_paused_until"] == NOW + timedelta(minutes=30)
    assert alerts(tick.db) == []


def test_a_withheld_profile_goes_behind_the_next_fresh_slug(tick):
    """Ruling P3-3's head-of-line case: the newest connection's sections are
    withheld, so after the pause the next tick fetches the next fresh slug
    rather than the withheld one again."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT + timedelta(minutes=10))
    seed_fetch(tick.db, "kim-lee", now=QUEUED_AT)
    tick.fail_with(incomplete(), charged=True)

    first = tick.fetch()

    tick.next_tick(NOW + timedelta(minutes=31))
    tick.client.users.profile_error = None
    tick.client.users.profiles["kim-lee"] = full_profile("kim-lee", "ACoAAKim")
    second = tick.fetch()

    assert first["fetch"] == "throttled"
    assert second == {"fetch": "stored", "classified": True}
    assert [call[0] for call in tick.client.users.profile_calls] == [SLUG, "kim-lee"]
    assert fetch_queue.get(tick.db, SLUG)["status"] == fetch_queue.QUEUED


def test_the_third_withheld_fetch_gives_the_slug_up_and_nothing_is_queued_behind_it(tick):
    """A profile LinkedIn withholds three times is marked failed with
    "LinkedIn withheld sections 3 times" -- it then waits for the notebook
    -- after three charged fetches, each an `incomplete` row, and no
    attempt. The next tick finds nothing queued."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.fail_with(incomplete(), charged=True)

    results = []
    for n in range(3):
        tick.next_tick(NOW + timedelta(hours=3 * n))
        results.append(tick.fetch())
    tick.next_tick(NOW + timedelta(hours=9))
    after = tick.fetch()

    assert [result["fetch"] for result in results] == ["throttled"] * 3
    item = fetch_queue.get(tick.db, SLUG)
    assert (item["status"], item["incomplete_count"], item["attempts"], item["last_error"]) == (
        fetch_queue.FAILED, 3, 0, "LinkedIn withheld sections 3 times",
    )
    assert [row["result"] for row in ledger_rows(tick.db)] == ["incomplete"] * 3
    assert after == {"fetch": "idle"}
    assert len(tick.client.users.profile_calls) == 3


@pytest.mark.parametrize(
    "make_error, fetch",
    [
        pytest.param(
            lambda: unipile_errors.BudgetExhausted(type="local/budget_exhausted", title="profile budget spent"),
            "budget",
            id="BudgetExhausted",
        ),
        pytest.param(
            lambda: unipile_errors.CircuitOpen(type="local/circuit_open", title="writes are blocked"),
            "circuit_open",
            id="CircuitOpen",
        ),
    ],
)
def test_a_fetch_refused_before_any_request_changes_nothing(tick, make_error, fetch):
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.fail_with(make_error(), charged=False)
    state_before = runtime(tick.db)

    result = tick.fetch()

    assert result == {"fetch": fetch}
    assert ledger_rows(tick.db) == []
    assert fetch_state(tick.db) == UNTOUCHED
    assert runtime(tick.db) == state_before
    assert alerts(tick.db) == []


@pytest.mark.parametrize(
    "retry_after, paused_for",
    [(120.0, timedelta(seconds=120)), (None, timedelta(hours=1))],
    ids=["with-retry-after", "without-retry-after"],
)
def test_a_rate_limit_pauses_fetches_for_its_retry_after_or_an_hour(tick, retry_after, paused_for):
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.fail_with(
        unipile_errors.RateLimited(
            type="errors/too_many_requests", status=429, title="Too many requests", retry_after=retry_after
        ),
        charged=False,
    )

    result = tick.fetch()

    assert result == {"fetch": "rate_limited"}
    assert ledger_rows(tick.db) == []
    assert fetch_state(tick.db) == UNTOUCHED
    assert runtime(tick.db)["fetches_paused_until"] == NOW + paused_for
    assert runtime(tick.db)["fetch_pause_reason"] == "rate limited"
    assert alerts(tick.db) == []


def test_a_short_retry_after_pauses_fetches_from_the_state_clock_when_it_is_ahead_of_now(tick):
    """Ruling P5-4: the tick's `now` is when it started; the state's clock
    is three minutes on when LinkedIn answers 429 with a 60-second
    Retry-After. The fetch pause is counted from the later of the two, so
    it is still in force by the state's own clock when it is written."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.fail_with(
        unipile_errors.RateLimited(
            type="errors/too_many_requests", status=429, title="Too many requests", retry_after=60.0
        ),
        charged=False,
    )
    tick.clock.now = NOW + timedelta(minutes=3)

    result = fetching.fetch_one(tick.db, tick.client, tick.settings, NOW, state=tick.state, owner=tick.owner)

    until = NOW + timedelta(minutes=3, seconds=60)
    assert result == {"fetch": "rate_limited"}
    assert runtime(tick.db)["fetches_paused_until"] == until
    assert tick.state.fetches_paused_until() == until


def test_a_restricted_account_pauses_fetches_for_a_day_and_raises_no_alert_of_its_own(tick):
    """The job wrapper `jobs._watch_restriction` blocks writes and raises
    the `restricted` alert once the client's breaker has tripped
    (test_jobs_tick covers that end to end). The fetch itself only pauses
    fetches for 24 hours: no alert, no block written here.
    """
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.fail_with(restricted(), charged=False)

    result = tick.fetch()

    assert result == {"fetch": "restricted"}
    assert ledger_rows(tick.db) == []
    assert fetch_state(tick.db) == UNTOUCHED
    assert runtime(tick.db)["fetches_paused_until"] == NOW + timedelta(hours=24)
    assert runtime(tick.db).get("writes_blocked_at") is None
    assert alerts(tick.db) == []
    assert tick.client.writes_blocked is True


@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(lambda: unipile_errors.PermissionDenied(status=403, title="forbidden"), id="PermissionDenied"),
        pytest.param(
            lambda: unipile_errors.FeatureNotSubscribed(type="errors/feature_not_subscribed", status=403),
            id="FeatureNotSubscribed",
        ),
    ],
)
def test_a_forbidden_fetch_pauses_fetches_for_a_day_and_raises_one_fetch_forbidden_alert(tick, make_error):
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    error = make_error()
    tick.fail_with(error, charged=False)

    result = tick.fetch()

    assert result == {"fetch": "forbidden"}
    assert ledger_rows(tick.db) == []
    assert fetch_state(tick.db) == UNTOUCHED
    assert runtime(tick.db)["fetches_paused_until"] == NOW + timedelta(hours=24)
    assert alerts(tick.db) == ["alert:fetch_forbidden:20260910"]
    context = decisions.get(tick.db, "alert:fetch_forbidden:20260910")["context"]
    assert (context["slug"], context["error"]) == (SLUG, type(error).__name__)


@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(
            lambda: unipile_errors.AccountDisconnected(type="errors/disconnected_account", status=401, title="x"),
            id="AccountDisconnected",
        ),
        pytest.param(
            lambda: unipile_errors.AuthenticationError(status=401, title="Unauthorized"), id="AuthenticationError"
        ),
    ],
)
def test_a_disconnected_account_pauses_fetches_an_hour_and_raises_one_fetch_disconnected_alert(tick, make_error):
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    error = make_error()
    tick.fail_with(error, charged=False)

    result = tick.fetch()

    assert result == {"fetch": "disconnected"}
    assert ledger_rows(tick.db) == []
    assert fetch_state(tick.db) == UNTOUCHED
    assert runtime(tick.db)["fetches_paused_until"] == NOW + timedelta(hours=1)
    assert alerts(tick.db) == ["alert:fetch_disconnected:20260910"]
    context = decisions.get(tick.db, "alert:fetch_disconnected:20260910")["context"]
    assert (context["slug"], context["error"]) == (SLUG, type(error).__name__)


def test_a_fetch_alert_that_cannot_be_written_leaves_fetches_unpaused_for_the_next_tick_to_raise(tick, monkeypatch):
    """The alert is written before the pause. Writing it fails: the fetch
    raises with nothing paused and the slug still queued. The next tick
    meets the refusal again, raises the alert, and pauses fetches. The
    fetch was not charged, so nothing counts it: no ledger row and no
    attempt.
    """
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.fail_with(unipile_errors.PermissionDenied(status=403, title="forbidden"), charged=False)

    with monkeypatch.context() as patched:
        patched.setattr(decisions, "raise_alert", raises_firestore_unavailable)
        with pytest.raises(RuntimeError, match="firestore unavailable"):
            tick.fetch()

    assert runtime(tick.db).get("fetches_paused_until") is None
    assert fetch_state(tick.db) == UNTOUCHED
    assert ledger_rows(tick.db) == []

    later = NOW + timedelta(minutes=5)
    tick.next_tick(later)
    result = tick.fetch()

    assert result == {"fetch": "forbidden"}
    assert alerts(tick.db) == ["alert:fetch_forbidden:20260910"]
    assert runtime(tick.db)["fetches_paused_until"] == later + timedelta(hours=24)


def test_a_fetch_alert_is_keyed_by_the_local_date_in_settings_tz(tmp_path):
    """02:00 UTC on 10 September is 21:00 on the 9th in Chicago: the key is
    the local date, so a condition that repeats all evening raises one alert.
    """
    tick = Tick(tmp_path)
    tick.settings = make_settings(tmp_path, tz="America/Chicago")
    tick.next_tick(datetime(2026, 9, 10, 2, 0, 0, tzinfo=UTC))
    seed_fetch(tick.db, SLUG, now=tick.clock.now - timedelta(hours=1))
    tick.fail_with(unipile_errors.PermissionDenied(status=403, title="forbidden"), charged=False)

    tick.fetch()

    assert alerts(tick.db) == ["alert:fetch_forbidden:20260909"]


@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(lambda: unipile_errors.NotFound(status=404, title="no such user"), id="NotFound"),
        pytest.param(lambda: unipile_errors.UnprocessableError(status=422, title="x"), id="UnprocessableError"),
        pytest.param(
            lambda: unipile_errors.UserUnreachable(type="errors/user_unreachable", status=422), id="UserUnreachable"
        ),
    ],
)
def test_a_profile_linkedin_cannot_serve_is_marked_failed(tick, make_error):
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    error = make_error()
    tick.fail_with(error, charged=False)

    result = tick.fetch()

    assert result == {"fetch": "failed"}
    assert ledger_rows(tick.db) == []
    assert fetch_state(tick.db) == (fetch_queue.FAILED, 0, type(error).__name__)
    assert runtime(tick.db).get("fetches_paused_until") is None
    assert alerts(tick.db) == []


def unavailable():
    """What ruling P3-5 calls transient infrastructure: a 5xx and a
    dropped or timed-out connection."""
    return [
        pytest.param(lambda: unipile_errors.ServerError(status=502, title="Bad gateway"), id="ServerError"),
        pytest.param(lambda: httpx.ReadTimeout("read timed out"), id="ReadTimeout"),
        pytest.param(lambda: httpx.ConnectError("connection refused"), id="ConnectError"),
    ]


@pytest.mark.parametrize("charged", [False, True], ids=["not-charged", "charged"])
@pytest.mark.parametrize("make_error", unavailable())
def test_linkedin_being_unavailable_pauses_fetches_fifteen_minutes_and_counts_no_attempt(tick, make_error, charged):
    """Ruling P3-5: the slug did nothing wrong. Fetches pause 15 minutes
    with the reason "LinkedIn API unavailable", the slug stays queued with
    no attempt counted, and a `failed` row is written only when LinkedIn
    charged the fetch (a 5xx or a dropped connection never is in practice).
    """
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.fail_with(make_error(), charged=charged)

    result = tick.fetch()

    assert result == {"fetch": "unavailable"}
    assert ledger_rows(tick.db) == ([profile_row("failed")] if charged else [])
    assert fetch_state(tick.db) == UNTOUCHED
    assert runtime(tick.db)["fetches_paused_until"] == NOW + timedelta(minutes=15)
    assert runtime(tick.db)["fetch_pause_reason"] == "LinkedIn API unavailable"
    assert alerts(tick.db) == []


def test_an_outage_never_fails_the_slug(tick):
    """Five ticks 20 minutes apart, every fetch answered with a 503: each
    pauses fetches 15 minutes and none counts an attempt, so after five
    fetches the slug is still queued -- an outage does not use up its
    `MAX_ATTEMPTS`."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.fail_with(unipile_errors.ServerError(status=503, title="Service unavailable"), charged=False)

    results = []
    for n in range(5):
        tick.next_tick(NOW + timedelta(minutes=20 * n))
        results.append(tick.fetch())

    assert results == [{"fetch": "unavailable"}] * 5
    assert len(tick.client.users.profile_calls) == 5
    assert fetch_state(tick.db) == UNTOUCHED


@pytest.mark.parametrize(
    "make_error, charged",
    [
        pytest.param(lambda: RuntimeError("unexpected"), False, id="RuntimeError"),
        pytest.param(
            lambda: unipile_errors.UnipileError(status=400, title="Bad request"), False, id="UnipileError-400"
        ),
        pytest.param(lambda: ValueError("a 200 whose body is not a profile"), True, id="ValueError-after-a-200"),
    ],
)
def test_any_other_error_counts_one_attempt_on_the_slug(tick, make_error, charged):
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    error = make_error()
    tick.fail_with(error, charged=charged)

    result = tick.fetch()

    name = type(error).__name__
    assert result == {"fetch": "error", "error": name}
    assert ledger_rows(tick.db) == ([profile_row("failed")] if charged else [])
    assert fetch_state(tick.db) == (fetch_queue.QUEUED, 1, name)
    assert runtime(tick.db).get("fetches_paused_until") is None
    assert alerts(tick.db) == []


def test_the_third_failed_attempt_marks_the_slug_failed_and_the_next_fetch_is_idle(tick):
    """A non-transient error (not a 5xx or a dropped connection, which pause
    instead -- ruling P3-5) counts an attempt each time; the third marks the
    slug failed."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.fail_with(RuntimeError("unexpected"), charged=False)

    results = [tick.fetch() for _ in range(fetch_queue.MAX_ATTEMPTS)]

    assert [result["fetch"] for result in results] == ["error"] * 3
    assert fetch_state(tick.db) == (fetch_queue.FAILED, 3, "RuntimeError")
    assert tick.fetch() == {"fetch": "idle"}
    assert len(tick.client.users.profile_calls) == 3


@pytest.mark.parametrize("charged", [True, False], ids=["charged", "not-charged"])
@pytest.mark.parametrize(
    "make_error, result",
    [
        pytest.param(incomplete, "incomplete", id="ProfileIncomplete"),
        pytest.param(lockout, "incomplete", id="ThrottleLockout"),
        pytest.param(lambda: unipile_errors.RateLimited(status=429, title="x"), "failed", id="RateLimited"),
        pytest.param(restricted, "failed", id="AccountRestricted"),
        pytest.param(lambda: unipile_errors.PermissionDenied(status=403, title="x"), "failed", id="PermissionDenied"),
        pytest.param(
            lambda: unipile_errors.AccountDisconnected(status=401, title="x"), "failed", id="AccountDisconnected"
        ),
        pytest.param(lambda: unipile_errors.NotFound(status=404, title="x"), "failed", id="NotFound"),
        pytest.param(lambda: unipile_errors.UnprocessableError(status=422, title="x"), "failed", id="Unprocessable"),
        pytest.param(lambda: unipile_errors.ServerError(status=502, title="x"), "failed", id="ServerError"),
        pytest.param(lambda: httpx.ReadTimeout("read timed out"), "failed", id="ReadTimeout"),
        pytest.param(lambda: RuntimeError("anything else"), "failed", id="RuntimeError"),
    ],
)
def test_a_ledger_row_is_written_exactly_when_the_fetch_was_charged(tick, make_error, result, charged):
    """The table's ledger column: each row's result is written when the
    budget's `profile` count moved during `get_profile`, and nothing is
    written when it did not -- whatever the error.
    """
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.fail_with(make_error(), charged=charged)

    tick.fetch()

    assert [row["result"] for row in ledger_rows(tick.db)] == ([result] if charged else [])


# =============================================================================
# storing and classifying
# =============================================================================


def test_the_stored_document_is_the_one_new_contacts_phase_d_stores(tick):
    """`to_lh_document`, plus `summary = join_keys(document, SUMMARY_KEYS)`
    and a server `created_at`, under `extracted/{document["id"]}`."""
    fetched = full_profile()
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = fetched

    tick.fetch()

    stored = document(tick.db, "extracted", SLUG)
    assert stored == notebook_document(fetched)
    assert "Coastal Pathology Associates" in stored["summary"]


@pytest.mark.parametrize(
    "analysis, classified",
    [
        pytest.param(
            {"industry": "RCM", "function": "Finance", "seniority": "Director", "email": "pat@example.com"},
            True,
            id="all-three-categories",
        ),
        pytest.param({"industry": "RCM", "function": "", "seniority": "Director"}, False, id="one-category-empty"),
        pytest.param({"industry": "RCM", "email": "pat@example.com"}, False, id="industry-only"),
        pytest.param(None, False, id="no-analysis-document"),
    ],
)
def test_a_profile_already_in_extracted_is_marked_stored_without_asking_linkedin(tick, gemini, analysis, classified):
    """Ruling P3-4 (amended): the notebook stored this profile after the
    daily job queued it. The slug is marked `stored` with no LinkedIn call,
    no ledger row, no Gemini call and no write to `extracted` or
    `analysis`. `classified` is whether `analysis/{slug}` holds industry,
    function and seniority all non-empty -- `new-contacts.ipynb` Phase E's
    rule, not the document's mere existence. Nothing was fetched, so the
    throttle count is left as it was.
    """
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    earlier = {"fullName": "stored by the notebook", "created_at": NOW - timedelta(days=2)}
    tick.db.collection("extracted").document(SLUG).set(earlier)
    if analysis is not None:
        seed_contact(tick.db, SLUG, **analysis)
    set_consecutive_throttled(tick.db, 2)
    tick.client.users.profiles[SLUG] = full_profile()

    result = tick.fetch()

    assert result == {"fetch": "already_stored", "classified": classified}
    assert tick.client.users.profile_calls == []
    assert ledger_rows(tick.db) == []
    assert gemini.calls == []
    item = fetch_queue.get(tick.db, SLUG)
    assert (item["status"], item["classified"], item["attempts"], item["last_error"]) == (
        fetch_queue.STORED, classified, 0, None,
    )
    assert document(tick.db, "extracted", SLUG) == earlier
    assert document(tick.db, "analysis", SLUG) == analysis
    assert runtime(tick.db)["consecutive_throttled"] == 2


def test_a_profile_already_in_extracted_is_marked_stored_even_with_too_little_lease_left_to_fetch(tick):
    """The `extracted` check comes before the lease floor: marking the slug
    needs no LinkedIn call, so 25 seconds of lease are enough for it."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.db.collection("extracted").document(SLUG).set({"created_at": NOW - timedelta(days=2)})
    tick.clock.now = NOW + timedelta(seconds=state.LEASE_SECONDS - 25)

    result = tick.fetch()

    assert result == {"fetch": "already_stored", "classified": False}
    assert fetch_queue.get(tick.db, SLUG)["status"] == fetch_queue.STORED
    assert tick.client.users.profile_calls == []


def test_a_profile_the_notebook_stores_while_linkedin_answers_is_left_untouched_and_still_classified(tick, gemini):
    """The create-only race: `extracted/{slug}` is absent when the tick
    checks, and the notebook stores it while LinkedIn answers the tick's own
    fetch. `create` finds it and nothing is overwritten; the tick records
    its `stored` row, classifies what it fetched, merges that into
    `analysis` and marks the slug stored.
    """
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile()
    earlier = {"fullName": "stored by the notebook", "created_at": NOW - timedelta(seconds=1)}
    fetch = tick.client.users.get_profile

    def the_notebook_stores_it_meanwhile(identifier, *, require_complete=False):
        tick.db.collection("extracted").document(SLUG).set(earlier)
        return fetch(identifier, require_complete=require_complete)

    tick.client.users.get_profile = the_notebook_stores_it_meanwhile

    result = tick.fetch()

    assert result == {"fetch": "stored", "classified": True}
    assert tick.client.users.profile_calls == [(SLUG, True)]
    assert document(tick.db, "extracted", SLUG) == earlier
    assert document(tick.db, "analysis", SLUG)["industry"] == "Pathology"
    assert fetch_queue.get(tick.db, SLUG)["status"] == fetch_queue.STORED
    assert ledger_rows(tick.db) == [profile_row("stored")]


def test_analysis_is_created_with_exactly_analysis_bodys_fields(tick, gemini):
    """No `analysis` document existed: this path is the one writer allowed
    to create one (ruling P3-2), and it holds exactly `analysis_body`'s
    eight fields, `created_at` resolved by the server."""
    fetched = full_profile()
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = fetched

    tick.fetch()

    expected_fields = profiles.analysis_body(notebook_document(fetched), gemini.result)
    stored = document(tick.db, "analysis", SLUG)
    assert set(stored) == set(expected_fields)
    assert stored == {
        "profileUrl": "https://www.linkedin.com/in/pat-doe",
        "lh_id": "",
        "industry": "Pathology",
        "function": "Operations",
        "seniority": "Director",
        "summary": notebook_document(fetched)["summary"].replace("\n", " "),
        "memberDistance": 1,
        "created_at": NOW,
    }


@pytest.mark.parametrize(
    "half_written",
    [
        pytest.param({"industry": "Hospital"}, id="industry-only"),
        pytest.param({"industry": "Hospital", "function": "Finance", "seniority": ""}, id="seniority-empty"),
    ],
)
def test_a_half_written_classification_is_merged_over_and_the_contacts_other_fields_survive(
    tick, gemini, half_written
):
    """An `analysis` document without all three categories non-empty holds
    no classification (Phase E's rule), so the tick's is merged into it:
    the three categories are replaced and the contact's other fields
    survive the merge."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    seed_contact(tick.db, SLUG, email="pat@example.com", firstName="Pat", sent_total=2, **half_written)
    tick.client.users.profiles[SLUG] = full_profile()

    result = tick.fetch()

    assert result == {"fetch": "stored", "classified": True}
    stored = document(tick.db, "analysis", SLUG)
    assert (stored["email"], stored["firstName"], stored["sent_total"]) == ("pat@example.com", "Pat", 2)
    assert (stored["industry"], stored["function"], stored["seniority"]) == ("Pathology", "Operations", "Director")


def test_a_field_set_by_hand_survives_the_merge_while_the_others_are_filled(tick, gemini):
    """The contacts webapp names a hand-picked `industry` in `hand_set`; the
    contact is not yet classified, so the merge runs, but leaves it alone."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    seed_contact(tick.db, SLUG, email="pat@example.com", industry="Hospital", hand_set=["industry"])
    tick.client.users.profiles[SLUG] = full_profile()

    result = tick.fetch()

    assert result == {"fetch": "stored", "classified": True}
    stored = document(tick.db, "analysis", SLUG)
    assert (stored["industry"], stored["function"], stored["seniority"]) == ("Hospital", "Operations", "Director")
    assert (stored["email"], stored["hand_set"]) == ("pat@example.com", ["industry"])


def test_a_complete_classification_in_analysis_is_kept_and_gemini_is_not_called(tick, gemini):
    """Minor M4: `analysis/{doc_id}` already holds industry, function and
    seniority, all non-empty. The profile is stored, but nothing is merged
    into `analysis` -- not even `summary` or `created_at` -- Gemini is not
    called, and the slug is marked stored with `classified=True`."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    existing = {"industry": "Hospital", "function": "Finance", "seniority": "VP", "email": "pat@example.com"}
    seed_contact(tick.db, SLUG, **existing)
    tick.client.users.profiles[SLUG] = full_profile()

    result = tick.fetch()

    assert result == {"fetch": "stored", "classified": True}
    assert document(tick.db, "analysis", SLUG) == existing
    assert gemini.calls == []
    assert document(tick.db, "extracted", SLUG) is not None
    item = fetch_queue.get(tick.db, SLUG)
    assert (item["status"], item["classified"]) == (fetch_queue.STORED, True)
    assert ledger_rows(tick.db) == [profile_row("stored")]


def test_a_classification_written_while_gemini_runs_is_not_overwritten(tick, gemini, monkeypatch):
    """The notebook's Phase E classifies the contact while the tick waits
    for Gemini. The tick's merge re-reads `analysis` in the same
    transaction as its write, finds all three categories, and writes
    nothing: the notebook's classification stays, and the slug is marked
    stored with `classified=True`."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    seed_contact(tick.db, SLUG, email="pat@example.com")
    tick.client.users.profiles[SLUG] = full_profile()
    notebooks = {"industry": "Medical Lab", "function": "Operations", "seniority": "Manager"}

    def phase_e_classifies_meanwhile(client, summary):
        tick.db.collection("analysis").document(SLUG).set(notebooks, merge=True)
        return gemini(client, summary)

    monkeypatch.setattr(profiles, "classify_profile", phase_e_classifies_meanwhile)

    result = tick.fetch()

    assert result == {"fetch": "stored", "classified": True}
    assert len(gemini.calls) == 1
    assert document(tick.db, "analysis", SLUG) == {"email": "pat@example.com", **notebooks}
    assert fetch_queue.get(tick.db, SLUG)["classified"] is True


def test_the_classifier_gets_the_gemini_client_and_the_summary_on_one_line(tick, gemini):
    fetched = full_profile()
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = fetched

    tick.fetch()

    summary = notebook_document(fetched)["summary"]
    assert "\n" in summary
    assert gemini.calls == [(GEMINI, summary.replace("\n", " "))]


def test_a_classification_that_returns_none_stores_the_profile_and_records_classified_false(tick, gemini):
    """`None` leaves the contact for the notebook's Phase E, which
    classifies anything stored but unclassified: the profile is stored, no
    `analysis` document is written, and the slug is marked stored with
    `classified=False`."""
    gemini.result = None
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile()

    result = tick.fetch()

    assert result == {"fetch": "stored", "classified": False}
    assert document(tick.db, "extracted", SLUG) is not None
    assert document(tick.db, "analysis", SLUG) is None
    item = fetch_queue.get(tick.db, SLUG)
    assert (item["status"], item["classified"]) == (fetch_queue.STORED, False)


def no_key():
    """`genai.Client` refusing to build without an API key."""
    raise ValueError("Missing key inputs argument!")


def test_a_gemini_client_that_cannot_be_built_leaves_the_contact_unclassified_rather_than_queued(
    tick, gemini, monkeypatch, caplog
):
    """Classification that cannot start is treated as `None` -- stored,
    `classified=False`, a warning naming the slug -- not as an error that
    would leave the slug queued, to be fetched from LinkedIn again on every
    tick. Minor M3: it also raises one `gemini_unavailable` alert, keyed by
    the local date, naming the slug and the error class.
    """
    monkeypatch.setattr(clients, "gemini_client", no_key)
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile()

    with caplog.at_level(logging.WARNING, logger="linkedinmcp.fetching"):
        result = tick.fetch()

    assert result == {"fetch": "stored", "classified": False}
    item = fetch_queue.get(tick.db, SLUG)
    assert (item["status"], item["classified"]) == (fetch_queue.STORED, False)
    assert document(tick.db, "analysis", SLUG) is None
    assert gemini.calls == []
    assert SLUG in caplog.text
    assert "ValueError" in caplog.text
    assert alerts(tick.db) == ["alert:gemini_unavailable:20260910"]
    context = decisions.get(tick.db, "alert:gemini_unavailable:20260910")["context"]
    assert (context["slug"], context["error"]) == (SLUG, "ValueError")


def test_a_gemini_client_that_cannot_be_built_raises_one_alert_per_local_day(tick, monkeypatch):
    """Two profiles stored the same day make one `gemini_unavailable`
    alert; a third the next day makes a second."""
    monkeypatch.setattr(clients, "gemini_client", no_key)
    for n, slug in enumerate(("pat-doe", "kim-lee", "ann-roe")):
        seed_fetch(tick.db, slug, now=QUEUED_AT + timedelta(minutes=n))
        tick.client.users.profiles[slug] = full_profile(slug, f"ACoAA{n}")

    results = []
    for at in (NOW, NOW + timedelta(hours=2), NOW + timedelta(days=1)):
        tick.next_tick(at)
        results.append(tick.fetch())

    assert results == [{"fetch": "stored", "classified": False}] * 3
    assert alerts(tick.db) == ["alert:gemini_unavailable:20260910", "alert:gemini_unavailable:20260911"]


def test_the_gemini_alert_is_written_before_the_slug_is_marked(tick, monkeypatch):
    """Alert first, as the fetch alerts are: writing it fails, so the error
    leaves `fetch_one` before the slug is marked stored. The slug stays
    queued with one attempt counted (finding I1) and no `failed` row -- the
    `extracted` document it created counts the fetch."""
    monkeypatch.setattr(clients, "gemini_client", no_key)
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile()

    with monkeypatch.context() as patched:
        patched.setattr(decisions, "raise_alert", raises_firestore_unavailable)
        with pytest.raises(RuntimeError, match="firestore unavailable"):
            tick.fetch()

    assert fetch_state(tick.db) == (fetch_queue.QUEUED, 1, "RuntimeError")
    assert document(tick.db, "extracted", SLUG) is not None
    assert ledger_rows(tick.db) == [profile_row("stored")]


@pytest.mark.parametrize(
    "floor, left, classified",
    [
        pytest.param(None, 25, False, id="floor-30-25s-left"),
        pytest.param(None, 30, True, id="floor-30-30s-left"),
        pytest.param(100, 99, False, id="floor-100-99s-left"),
        pytest.param(100, 100, True, id="floor-100-100s-left"),
    ],
)
def test_a_lease_too_short_to_classify_stores_the_profile_unclassified(
    tick, gemini, monkeypatch, floor, left, classified
):
    """Minor M2: LinkedIn answered so slowly that the tick now holds less
    than `jobs.LEASE_FLOOR_SECONDS` of its lease (`state.LEASE_SECONDS`). The profile is
    stored and the slug marked stored with `classified=False`; Gemini is not
    called and no `analysis` document is written -- `new-contacts.ipynb`
    Phase E classifies it. With exactly the floor left it is classified.
    The floor is read through `jobs` (monkeypatched to 100 here too).
    """
    if floor is not None:
        monkeypatch.setattr(jobs, "LEASE_FLOOR_SECONDS", floor)
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile()
    fetch = tick.client.users.get_profile

    def slow_fetch(identifier, *, require_complete=False):
        tick.clock.now = NOW + timedelta(seconds=state.LEASE_SECONDS - left)
        return fetch(identifier, require_complete=require_complete)

    tick.client.users.get_profile = slow_fetch

    result = tick.fetch()

    assert result == {"fetch": "stored", "classified": classified}
    assert len(gemini.calls) == (1 if classified else 0)
    assert (document(tick.db, "analysis", SLUG) is not None) is classified
    assert document(tick.db, "extracted", SLUG) is not None
    item = fetch_queue.get(tick.db, SLUG)
    assert (item["status"], item["classified"]) == (fetch_queue.STORED, classified)


def test_a_profile_with_no_public_identifier_is_stored_under_its_provider_id_and_its_slug_is_marked(tick):
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile(slug=None, provider_id="ACoAAPat")

    result = tick.fetch()

    assert result == {"fetch": "stored", "classified": True}
    assert document(tick.db, "extracted", "ACoAAPat") is not None
    assert document(tick.db, "analysis", "ACoAAPat") is not None
    assert document(tick.db, "extracted", SLUG) is None
    assert fetch_queue.get(tick.db, SLUG)["status"] == fetch_queue.STORED
    assert ledger_rows(tick.db) == [profile_row("stored", "ACoAAPat")]


# =============================================================================
# a step after a CHARGED fetch fails (finding I1)
# =============================================================================


def extracted_create_fails(monkeypatch) -> None:
    """Every `create` in `extracted` raises a non-`Conflict` error."""
    real_create = FakeDocumentReference.create

    def create(self, document_data):
        if self._collection_name == "extracted":
            raise RuntimeError("firestore unavailable")
        return real_create(self, document_data)

    monkeypatch.setattr(FakeDocumentReference, "create", create)


def unmappable(fetched):
    raise ValueError("a profile to_lh_document cannot map")


def test_a_charged_fetch_whose_extracted_document_cannot_be_created_is_counted_before_the_error_leaves(
    tick, gemini, monkeypatch
):
    """LinkedIn charged the fetch; creating `extracted` raised. Nothing
    else counts that fetch, so before the error leaves `fetch_one` it
    writes one `failed` row and counts one attempt on the slug, which stays
    queued. No `analysis` document and no Gemini call.
    """
    extracted_create_fails(monkeypatch)
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile()

    with pytest.raises(RuntimeError, match="firestore unavailable"):
        tick.fetch()

    assert ledger_rows(tick.db) == [profile_row("failed")]
    assert fetch_state(tick.db) == (fetch_queue.QUEUED, 1, "RuntimeError")
    assert document(tick.db, "analysis", SLUG) is None
    assert gemini.calls == []


def test_a_charged_fetch_that_fails_the_same_way_every_tick_is_counted_each_time_and_given_up_at_max_attempts(
    tick, monkeypatch
):
    """The review's probe: `extracted` cannot be created on any tick. Five
    ticks make three LinkedIn fetches -- each counted by a `failed` row,
    three in `profiles_last_24h` -- and the third attempt marks the slug
    failed, so the last two ticks find nothing queued.
    """
    extracted_create_fails(monkeypatch)
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile()

    results = []
    for n in range(5):
        tick.next_tick(NOW + timedelta(minutes=5 * n))
        try:
            results.append(tick.fetch())
        except RuntimeError as error:
            results.append(str(error))

    assert results == ["firestore unavailable"] * 3 + [{"fetch": "idle"}] * 2
    assert len(tick.client.users.profile_calls) == 3
    assert [row["result"] for row in ledger_rows(tick.db)] == ["failed"] * 3
    assert fetch_state(tick.db) == (fetch_queue.FAILED, 3, "RuntimeError")
    assert fetching.profiles_last_24h(tick.db, NOW + timedelta(minutes=20)) == 3


def test_a_charged_fetch_whose_profile_cannot_be_mapped_is_counted_before_the_error_leaves(tick, monkeypatch):
    monkeypatch.setattr(compat, "to_lh_document", unmappable)
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile()

    with pytest.raises(ValueError, match="cannot map"):
        tick.fetch()

    assert ledger_rows(tick.db) == [profile_row("failed")]
    assert fetch_state(tick.db) == (fetch_queue.QUEUED, 1, "ValueError")
    assert document(tick.db, "extracted", SLUG) is None


def test_a_failure_after_extracted_was_created_adds_no_failed_row_and_the_next_tick_needs_no_linkedin_call(
    tick, monkeypatch
):
    """The profile was stored and classified; marking the slug raised. The
    `extracted` document this call created already counts the fetch, so no
    `failed` row is added -- the one row is `stored` and the 24 h count is
    1 -- and one attempt is counted. The next tick finds the profile in
    `extracted` and marks the slug stored without asking LinkedIn again
    (ruling P3-4).
    """
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile()

    with monkeypatch.context() as patched:
        patched.setattr(fetch_queue, "mark", raises_firestore_unavailable)
        with pytest.raises(RuntimeError, match="firestore unavailable"):
            tick.fetch()

    assert ledger_rows(tick.db) == [profile_row("stored")]
    assert fetch_state(tick.db) == (fetch_queue.QUEUED, 1, "RuntimeError")
    assert document(tick.db, "extracted", SLUG) is not None
    assert fetching.profiles_last_24h(tick.db, NOW) == 1

    tick.next_tick(NOW + timedelta(minutes=5))
    result = tick.fetch()

    assert result == {"fetch": "already_stored", "classified": True}
    assert len(tick.client.users.profile_calls) == 1
    assert fetch_queue.get(tick.db, SLUG)["status"] == fetch_queue.STORED


def test_a_failure_after_the_incomplete_row_adds_no_second_row(tick, monkeypatch):
    """LinkedIn withheld sections (charged: one `incomplete` row), then
    pausing fetches raised. The `incomplete` row already counts the fetch,
    so no `failed` row is added; one attempt is counted and the error
    leaves."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.fail_with(incomplete(), charged=True)

    with monkeypatch.context() as patched:
        patched.setattr(tick.state, "note_fetch_throttled", raises_firestore_unavailable)
        with pytest.raises(RuntimeError, match="firestore unavailable"):
            tick.fetch()

    assert ledger_rows(tick.db) == [profile_row("incomplete")]
    assert fetch_state(tick.db) == (fetch_queue.QUEUED, 1, "RuntimeError")


def test_when_counting_the_charged_fetch_fails_too_the_original_error_is_the_one_that_leaves(
    tick, monkeypatch, caplog
):
    """Mapping raised `ValueError` after a charged fetch, and neither the
    `failed` row nor the attempt can be written. Both are best-effort: each
    failure is logged with the slug, nothing is written, and the error that
    leaves `fetch_one` is still the `ValueError`."""
    monkeypatch.setattr(compat, "to_lh_document", unmappable)
    monkeypatch.setattr(ledger, "record", raises_firestore_unavailable)
    monkeypatch.setattr(fetch_queue, "note_attempt", raises_firestore_unavailable)
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.client.users.profiles[SLUG] = full_profile()

    with caplog.at_level(logging.WARNING, logger="linkedinmcp.fetching"):
        with pytest.raises(ValueError, match="cannot map"):
            tick.fetch()

    assert ledger_rows(tick.db) == []
    assert fetch_state(tick.db) == UNTOUCHED
    assert caplog.text.count("firestore unavailable") == 2
    assert SLUG in caplog.text


# =============================================================================
# the throttle back-off across ticks
# =============================================================================


def test_the_back_off_doubles_across_two_throttled_ticks_and_resets_after_a_stored_one(tick):
    """30 minutes, then an hour; a tick inside the pause does not fetch; a
    stored profile resets the count, so the next throttle is 30 minutes
    again."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.fail_with(incomplete(), charged=True)

    first = tick.fetch()

    tick.next_tick(NOW + timedelta(minutes=10))
    during_pause = tick.fetch()

    second_at = NOW + timedelta(minutes=31)
    tick.next_tick(second_at)
    second = tick.fetch()

    stored_at = second_at + timedelta(hours=1, minutes=1)
    tick.next_tick(stored_at)
    tick.client.users.profile_error = None
    tick.client.users.profiles[SLUG] = full_profile()
    stored = tick.fetch()

    third_at = stored_at + timedelta(minutes=5)
    tick.next_tick(third_at)
    seed_fetch(tick.db, "next-one", now=third_at - timedelta(minutes=1))
    tick.fail_with(incomplete(), charged=True)
    third = tick.fetch()

    assert first == {"fetch": "throttled", "fetch_until": (NOW + timedelta(minutes=30)).isoformat()}
    assert during_pause == {"fetch": "paused", "fetch_until": (NOW + timedelta(minutes=30)).isoformat()}
    assert second == {"fetch": "throttled", "fetch_until": (second_at + timedelta(hours=1)).isoformat()}
    assert stored == {"fetch": "stored", "classified": True}
    assert third == {"fetch": "throttled", "fetch_until": (third_at + timedelta(minutes=30)).isoformat()}
    assert runtime(tick.db)["consecutive_throttled"] == 1
    assert [call[0] for call in tick.client.users.profile_calls] == [SLUG, SLUG, SLUG, "next-one"]


# =============================================================================
# preview (the dry tick)
# =============================================================================


def test_preview_names_the_slug_it_would_fetch_and_the_24h_count_and_writes_nothing(tick):
    seed_fetch(tick.db, "older", now=QUEUED_AT - timedelta(days=1))
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.db.collection("extracted").document("stored-today").set({"created_at": NOW - timedelta(hours=2)})
    ledger.record(tick.db, "profile", "withheld", "incomplete", NOW - timedelta(hours=2))
    before = store_snapshot(tick.db)

    result = fetching.preview(tick.db, NOW, state=tick.state)

    assert result == {"fetch": "would_fetch", "slug": SLUG, "profiles_24h": 2}
    assert store_snapshot(tick.db) == before


@pytest.mark.parametrize("classified", [True, False], ids=["classified", "unclassified"])
def test_preview_reports_a_profile_already_in_extracted_and_writes_nothing(tick, classified):
    """The case `fetch_one` would mark `already_stored` without a LinkedIn
    call: reported under that name with the slug and whether `analysis`
    holds all three categories, and nothing is marked."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.db.collection("extracted").document(SLUG).set({"created_at": NOW - timedelta(days=2)})
    seed_contact(tick.db, SLUG, industry="RCM", function="Finance", seniority="Director" if classified else "")
    before = store_snapshot(tick.db)

    result = fetching.preview(tick.db, NOW, state=tick.state)

    assert result == {"fetch": "already_stored", "slug": SLUG, "classified": classified, "profiles_24h": 0}
    assert store_snapshot(tick.db) == before


def test_preview_with_nothing_queued_is_idle_and_writes_nothing(tick):
    before = store_snapshot(tick.db)

    result = fetching.preview(tick.db, NOW, state=tick.state)

    assert result == {"fetch": "idle", "profiles_24h": 0}
    assert store_snapshot(tick.db) == before


def test_preview_with_writes_blocked_reports_it_and_writes_nothing(tick):
    """Ruling P3-8: the dry tick's preview agrees with `fetch_one` -- with
    writes blocked in the runtime state it says `writes_blocked`, not the
    slug it would otherwise fetch."""
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.state.block_writes("restricted earlier")
    before = store_snapshot(tick.db)

    result = fetching.preview(tick.db, NOW, state=tick.state)

    assert result == {"fetch": "writes_blocked"}
    assert store_snapshot(tick.db) == before


def test_preview_during_a_fetch_pause_reports_it_and_writes_nothing(tick):
    seed_fetch(tick.db, SLUG, now=QUEUED_AT)
    tick.state.pause_fetches(NOW + timedelta(hours=2), "rate limited")
    before = store_snapshot(tick.db)

    result = fetching.preview(tick.db, NOW, state=tick.state)

    assert result == {"fetch": "paused", "fetch_until": (NOW + timedelta(hours=2)).isoformat()}
    assert store_snapshot(tick.db) == before
