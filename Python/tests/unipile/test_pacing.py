"""Pacing: the gaps between calls should look like a person, not a cron job."""

import random
import statistics

import pytest

from lib.unipile.pacing import HumanCadence

BOUNDS = {"min_delay": 4.0, "max_delay": 12.0}
BREAK = {"long_pause_min": 60.0, "long_pause_max": 240.0}


def cadence(slept, *, seed=7, long_pause_every=0, **kwargs):
    return HumanCadence(
        **{**BOUNDS, **BREAK, **kwargs},
        long_pause_every=long_pause_every,
        rng=random.Random(seed),
        sleep=slept.append,
    )


def test_a_short_gap_stays_within_the_configured_bounds():
    slept = []
    pace = cadence(slept)

    for _ in range(200):
        pace.wait()

    assert min(slept) >= 4.0
    assert max(slept) <= 12.0


def test_short_gaps_cluster_near_the_low_end():
    """A person hesitates briefly most of the time and dawdles occasionally.

    A uniform draw would put the median at the midpoint (8s), so this fails if
    the delay is drawn flat across the range.
    """
    slept = []
    pace = cadence(slept)

    for _ in range(400):
        pace.wait()

    assert statistics.median(slept) < 7.0
    assert max(slept) > 9.0  # the long tail is still reachable


def test_consecutive_gaps_differ():
    slept = []
    pace = cadence(slept)

    pace.wait()
    pace.wait()

    assert slept[0] != slept[1]


def test_wait_returns_the_seconds_it_slept():
    slept = []
    pace = cadence(slept)

    assert pace.wait() == pytest.approx(slept[0])


def test_a_long_break_interrupts_the_run():
    """With a break due every call, every gap is a break-sized gap."""
    slept = []
    pace = cadence(slept, long_pause_every=1)

    pace.wait()

    assert slept[0] >= 60.0


def test_long_breaks_are_occasional_not_constant():
    slept = []
    pace = cadence(slept, long_pause_every=10)

    for _ in range(500):
        pace.wait()

    breaks = [gap for gap in slept if gap > 12.0]
    assert 20 < len(breaks) < 100, f"expected roughly 50 breaks in 500 calls, got {len(breaks)}"


def test_long_breaks_can_be_switched_off():
    slept = []
    pace = cadence(slept, long_pause_every=0)

    for _ in range(300):
        pace.wait()

    assert max(slept) <= 12.0


def test_a_long_break_is_announced(caplog):
    slept = []
    pace = cadence(slept, long_pause_every=1)

    with caplog.at_level("INFO", logger="lib.unipile.pacing"):
        pace.wait()

    assert "pausing" in caplog.text.lower()


def test_a_throttled_wait_says_so(caplog):
    """A silent multi-minute pause is indistinguishable from a hang."""
    slept = []
    pace = cadence(slept, long_pause_every=0)
    pace.back_off()

    with caplog.at_level("WARNING", logger="lib.unipile.pacing"):
        pace.wait()

    assert "throttl" in caplog.text.lower()
    assert "waiting" in caplog.text.lower()


def test_a_normal_wait_is_not_announced_as_throttling(caplog):
    slept = []
    pace = cadence(slept, long_pause_every=0)

    with caplog.at_level("WARNING", logger="lib.unipile.pacing"):
        pace.wait()

    assert caplog.text == ""


def test_backing_off_stretches_the_gaps():
    """LinkedIn withholding data is a signal to slow down, not to keep pace."""
    normal, slowed = [], []
    cadence(normal).wait()

    pace = cadence(slowed)
    pace.back_off()
    pace.wait()

    assert slowed[0] > normal[0]


def test_backing_off_is_capped():
    slept = []
    pace = cadence(slept)

    for _ in range(50):
        pace.back_off()
    pace.wait()

    assert slept[0] <= 12.0 * HumanCadence.MAX_BACKOFF


def test_a_clean_call_resets_the_backoff():
    normal, recovered = [], []
    cadence(normal).wait()

    pace = cadence(recovered)
    pace.back_off()
    pace.recovered()
    pace.wait()

    assert recovered[0] == pytest.approx(normal[0])
