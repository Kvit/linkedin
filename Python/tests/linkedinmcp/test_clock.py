"""Tests for `linkedinmcp.clock`: the one place this service reads the wall
clock and converts a stored UTC moment into a human's local date.

`local_date` is the one this file spends the most care on: a wrong-by-one-zone
conversion silently shifts what "today" means for the daily cap and the
follow-up cadence.
"""

from datetime import UTC, date, datetime

import pytest

from linkedinmcp import clock


def test_utcnow_is_timezone_aware_with_a_zero_utc_offset():
    now = clock.utcnow()

    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 0


def test_local_date_converts_across_a_date_boundary():
    """03:30 UTC on 2026-03-10 is still 2026-03-09 in New York (UTC-4/-5), but
    is 2026-03-10 in UTC itself -- the same instant, two different calendar
    dates, which is the entire reason `local_date` takes a `tz` argument
    instead of just calling `.date()`.
    """
    moment = datetime(2026, 3, 10, 3, 30, tzinfo=UTC)

    assert clock.local_date(moment, "America/New_York") == date(2026, 3, 9)
    assert clock.local_date(moment, "UTC") == date(2026, 3, 10)


def test_local_date_rejects_a_naive_moment():
    """A naive datetime could be anything -- UTC, server local time, whatever
    the caller last stored -- and guessing would silently shift what "today"
    means. This must raise rather than assume.
    """
    naive = datetime(2026, 3, 10, 3, 30)

    with pytest.raises(ValueError):
        clock.local_date(naive, "America/New_York")
