"""Send budget for rate-limited actions.

LinkedIn restricts accounts that act too fast, so every write and every profile
fetch passes through a cap and a randomised delay.

Counters are held in memory for the life of the client and seeded by
``reconcile``, which the caller invokes at the start of a run: it recounts the
last 24 hours from the stores that actually outlive the process -- ``created_at``
on the contacts saved to Firestore, LinkedIn's own record of invitations and
messages sent -- and passes the totals in. Those stores are the durable state, so
there is nothing here worth writing to disk.

Counters used to persist to a JSON file bucketed by UTC calendar day. That is
gone on both counts: the day bucket made a cap spent at 23:59 a full one a minute
later -- a burst, which is the rhythm LinkedIn restricts -- and the file itself
drifted from reality the moment an invitation went out from LinkedIn directly,
which it could not see and a recount can.

A process that spends without reconciling first therefore starts from zero. That
is logged, loudly, the first time it happens.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .errors import BudgetExhausted
from .pacing import HumanCadence

Kind = Literal["invite", "message", "profile"]

#: Reserved keys holding LinkedIn's own quota reading and when it was taken.
#: They share the per-account bucket with the counters; the leading underscore
#: keeps them out of the ``Kind`` namespace.
_USAGE_KEY = "_usage_pct"
_USAGE_AT_KEY = "_usage_pct_at"

#: How long a usage reading is believed. LinkedIn publishes no reset for the
#: percentage, so it has to age out on its own -- a latch that never cleared
#: would strand the account. It expires on the same 24 hours as the counters.
_USAGE_TTL = timedelta(hours=24)

log = logging.getLogger(__name__)


class SendBudget:
    """Per-account counters for rate-limited actions, over a rolling 24 hours.

    The call order is deliberately ``check`` -> ``throttle`` -> send ->
    ``record``. Fusing check and record into one "reserve" step would either
    charge the budget for sends that failed, or make it impossible to refuse
    before the network call.

    ``account_id`` may be a callable. The client does not know its account until
    it resolves one from the API, and a placeholder would mean the first check of
    every client instance ran against an empty counter -- one silent over-send
    per process.

    ``cadence`` and the two usage thresholds are required, and there are no
    pacing bounds to pass here: this module must not import ``config``, so any
    default would be a second copy of a value ``UnipileSettings`` owns.
    :class:`~lib.unipile.client.UnipileClient` is the one place that reads
    settings and injects them.
    """

    def __init__(
        self,
        account_id: str | Callable[[], str],
        limits: dict[str, int],
        *,
        cadence: HumanCadence,
        usage_warn_pct: float,
        usage_halt_pct: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._account_id = account_id
        #: Counters per account, keyed the same way the resolved account id is,
        #: so a client that rekeys after resolution does not carry a placeholder
        #: account's counts forward.
        self._state: dict[str, dict[str, Any]] = {}
        self._reconciled = False
        self._warned_unreconciled = False
        self._limits = dict(limits)
        self._clock = clock
        self._cadence = cadence
        self._usage_warn_pct = usage_warn_pct
        self._usage_halt_pct = usage_halt_pct

    @property
    def account_id(self) -> str:
        """The account these counters belong to, resolved on each use."""
        source = self._account_id
        return source() if callable(source) else source

    # --- querying -------------------------------------------------------------

    def used(self, kind: Kind) -> int:
        """How many of ``kind`` are charged against the current window."""
        return int(self._counts().get(kind, 0))

    def remaining(self, kind: Kind) -> int:
        """How many more of ``kind`` the cap still allows."""
        return max(0, self._limits.get(kind, 0) - self.used(kind))

    # --- the send path --------------------------------------------------------

    def check(self, kind: Kind) -> None:
        """Refuse before the network call when the budget is spent."""
        self._warn_if_unreconciled()
        if kind == "invite":
            self._check_provider_usage()
        if self.remaining(kind) <= 0:
            raise BudgetExhausted(
                type="local/budget_exhausted",
                title=f"{kind} budget spent ({self._limits.get(kind, 0)} per 24h)",
                detail="Allowance returns as earlier actions age out of the "
                "24-hour window; re-run later, or raise the cap in .env.",
            )

    def throttle(self) -> None:
        """Wait one human-looking interval before the next call."""
        self._cadence.wait()

    def back_off(self) -> None:
        """LinkedIn is withholding data: widen every gap until it clears."""
        self._cadence.back_off()

    def recovered(self) -> None:
        """A clean response: return to the normal cadence."""
        self._cadence.recovered()

    def record(self, kind: Kind, count: int = 1) -> None:
        """Charge ``count`` against the budget and persist immediately.

        Profile fetches are recorded on any 200, complete or not -- LinkedIn
        counted the fetch either way.
        """
        self._update(lambda counts: counts.update({kind: int(counts.get(kind, 0)) + count}))

    # --- drift correction -----------------------------------------------------

    def reconcile(self, **observed: int) -> None:
        """Replace local counts with the totals actually observed elsewhere.

        This is how the rolling window is applied. The caller recounts the last
        24 hours from records that outlive the process -- ``created_at`` on the
        contacts it saved, ``count_invitations_since()``,
        ``count_messages_sent_since()`` -- and passes the totals in. Counts are
        passed rather than fetched so this module keeps no dependency on the
        resource layer or on any particular store.

        Call it before spending, not during: within a run ``record`` charges
        every action, including profile fetches LinkedIn withheld, which leave
        no saved contact behind for a later recount to find.

        Calling it with nothing is meaningful -- it says the recount ran and
        found no activity, which is not the same as never having looked.
        """
        self._reconciled = True
        self._update(lambda counts: counts.update(observed))

    def _warn_if_unreconciled(self) -> None:
        """Say so, once, when a run spends against counters nobody seeded.

        Nothing persists between processes any more, so an unreconciled budget
        is not a budget -- it is a full allowance handed out on every start. The
        caller is meant to recount first; this makes forgetting visible instead
        of silent.
        """
        if self._reconciled or self._warned_unreconciled:
            return
        self._warned_unreconciled = True
        log.warning(
            "Spending against counters that were never reconciled: this process "
            "starts from zero and cannot see actions taken before it. Call "
            "reconcile() with a recount of the last 24 hours first."
        )

    def note_usage(self, pct: float | None) -> None:
        """Consume LinkedIn's own quota reading, returned on invitations.

        The reading is stored with the time it was taken, so a caller who
        swallows the exception, or simply starts a new process, still cannot keep
        inviting. It is deliberately **not** a permanent flag: LinkedIn publishes
        no reset for this percentage, so a latch that never clears would strand
        the account. It ages out after ``_USAGE_TTL``, and a later, lower reading
        lifts it immediately.
        """
        if pct is None:
            return
        taken_at = self._clock().astimezone(UTC).isoformat()
        self._update(
            lambda counts: counts.update({_USAGE_KEY: pct, _USAGE_AT_KEY: taken_at})
        )
        if pct >= self._usage_halt_pct:
            raise BudgetExhausted(
                type="local/provider_usage_halt",
                title=f"Provider usage at {pct:g}% of the LinkedIn limit",
                detail="Halting invitations to avoid a restriction.",
            )
        if pct >= self._usage_warn_pct:
            log.warning("Provider usage at %g%% of the LinkedIn limit", pct)

    def _check_provider_usage(self) -> None:
        """Refuse invitations while an unexpired usage reading sits at the halt
        threshold. Only invitations: the percentage is returned on the
        invitation route and describes that quota, so blocking messages on it
        would over-refuse."""
        counts = self._counts()
        pct = counts.get(_USAGE_KEY)
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            return
        if pct < self._usage_halt_pct or self._usage_age(counts) >= _USAGE_TTL:
            return
        raise BudgetExhausted(
            type="local/provider_usage_halt",
            title=f"Provider usage at {pct:g}% of the LinkedIn limit",
            detail="Halting invitations until the reading ages out, or until "
            "LinkedIn reports a lower usage.",
        )

    def _usage_age(self, counts: dict[str, Any]) -> timedelta:
        """How long ago the stored usage reading was taken.

        An unparseable or missing timestamp counts as expired. A reading carried
        over from the UTC-day-keyed file has none, and it would have cleared at
        the next midnight anyway; keeping it forever is the one outcome this
        signal must never produce.
        """
        taken_at = counts.get(_USAGE_AT_KEY)
        if not isinstance(taken_at, str):
            return timedelta.max
        try:
            stamped = datetime.fromisoformat(taken_at)
        except ValueError:
            return timedelta.max
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=UTC)
        return self._clock().astimezone(UTC) - stamped

    # --- state ----------------------------------------------------------------

    def _counts(self) -> dict[str, Any]:
        return self._state.get(self.account_id, {})

    def _update(self, mutate: Callable[[dict[str, Any]], None]) -> None:
        mutate(self._state.setdefault(self.account_id, {}))
