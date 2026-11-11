"""The one place this service reads the wall clock or converts to a local date.

Storage modules take time as a parameter rather than calling `datetime.now()`
themselves -- see `state.py` and `ledger.py` -- so this module supplies the one
function that actually reads it (`utcnow`, the default `clock` callable
`RuntimeState` is built with) and the one function that converts a stored,
timezone-aware moment into "today" in a human's zone (`local_date`, driven by
`settings.tz`). Keeping both here means a test can swap `utcnow` for a fixed
callable and every caller downstream -- lease expiry, budget windows, daily
caps -- moves in lockstep.

`local_date` refuses a naive `moment` rather than guessing its zone: a
datetime with no `tzinfo` could be UTC, local server time, or anything else,
and silently treating it as one of those would shift "today" by hours in a way
that is easy to miss in a test and expensive to debug in production (an intro
queued for "today" that was actually queued for yesterday).
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    """The current time, timezone-aware, in UTC. Every stored datetime in this
    service is produced this way (or by an injected fake standing in for it in
    tests) -- never a naive `datetime.now()`.
    """
    return datetime.now(UTC)


def local_date(moment: datetime, tz: str) -> date:
    """The calendar date `moment` falls on in the IANA zone `tz`.

    `moment` must already be timezone-aware -- see the module docstring for why
    a naive value raises rather than being assumed to be UTC.
    """
    if moment.tzinfo is None:
        raise ValueError("local_date: `moment` must be timezone-aware")
    return moment.astimezone(ZoneInfo(tz)).date()
