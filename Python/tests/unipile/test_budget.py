"""Send budget over a rolling 24 hours.

Counters live in memory and are seeded by `reconcile`, which the caller feeds a
recount of the last 24 hours taken from Firestore and LinkedIn. Nothing here
expires them on a clock, and nothing persists them: the durable state is those
stores, not this object.
"""

import random
from datetime import UTC, datetime, timedelta

import pytest

from lib.unipile.budget import SendBudget
from lib.unipile.errors import BudgetExhausted
from lib.unipile.pacing import HumanCadence

ACCOUNT = "ZIGT4FVWS4CCJze_MuVHCg"
LIMITS = {"invite": 2, "message": 3, "profile": 5}


def make(clock=None):
    return SendBudget(
        account_id=ACCOUNT,
        limits=LIMITS,
        clock=clock or (lambda: datetime(2026, 9, 8, 12, 0, tzinfo=UTC)),
        sleep=lambda _seconds: None,
    )


def test_check_raises_once_the_daily_cap_is_reached():
    budget = make()

    for _ in range(LIMITS["invite"]):
        budget.check("invite")
        budget.record("invite")

    with pytest.raises(BudgetExhausted) as excinfo:
        budget.check("invite")
    assert "invite" in str(excinfo.value)


def test_a_refused_check_does_not_advance_the_counter():
    budget = make()
    for _ in range(LIMITS["invite"]):
        budget.check("invite")
        budget.record("invite")

    with pytest.raises(BudgetExhausted):
        budget.check("invite")

    assert budget.used("invite") == LIMITS["invite"]


def test_counters_do_not_reset_at_utc_midnight():
    """The window is a rolling 24h set by `reconcile`, not the calendar day.

    Rolling over at midnight let a cap spent at 23:59 come back a minute later,
    which is the burst LinkedIn restricts. Crossing midnight must change nothing.
    """
    now = datetime(2026, 9, 8, 23, 59, tzinfo=UTC)
    budget = make(clock=lambda: now)
    budget.record("invite")
    budget.record("invite")
    assert budget.remaining("invite") == 0

    now = now + timedelta(minutes=2)
    assert budget.remaining("invite") == 0

    # Only a recount hands allowance back.
    budget.reconcile(invite=0)
    assert budget.remaining("invite") == LIMITS["invite"]


def test_a_new_budget_starts_empty_and_says_so(caplog):
    """Nothing persists between processes, so an unreconciled budget is a full
    allowance. Spending against one has to be visible, not silent."""
    budget = make()

    with caplog.at_level("WARNING"):
        budget.check("invite")

    assert budget.used("invite") == 0
    assert "reconcile" in caplog.text


def test_a_reconciled_budget_does_not_warn(caplog):
    budget = make()
    budget.reconcile(invite=1)

    with caplog.at_level("WARNING"):
        budget.check("invite")

    assert caplog.text == ""


def test_reconciling_with_nothing_still_counts_as_having_looked(caplog):
    """"The recount found no activity" is not the same as never looking."""
    budget = make()
    budget.reconcile()

    with caplog.at_level("WARNING"):
        budget.check("invite")

    assert caplog.text == ""


def test_the_unreconciled_warning_is_logged_only_once(caplog):
    budget = make()

    with caplog.at_level("WARNING"):
        budget.check("invite")
        budget.check("invite")

    assert len(caplog.records) == 1


def test_counters_follow_the_account_the_client_resolves_to():
    """`account_id` is a callable because the account is not known at
    construction. Counts recorded against a placeholder must not follow the
    real account once it resolves."""
    account = "pending"
    budget = SendBudget(
        account_id=lambda: account,
        limits=LIMITS,
        clock=lambda: datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        sleep=lambda _s: None,
    )
    budget.record("message")

    account = ACCOUNT

    assert budget.used("message") == 0


def paced(slept, **cadence_kwargs):
    """A budget whose cadence is seeded, so the gaps it draws are reproducible."""
    return SendBudget(
        account_id=ACCOUNT,
        limits=LIMITS,
        clock=lambda: datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        cadence=HumanCadence(
            4.0, 12.0, rng=random.Random(7), sleep=slept.append, **cadence_kwargs
        ),
    )


def test_throttle_sleeps_within_the_configured_bounds():
    slept = []
    budget = paced(slept, long_pause_every=0)

    budget.throttle()

    assert len(slept) == 1
    assert 4.0 <= slept[0] <= 12.0


def test_throttle_takes_the_cadence_long_breaks():
    """The pause between calls is the cadence's decision, not the budget's."""
    slept = []
    budget = paced(slept, long_pause_every=1)

    budget.throttle()

    assert slept[0] >= 60.0


def test_backing_off_stretches_the_gaps():
    """A throttled response makes every following gap longer until it clears."""
    normal, slowed = [], []
    paced(normal, long_pause_every=0).throttle()

    budget = paced(slowed, long_pause_every=0)
    budget.back_off()
    budget.throttle()

    assert slowed[0] > normal[0]


def test_recovering_restores_the_normal_gaps():
    normal, restored = [], []
    paced(normal, long_pause_every=0).throttle()

    budget = paced(restored, long_pause_every=0)
    budget.back_off()
    budget.recovered()
    budget.throttle()

    assert restored[0] == pytest.approx(normal[0])


def test_usage_below_the_halt_threshold_is_allowed():
    make().note_usage(74.0)


def test_usage_at_the_halt_threshold_stops_sending():
    budget = make()

    with pytest.raises(BudgetExhausted):
        budget.note_usage(90.0)


def test_usage_past_the_warn_threshold_logs_a_warning(caplog):
    budget = make()

    with caplog.at_level("WARNING"):
        budget.note_usage(80.0)

    assert "80" in caplog.text


def test_reconcile_overrides_local_counts_with_observed_totals():
    """The local file drifts if sends happen from LinkedIn directly."""
    budget = make()
    budget.record("invite")

    budget.reconcile(invite=9, message=4)

    assert budget.used("invite") == 9
    assert budget.used("message") == 4


# --- provider usage signal ----------------------------------------------------


def test_usage_halt_blocks_later_invites_in_the_same_process():
    budget = make()

    with pytest.raises(BudgetExhausted):
        budget.note_usage(95.0)

    with pytest.raises(BudgetExhausted, match="usage"):
        budget.check("invite")


def test_usage_halt_does_not_outlive_the_process():
    """The reading is in-process state now. A new run learns the real figure
    from LinkedIn's next invitation response, not from a stale local copy."""
    with pytest.raises(BudgetExhausted):
        make().note_usage(95.0)

    make().check("invite")


def test_usage_halt_expires_once_the_reading_is_a_day_old():
    """LinkedIn's usage percentage has no documented reset, so a flag that never
    clears would strand the account. With no day key left to roll it over, the
    reading carries the time it was taken and ages out on that."""
    now = datetime(2026, 9, 8, 23, 59, tzinfo=UTC)
    budget = make(clock=lambda: now)
    with pytest.raises(BudgetExhausted):
        budget.note_usage(95.0)

    # Crossing midnight is not what clears it any more.
    now = now + timedelta(minutes=2)
    with pytest.raises(BudgetExhausted, match="usage"):
        budget.check("invite")

    now = now + timedelta(hours=24)
    budget.check("invite")


def test_a_lower_usage_reading_lifts_the_halt():
    """The signal is live, so LinkedIn reporting less usage must be believed."""
    budget = make()
    with pytest.raises(BudgetExhausted):
        budget.note_usage(95.0)

    budget.note_usage(40.0)

    budget.check("invite")


def test_the_usage_halt_does_not_block_messages():
    """The usage percentage is returned on the invitation route and describes
    the invitation quota; blocking messages on it would over-refuse."""
    budget = make()
    with pytest.raises(BudgetExhausted):
        budget.note_usage(95.0)

    budget.check("message")
