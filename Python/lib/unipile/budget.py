"""Daily send budget, persisted to disk.

LinkedIn restricts accounts that act too fast, so every write and every profile
fetch passes through a per-day cap and a randomised delay. Counters live in a
JSON file rather than memory because a notebook kernel restart or a re-run must
not hand back a fresh allowance.

Days are **UTC**. Unipile timestamps invitations in UTC, so a UTC key lets
``reconcile`` compare like with like instead of drifting by the local offset.
"""

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .errors import BudgetExhausted
from .pacing import HumanCadence

Kind = Literal["invite", "message", "profile"]

#: Reserved key holding LinkedIn's own quota reading for the day. It shares the
#: per-account bucket with the counters so it rolls over with them; the leading
#: underscore keeps it out of the ``Kind`` namespace.
_USAGE_KEY = "_usage_pct"

log = logging.getLogger(__name__)


class SendBudget:
    """Per-day, per-account counters for rate-limited actions.

    The call order is deliberately ``check`` -> ``throttle`` -> send ->
    ``record``. Fusing check and record into one "reserve" step would either
    charge the budget for sends that failed, or make it impossible to refuse
    before the network call.

    ``account_id`` may be a callable. The client does not know its account until
    it resolves one from the API, and a placeholder would mean the first check of
    every client instance ran against an empty counter -- one silent over-send
    per process.
    """

    def __init__(
        self,
        path: Path,
        account_id: str | Callable[[], str],
        limits: dict[str, int],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        min_delay: float = 20.0,
        max_delay: float = 90.0,
        cadence: HumanCadence | None = None,
        usage_warn_pct: float = 75.0,
        usage_halt_pct: float = 90.0,
    ) -> None:
        self._path = Path(path)
        self._account_id = account_id
        self._limits = dict(limits)
        self._clock = clock
        self._cadence = cadence or HumanCadence(min_delay, max_delay, sleep=sleep)
        self._usage_warn_pct = usage_warn_pct
        self._usage_halt_pct = usage_halt_pct

    @property
    def account_id(self) -> str:
        """The account these counters belong to, resolved on each use."""
        source = self._account_id
        return source() if callable(source) else source

    # --- querying -------------------------------------------------------------

    def used(self, kind: Kind) -> int:
        """How many of ``kind`` have been recorded today."""
        return int(self._today_counts().get(kind, 0))

    def remaining(self, kind: Kind) -> int:
        """How many more of ``kind`` today's cap still allows."""
        return max(0, self._limits.get(kind, 0) - self.used(kind))

    # --- the send path --------------------------------------------------------

    def check(self, kind: Kind) -> None:
        """Refuse before the network call when the budget is spent."""
        if kind == "invite":
            self._check_provider_usage()
        if self.remaining(kind) <= 0:
            raise BudgetExhausted(
                type="local/budget_exhausted",
                title=f"Daily {kind} budget spent ({self._limits.get(kind, 0)})",
                detail="Resume after the next UTC midnight, or raise the cap in .env.",
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
        """Charge ``count`` against today's budget and persist immediately.

        Profile fetches are recorded on any 200, complete or not -- LinkedIn
        counted the fetch either way.
        """
        self._update(lambda counts: counts.update({kind: int(counts.get(kind, 0)) + count}))

    # --- drift correction -----------------------------------------------------

    def reconcile(self, **observed: int) -> None:
        """Replace local counts with totals actually observed at the provider.

        Counts are passed in rather than fetched, so this module stays free of
        any dependency on the resource layer; the caller counts today's real
        sends from ``iter_invitations_sent()`` and ``iter_messages()``.
        """
        self._update(lambda counts: counts.update(observed))

    def note_usage(self, pct: float | None) -> None:
        """Consume LinkedIn's own quota reading, returned on invitations.

        The reading is stored for the rest of the UTC day so that a caller who
        swallows the exception, or simply starts a new process, still cannot
        keep inviting. It is deliberately **not** a permanent flag: LinkedIn
        publishes no reset for this percentage, so a latch that never clears
        would strand the account. Rolling it over with the daily counters keeps
        the batch model intact, and a later, lower reading lifts it immediately.
        """
        if pct is None:
            return
        self._update(lambda counts: counts.update({_USAGE_KEY: pct}))
        if pct >= self._usage_halt_pct:
            raise BudgetExhausted(
                type="local/provider_usage_halt",
                title=f"Provider usage at {pct:g}% of the LinkedIn limit",
                detail="Halting invitations to avoid a restriction.",
            )
        if pct >= self._usage_warn_pct:
            log.warning("Provider usage at %g%% of the LinkedIn limit", pct)

    def _check_provider_usage(self) -> None:
        """Refuse invitations while today's stored usage reading is at the halt
        threshold. Only invitations: the percentage is returned on the
        invitation route and describes that quota, so blocking messages on it
        would over-refuse."""
        pct = self._today_counts().get(_USAGE_KEY)
        if isinstance(pct, (int, float)) and pct >= self._usage_halt_pct:
            raise BudgetExhausted(
                type="local/provider_usage_halt",
                title=f"Provider usage at {pct:g}% of the LinkedIn limit",
                detail="Halting invitations until the next UTC day, or until "
                "LinkedIn reports a lower usage.",
            )

    # --- persistence ----------------------------------------------------------

    def _today(self) -> str:
        return self._clock().astimezone(UTC).date().isoformat()

    def _read(self) -> dict[str, dict[str, dict[str, float]]]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def _today_counts(self) -> dict[str, float]:
        return self._read().get(self._today(), {}).get(self.account_id, {})

    def _update(self, mutate: Callable[[dict[str, float]], None]) -> None:
        today = self._today()
        # Yesterday's counters are dead weight; the file holds one day only.
        by_account = self._read().get(today, {})
        counts = by_account.get(self.account_id, {})
        mutate(counts)
        by_account[self.account_id] = counts
        self._write({today: by_account})

    def _write(self, state: dict[str, dict[str, dict[str, float]]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)
