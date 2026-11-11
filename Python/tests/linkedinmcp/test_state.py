"""Tests for `linkedinmcp.state.RuntimeState`: the single
`runtime_state/linkedin` document that coordinates every LinkedIn write
(a lease so only one send or fetch runs at a time), a human's pause/block controls, a
webhook's sync request, and the require-approval override.

Every method is exercised against `FakeFirestore` with a `MutableClock` the
test advances by hand -- never a real `time.sleep` -- so "the lease expired"
and "the lease is still live" are both deterministic. The three transactional
methods (`acquire_tick_lease`, `release_tick_lease`, `clear_sync_request`) are
driven through the REAL `@firestore.transactional` decorator inside
`state.py`, not reimplemented here, so `db.contend_once()` proves the retry
path actually runs (see `fake_firestore.py`'s own docstring for what that
helper pins).

The "merge discipline" test at the bottom is the one that matters most: this
whole service shares ONE document, so a method that wrote with a bare
`set()` instead of `set(..., merge=True)` would silently erase every field
it does not itself own the moment it ran.
"""

from datetime import UTC, datetime, timedelta

import pytest

from linkedinmcp import jobs, state
from tests.linkedinmcp.fake_firestore import FakeFirestore


class MutableClock:
    """A `clock` callable a test can advance by hand, standing in for
    `clock.utcnow` so "later" and "already expired" are deterministic
    instead of depending on a real sleep.
    """

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


START = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def make_state():
    db = FakeFirestore()
    clock = MutableClock(START)
    return state.RuntimeState(db, clock=clock), db, clock


# --- read() -------------------------------------------------------------


def test_read_on_an_empty_database_is_an_empty_dict():
    rs, db, clock = make_state()

    assert rs.read() == {}


def test_now_is_the_states_own_clock_reading():
    rs, db, clock = make_state()
    first = rs.now()
    clock.now = clock.now + timedelta(minutes=3)

    assert (first, rs.now()) == (START, START + timedelta(minutes=3))


# --- acquire_tick_lease() / release_tick_lease() / lease_remaining() -----


def test_the_tick_lease_lasts_225_seconds():
    """Ruling P5-4: long enough for a sweep, a requested sync, the chat check
    and `jobs.LEASE_FLOOR_SECONDS` for the write; short enough that a lease
    a dead job left behind is gone within four minutes."""
    assert state.LEASE_SECONDS == 225
    assert state.LEASE_SECONDS - jobs.LEASE_FLOOR_SECONDS >= 180


def test_acquire_on_an_empty_database_returns_a_32_char_hex_owner_with_exact_expiry():
    rs, db, clock = make_state()

    owner = rs.acquire_tick_lease()

    assert owner is not None
    assert len(owner) == 32
    assert all(c in "0123456789abcdef" for c in owner)
    assert rs.read()["tick_lease_owner"] == owner
    assert rs.read()["tick_lease_until"] == clock.now + timedelta(seconds=225)


def test_second_acquire_while_the_lease_is_live_returns_none_and_leaves_the_owner():
    rs, db, clock = make_state()
    first = rs.acquire_tick_lease()

    second = rs.acquire_tick_lease()

    assert second is None
    assert rs.read()["tick_lease_owner"] == first


def test_acquire_after_the_stored_until_has_passed_returns_a_new_owner():
    rs, db, clock = make_state()
    first = rs.acquire_tick_lease(seconds=60)
    clock.now = clock.now + timedelta(seconds=61)

    second = rs.acquire_tick_lease(seconds=60)

    assert second is not None
    assert second != first
    assert rs.read()["tick_lease_owner"] == second


def test_release_tick_lease_with_the_wrong_owner_returns_false_and_lease_is_intact():
    rs, db, clock = make_state()
    owner = rs.acquire_tick_lease()

    cleared = rs.release_tick_lease("not-the-owner")

    assert cleared is False
    assert rs.read()["tick_lease_owner"] == owner


def test_release_tick_lease_with_the_right_owner_clears_it_and_a_following_acquire_succeeds():
    rs, db, clock = make_state()
    owner = rs.acquire_tick_lease()

    cleared = rs.release_tick_lease(owner)

    assert cleared is True
    assert rs.read()["tick_lease_owner"] is None
    assert rs.read()["tick_lease_until"] is None
    assert rs.acquire_tick_lease() is not None


def test_lease_remaining_for_the_holder_is_the_exact_remaining_seconds():
    rs, db, clock = make_state()
    owner = rs.acquire_tick_lease(seconds=240)
    clock.now = clock.now + timedelta(seconds=100)

    assert rs.lease_remaining(owner) == 140.0


def test_lease_remaining_for_any_other_owner_is_zero():
    rs, db, clock = make_state()
    rs.acquire_tick_lease()

    assert rs.lease_remaining("someone-else") == 0.0


def test_lease_remaining_after_expiry_is_zero_even_for_the_holder():
    rs, db, clock = make_state()
    owner = rs.acquire_tick_lease(seconds=60)
    clock.now = clock.now + timedelta(seconds=61)

    assert rs.lease_remaining(owner) == 0.0


def test_acquire_still_returns_an_owner_when_the_transaction_contends_once():
    """`db.contend_once()` forces the FIRST commit attempt to abort; the real
    `@firestore.transactional` decorator inside `acquire_tick_lease` must
    retry, and the owner it finally returns must be the one actually stored.

    `owner is not None` and the stored-owner check alone would also pass a
    non-transactional `ref.get()` + `ref.set()` implementation that never
    calls `db.transaction()` at all -- `contend_once()`'s arm would simply
    sit unconsumed, since only `FakeTransaction._commit()` ever calls
    `_consume_contend_once()`. Asserting the arm was consumed
    (`_contend_once_armed` back to `False`) is what actually proves a
    transaction committed -- and, since the forced `Aborted` still let
    `acquire_tick_lease` succeed, that it retried after aborting once,
    rather than the abort somehow being skipped.
    """
    rs, db, clock = make_state()
    db.contend_once()

    owner = rs.acquire_tick_lease()

    assert owner is not None
    assert rs.read()["tick_lease_owner"] == owner
    assert db._contend_once_armed is False  # the arm was consumed by a real _commit()


# --- pause_sends() / resume_sends() / sends_paused_until() ---------------


def test_sends_paused_until_reports_a_future_until():
    rs, db, clock = make_state()
    until = clock.now + timedelta(hours=1)

    rs.pause_sends(until, "rate limited")

    assert rs.sends_paused_until() == until
    assert rs.read()["pause_reason"] == "rate limited"


def test_sends_paused_until_reads_a_past_until_as_none():
    rs, db, clock = make_state()
    until = clock.now + timedelta(hours=1)
    rs.pause_sends(until, "rate limited")
    clock.now = until + timedelta(seconds=1)

    assert rs.sends_paused_until() is None


def test_resume_sends_clears_the_pause():
    rs, db, clock = make_state()
    rs.pause_sends(clock.now + timedelta(hours=1), "rate limited")

    rs.resume_sends()

    assert rs.sends_paused_until() is None
    assert rs.read()["sends_paused_until"] is None
    assert rs.read()["pause_reason"] is None


def test_pause_sends_rejects_a_naive_until():
    rs, db, clock = make_state()

    with pytest.raises(ValueError):
        rs.pause_sends(datetime(2026, 9, 8, 13, 0), "rate limited")


# --- pause_fetches() / resume_fetches() / fetches_paused_until() ---------


def test_fetches_paused_until_reports_a_future_until():
    rs, db, clock = make_state()
    until = clock.now + timedelta(hours=1)

    rs.pause_fetches(until, "throttled")

    assert rs.fetches_paused_until() == until
    assert rs.read()["fetch_pause_reason"] == "throttled"


def test_fetches_paused_until_reads_a_past_until_as_none():
    rs, db, clock = make_state()
    until = clock.now + timedelta(hours=1)
    rs.pause_fetches(until, "throttled")
    clock.now = until + timedelta(seconds=1)

    assert rs.fetches_paused_until() is None


def test_resume_fetches_clears_the_pause():
    rs, db, clock = make_state()
    rs.pause_fetches(clock.now + timedelta(hours=1), "throttled")

    rs.resume_fetches()

    assert rs.fetches_paused_until() is None
    assert rs.read()["fetches_paused_until"] is None
    assert rs.read()["fetch_pause_reason"] is None


def test_pause_fetches_rejects_a_naive_until():
    rs, db, clock = make_state()

    with pytest.raises(ValueError):
        rs.pause_fetches(datetime(2026, 9, 8, 13, 0), "throttled")


def test_pause_fetches_does_not_touch_a_sends_pause():
    """The two pauses are independent fields on the same shared document --
    pausing fetches must not read as `sends_paused_until` or vice versa."""
    rs, db, clock = make_state()
    rs.pause_sends(clock.now + timedelta(hours=2), "rate limited")

    rs.pause_fetches(clock.now + timedelta(hours=1), "throttled")

    assert rs.sends_paused_until() == clock.now + timedelta(hours=2)
    assert rs.fetches_paused_until() == clock.now + timedelta(hours=1)


# --- note_fetch_throttled() / note_fetch_ok() -----------------------------


def test_note_fetch_throttled_first_call_pauses_30_minutes():
    rs, db, clock = make_state()

    until = rs.note_fetch_throttled()

    assert until == clock.now + timedelta(minutes=30)
    assert rs.fetches_paused_until() == until
    assert rs.read()["consecutive_throttled"] == 1
    assert rs.read()["fetch_pause_reason"] == "LinkedIn withheld profile sections (1 in a row)"


def test_note_fetch_throttled_backoff_doubles_then_caps_at_24_hours():
    """30 min, 1 h, 2 h, 4 h, 8 h, 16 h, then capped at 24 h from then on."""
    rs, db, clock = make_state()
    expected = [
        timedelta(minutes=30), timedelta(hours=1), timedelta(hours=2), timedelta(hours=4),
        timedelta(hours=8), timedelta(hours=16), timedelta(hours=24), timedelta(hours=24),
    ]

    for n, delta in enumerate(expected, start=1):
        until = rs.note_fetch_throttled()
        assert until == clock.now + delta, n
        assert rs.read()["consecutive_throttled"] == n
        assert rs.read()["fetch_pause_reason"] == f"LinkedIn withheld profile sections ({n} in a row)"


def test_note_fetch_ok_resets_consecutive_throttled_without_clearing_the_pause():
    rs, db, clock = make_state()
    rs.note_fetch_throttled()
    rs.note_fetch_throttled()

    rs.note_fetch_ok()

    assert rs.read()["consecutive_throttled"] == 0
    assert rs.fetches_paused_until() is not None  # only the counter is reset


def test_note_fetch_throttled_still_succeeds_when_the_transaction_contends_once():
    """Same proof as `acquire_tick_lease`'s own contention test: the real
    `@firestore.transactional` decorator retries after a forced `Aborted`,
    and the value returned is the one actually stored."""
    rs, db, clock = make_state()
    db.contend_once()

    until = rs.note_fetch_throttled()

    assert until == clock.now + timedelta(minutes=30)
    assert rs.read()["fetches_paused_until"] == until
    assert db._contend_once_armed is False


# --- block_writes() / unblock_writes() / writes_blocked() ----------------


def test_block_writes_then_writes_blocked_is_true_and_unblock_clears_it():
    rs, db, clock = make_state()

    rs.block_writes("account restricted")

    assert rs.writes_blocked() is True
    assert rs.read()["writes_blocked_at"] == clock.now
    assert rs.read()["writes_blocked_reason"] == "account restricted"

    rs.unblock_writes()

    assert rs.writes_blocked() is False
    assert rs.read()["writes_blocked_at"] is None
    assert rs.read()["writes_blocked_reason"] is None


def test_writes_blocked_is_false_when_nothing_is_stored():
    rs, db, clock = make_state()

    assert rs.writes_blocked() is False


# --- request_sync() / sync_requested() / clear_sync_request() ------------


def test_request_sync_then_sync_requested_returns_the_stored_time():
    rs, db, clock = make_state()

    rs.request_sync()

    assert rs.sync_requested() == clock.now


def test_sync_requested_is_none_when_nothing_is_stored():
    rs, db, clock = make_state()

    assert rs.sync_requested() is None


def test_clear_sync_request_clears_one_stored_at_or_before_seen():
    rs, db, clock = make_state()
    rs.request_sync()

    cleared = rs.clear_sync_request(clock.now)

    assert cleared is True
    assert rs.sync_requested() is None


def test_clear_sync_request_does_not_clear_one_stored_after_seen():
    """The webhook race the contract calls out: a second sync request can
    land while the first sync is still running. The sweep must not clear a
    request that arrived after the moment it saw.
    """
    rs, db, clock = make_state()
    seen = clock.now
    clock.now = clock.now + timedelta(seconds=1)
    rs.request_sync()  # stored AFTER `seen`

    cleared = rs.clear_sync_request(seen)

    assert cleared is False
    assert rs.sync_requested() == clock.now


# --- require_approval() / set_require_approval() -------------------------


def test_require_approval_returns_the_default_when_nothing_is_stored():
    rs, db, clock = make_state()

    assert rs.require_approval(True) is True
    assert rs.require_approval(False) is False


@pytest.mark.parametrize("value", [True, False])
def test_require_approval_returns_the_stored_value_after_set(value):
    rs, db, clock = make_state()

    rs.set_require_approval(value)

    assert rs.require_approval(not value) is value


# --- budget_snapshot() / store_budget_snapshot() --------------------------


def test_budget_snapshot_round_trips():
    rs, db, clock = make_state()
    taken_at = clock.now

    rs.store_budget_snapshot(17, taken_at)

    assert rs.budget_snapshot() == (17, taken_at)


def test_budget_snapshot_is_none_when_nothing_is_stored():
    rs, db, clock = make_state()

    assert rs.budget_snapshot() is None


# --- merge discipline -----------------------------------------------------


def test_every_setter_preserves_an_unrelated_pre_written_field():
    rs, db, clock = make_state()
    db.collection(state.STATE_COLLECTION).document(state.STATE_DOCUMENT).set(
        {"unrelated": "keep-me"}
    )

    owner = rs.acquire_tick_lease()
    rs.release_tick_lease(owner)
    rs.pause_sends(clock.now + timedelta(hours=1), "reason")
    rs.resume_sends()
    rs.block_writes("reason")
    rs.unblock_writes()
    rs.request_sync()
    rs.clear_sync_request(clock.now)
    rs.set_require_approval(True)
    rs.store_budget_snapshot(1, clock.now)
    rs.pause_fetches(clock.now + timedelta(hours=1), "reason")
    rs.resume_fetches()
    rs.note_fetch_throttled()
    rs.note_fetch_ok()

    assert rs.read()["unrelated"] == "keep-me"
