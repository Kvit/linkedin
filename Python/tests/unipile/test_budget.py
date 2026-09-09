"""Daily send budget.

Counters live in a file keyed by UTC date and account, so they survive notebook
kernel restarts and script re-runs. A run that exhausts its budget stops; the
next day's run resumes.
"""

import random
from datetime import UTC, datetime, timedelta

import pytest

from lib.unipile.budget import SendBudget
from lib.unipile.errors import BudgetExhausted
from lib.unipile.pacing import HumanCadence

ACCOUNT = "ZIGT4FVWS4CCJze_MuVHCg"
LIMITS = {"invite": 2, "message": 3, "profile": 5}


def make(tmp_path, clock=None):
    return SendBudget(
        path=tmp_path / "budget.json",
        account_id=ACCOUNT,
        limits=LIMITS,
        clock=clock or (lambda: datetime(2026, 9, 8, 12, 0, tzinfo=UTC)),
        sleep=lambda _seconds: None,
    )


def test_check_raises_once_the_daily_cap_is_reached(tmp_path):
    budget = make(tmp_path)

    for _ in range(LIMITS["invite"]):
        budget.check("invite")
        budget.record("invite")

    with pytest.raises(BudgetExhausted) as excinfo:
        budget.check("invite")
    assert "invite" in str(excinfo.value)


def test_a_refused_check_does_not_advance_the_counter(tmp_path):
    budget = make(tmp_path)
    for _ in range(LIMITS["invite"]):
        budget.check("invite")
        budget.record("invite")

    with pytest.raises(BudgetExhausted):
        budget.check("invite")

    assert budget.used("invite") == LIMITS["invite"]


def test_counters_reset_after_utc_midnight(tmp_path):
    now = datetime(2026, 9, 8, 23, 59, tzinfo=UTC)
    budget = make(tmp_path, clock=lambda: now)
    budget.record("invite")
    budget.record("invite")
    assert budget.remaining("invite") == 0

    now = now + timedelta(minutes=2)
    assert budget.remaining("invite") == LIMITS["invite"]


def test_counters_survive_a_new_instance_against_the_same_file(tmp_path):
    make(tmp_path).record("message")

    assert make(tmp_path).used("message") == 1


def test_counters_are_scoped_per_account(tmp_path):
    make(tmp_path).record("message")

    other = SendBudget(
        path=tmp_path / "budget.json",
        account_id="OTHER_ACCOUNT",
        limits=LIMITS,
        clock=lambda: datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        sleep=lambda _s: None,
    )
    assert other.used("message") == 0


def test_writes_leave_no_temp_file_behind(tmp_path):
    budget = make(tmp_path)
    budget.record("profile")

    assert [p.name for p in tmp_path.iterdir()] == ["budget.json"]


def test_stale_dates_are_pruned_on_write(tmp_path):
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    budget = make(tmp_path, clock=lambda: now)
    budget.record("invite")

    now = now + timedelta(days=1)
    budget.record("invite")

    import json

    state = json.loads((tmp_path / "budget.json").read_text())
    assert list(state) == ["2026-09-09"]


def paced(tmp_path, slept, **cadence_kwargs):
    """A budget whose cadence is seeded, so the gaps it draws are reproducible."""
    return SendBudget(
        path=tmp_path / "budget.json",
        account_id=ACCOUNT,
        limits=LIMITS,
        clock=lambda: datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        cadence=HumanCadence(
            4.0, 12.0, rng=random.Random(7), sleep=slept.append, **cadence_kwargs
        ),
    )


def test_throttle_sleeps_within_the_configured_bounds(tmp_path):
    slept = []
    budget = paced(tmp_path, slept, long_pause_every=0)

    budget.throttle()

    assert len(slept) == 1
    assert 4.0 <= slept[0] <= 12.0


def test_throttle_takes_the_cadence_long_breaks(tmp_path):
    """The pause between calls is the cadence's decision, not the budget's."""
    slept = []
    budget = paced(tmp_path, slept, long_pause_every=1)

    budget.throttle()

    assert slept[0] >= 60.0


def test_backing_off_stretches_the_gaps(tmp_path):
    """A throttled response makes every following gap longer until it clears."""
    normal, slowed = [], []
    paced(tmp_path, normal, long_pause_every=0).throttle()

    budget = paced(tmp_path, slowed, long_pause_every=0)
    budget.back_off()
    budget.throttle()

    assert slowed[0] > normal[0]


def test_recovering_restores_the_normal_gaps(tmp_path):
    normal, restored = [], []
    paced(tmp_path, normal, long_pause_every=0).throttle()

    budget = paced(tmp_path, restored, long_pause_every=0)
    budget.back_off()
    budget.recovered()
    budget.throttle()

    assert restored[0] == pytest.approx(normal[0])


def test_usage_below_the_halt_threshold_is_allowed(tmp_path):
    make(tmp_path).note_usage(74.0)


def test_usage_at_the_halt_threshold_stops_sending(tmp_path):
    budget = make(tmp_path)

    with pytest.raises(BudgetExhausted):
        budget.note_usage(90.0)


def test_usage_past_the_warn_threshold_logs_a_warning(tmp_path, caplog):
    budget = make(tmp_path)

    with caplog.at_level("WARNING"):
        budget.note_usage(80.0)

    assert "80" in caplog.text


def test_reconcile_overrides_local_counts_with_observed_totals(tmp_path):
    """The local file drifts if sends happen from LinkedIn directly."""
    budget = make(tmp_path)
    budget.record("invite")

    budget.reconcile(invite=9, message=4)

    assert budget.used("invite") == 9
    assert budget.used("message") == 4


# --- provider usage signal ----------------------------------------------------


def test_usage_halt_blocks_later_invites_in_the_same_process(tmp_path):
    budget = make(tmp_path)

    with pytest.raises(BudgetExhausted):
        budget.note_usage(95.0)

    with pytest.raises(BudgetExhausted, match="usage"):
        budget.check("invite")


def test_usage_halt_survives_a_restart(tmp_path):
    with pytest.raises(BudgetExhausted):
        make(tmp_path).note_usage(95.0)

    with pytest.raises(BudgetExhausted):
        make(tmp_path).check("invite")


def test_usage_halt_clears_at_utc_midnight_like_every_other_counter(tmp_path):
    """LinkedIn's usage percentage has no documented reset, so a flag that never
    clears would strand the account. Rolling it over with the daily counters
    keeps the batch model intact."""
    now = datetime(2026, 9, 8, 23, 59, tzinfo=UTC)
    budget = make(tmp_path, clock=lambda: now)
    with pytest.raises(BudgetExhausted):
        budget.note_usage(95.0)

    now = now + timedelta(minutes=2)
    budget.check("invite")


def test_a_lower_usage_reading_lifts_the_halt(tmp_path):
    """The signal is live, so LinkedIn reporting less usage must be believed."""
    budget = make(tmp_path)
    with pytest.raises(BudgetExhausted):
        budget.note_usage(95.0)

    budget.note_usage(40.0)

    budget.check("invite")


def test_the_usage_halt_does_not_block_messages(tmp_path):
    """The usage percentage is returned on the invitation route and describes
    the invitation quota; blocking messages on it would over-refuse."""
    budget = make(tmp_path)
    with pytest.raises(BudgetExhausted):
        budget.note_usage(95.0)

    budget.check("message")
