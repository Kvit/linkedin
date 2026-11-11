"""The single `runtime_state/linkedin` Firestore document that coordinates
everything cross-cutting about sending and fetching: the lease that keeps two
scheduled tick jobs from sending at once, a human's pause and writes-blocked
controls, a webhook's request for an out-of-band sync, the `require_approval`
override, and (task 3a) an independent pause and back-off for task 3b's
profile fetches. One document because all of it is read together at the top
of every tick, and every field is optional because most of the time none of
it is set.

Every write here is `ref.set({...}, merge=True)` -- never a bare `set()` --
because this document is shared: a method that wrote without `merge=True`
would erase every field owned by every OTHER method the moment it ran. Every
plain read is one `ref.get()`; `read()` is the single place that happens, and
every other read-only method goes through it, so an absent document reads as
`{}` everywhere rather than raising.

`acquire_tick_lease`, `release_tick_lease`, `clear_sync_request` and
`note_fetch_throttled` are the methods where a lost race would actually
matter -- two ticks both believing they hold the lease, a sync request
cleared out from under a webhook that asked again while the first sync was
still running, or two concurrent throttle reports both computing the same
back-off from a `consecutive_throttled` neither has yet seen the other
increment -- so those are the real `google.cloud.firestore.transactional`
decorator wrapped around a read-then-write of this one document, not a plain
read followed by a plain write. `google.cloud.firestore` is imported inside
each of those methods rather than at module level, so importing `state`
itself stays free.
"""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from linkedinmcp import clock

STATE_COLLECTION = "runtime_state"
STATE_DOCUMENT = "linkedin"

#: How long the tick lease is held (ruling P5-4): by `send_messages` for one
#: message at a time, by `get_contacts` for one profile, and by a tick run by
#: hand. Each releases it as soon as its one LinkedIn write is done. A lease
#: left behind by a job that died expires after 225 seconds; until then the
#: next `send_messages` or `get_contacts` stops as `tick_busy`. It leaves
#: `jobs.LEASE_FLOOR_SECONDS` for the write after a sweep, a requested sync
#: and the chat check.
LEASE_SECONDS = 225


class RuntimeState:
    """Wraps the one `runtime_state/linkedin` document. Building an instance
    performs no I/O -- `db.collection(...).document(...)` only constructs a
    reference, in both the real client and `FakeFirestore`.
    """

    def __init__(self, db, clock: Callable[[], datetime] = clock.utcnow) -> None:
        self._db = db
        self._clock = clock
        self._ref = db.collection(STATE_COLLECTION).document(STATE_DOCUMENT)

    def read(self) -> dict:
        """The document's fields, or `{}` when it does not exist."""
        data = self._ref.get().to_dict()
        return data if data is not None else {}

    def now(self) -> datetime:
        """This state's own clock reading: the time every pause, the lease
        and the writes-blocked stamp are compared with or stamped by. A job
        that started at `now` may be minutes further on by this clock."""
        return self._clock()

    # --- tick lease --------------------------------------------------------

    def acquire_tick_lease(self, seconds: int = LEASE_SECONDS) -> str | None:
        """Claim the tick lease for `seconds`, or return `None` if another
        tick already holds an unexpired one.

        One transaction: read the document, and when no lease is stored or
        its `tick_lease_until` is already at or before `clock()`, write a
        fresh `tick_lease_owner` (a uuid4 hex) and `tick_lease_until`. Nothing
        outside the transaction observes any effect of a losing attempt --
        the owner and the expiry are both computed from values local to this
        call, so the library retrying the wrapped function on contention
        (see `FakeFirestore.contend_once`) is safe.
        """
        from google.cloud import firestore

        @firestore.transactional
        def _acquire(transaction):
            now = self._clock()
            snapshot = self._ref.get(transaction=transaction)
            data = snapshot.to_dict() or {}
            until = data.get("tick_lease_until")
            if until is not None and until > now:
                return None
            owner = uuid.uuid4().hex
            transaction.set(
                self._ref,
                {"tick_lease_owner": owner, "tick_lease_until": now + timedelta(seconds=seconds)},
                merge=True,
            )
            return owner

        return _acquire(self._db.transaction())

    def release_tick_lease(self, owner: str) -> bool:
        """Clear the lease, only when `owner` is the one currently holding
        it. Returns whether it cleared.
        """
        from google.cloud import firestore

        @firestore.transactional
        def _release(transaction):
            snapshot = self._ref.get(transaction=transaction)
            data = snapshot.to_dict() or {}
            if data.get("tick_lease_owner") != owner:
                return False
            transaction.set(
                self._ref,
                {"tick_lease_owner": None, "tick_lease_until": None},
                merge=True,
            )
            return True

        return _release(self._db.transaction())

    def lease_remaining(self, owner: str) -> float:
        """Seconds until the lease `owner` holds expires, or `0.0` when
        `owner` does not hold an unexpired lease -- including when nothing is
        stored, when a different owner holds it, and after expiry.
        """
        data = self.read()
        if data.get("tick_lease_owner") != owner:
            return 0.0
        until = data.get("tick_lease_until")
        if until is None:
            return 0.0
        remaining = (until - self._clock()).total_seconds()
        return remaining if remaining > 0 else 0.0

    # --- sends pause ---------------------------------------------------------

    def pause_sends(self, until: datetime, reason: str) -> None:
        """No sends before `until`. Raises `ValueError` for a naive `until`
        -- the same "never guess a timezone" rule as `clock.local_date`.
        """
        if until.tzinfo is None:
            raise ValueError("pause_sends: `until` must be timezone-aware")
        self._ref.set({"sends_paused_until": until, "pause_reason": reason}, merge=True)

    def resume_sends(self) -> None:
        self._ref.set({"sends_paused_until": None, "pause_reason": None}, merge=True)

    def sends_paused_until(self) -> datetime | None:
        """The stored pause, when it is still in the future; `None` when
        nothing is stored or the stored value has already passed.
        """
        until = self.read().get("sends_paused_until")
        return until if until is not None and until > self._clock() else None

    # --- fetch pause -----------------------------------------------------

    def pause_fetches(self, until: datetime, reason: str) -> None:
        """No profile fetches (task 3b) before `until`. Raises `ValueError`
        for a naive `until` -- the same rule as `pause_sends`.
        """
        if until.tzinfo is None:
            raise ValueError("pause_fetches: `until` must be timezone-aware")
        self._ref.set({"fetches_paused_until": until, "fetch_pause_reason": reason}, merge=True)

    def resume_fetches(self) -> None:
        self._ref.set({"fetches_paused_until": None, "fetch_pause_reason": None}, merge=True)

    def fetches_paused_until(self) -> datetime | None:
        """The stored fetch pause, when it is still in the future; `None`
        when nothing is stored or the stored value has already passed --
        the same rule as `sends_paused_until`.
        """
        until = self.read().get("fetches_paused_until")
        return until if until is not None and until > self._clock() else None

    def note_fetch_throttled(self) -> datetime:
        """LinkedIn withheld a profile's sections again: one transaction
        that reads `consecutive_throttled`, increments it to `n`, and pauses
        fetches until `clock() + min(24 h, 30 min * 2 ** (n - 1))` -- 30 min,
        1 h, 2 h, 4 h, ... doubling, capped at 24 h. Stores
        `consecutive_throttled = n`, `fetches_paused_until`, and
        `fetch_pause_reason = f"LinkedIn withheld profile sections ({n} in a
        row)"`. Returns the `until` it computed.
        """
        from google.cloud import firestore

        @firestore.transactional
        def _note(transaction):
            now = self._clock()
            snapshot = self._ref.get(transaction=transaction)
            data = snapshot.to_dict() or {}
            n = (data.get("consecutive_throttled") or 0) + 1
            until = now + min(timedelta(hours=24), timedelta(minutes=30) * (2 ** (n - 1)))
            transaction.set(
                self._ref,
                {
                    "consecutive_throttled": n,
                    "fetches_paused_until": until,
                    "fetch_pause_reason": f"LinkedIn withheld profile sections ({n} in a row)",
                },
                merge=True,
            )
            return until

        return _note(self._db.transaction())

    def note_fetch_ok(self) -> None:
        """A profile fetch went cleanly: reset the consecutive-throttle
        count so the NEXT throttle starts back at 30 minutes rather than
        continuing to escalate from a stale count. Does not itself resume a
        pause already in effect -- that is `resume_fetches`' job.
        """
        self._ref.set({"consecutive_throttled": 0}, merge=True)

    # --- writes-blocked --------------------------------------------------

    def block_writes(self, reason: str) -> None:
        self._ref.set(
            {"writes_blocked_at": self._clock(), "writes_blocked_reason": reason}, merge=True
        )

    def unblock_writes(self) -> None:
        self._ref.set({"writes_blocked_at": None, "writes_blocked_reason": None}, merge=True)

    def writes_blocked(self) -> bool:
        return self.read().get("writes_blocked_at") is not None

    # --- sync request ------------------------------------------------------

    def request_sync(self) -> None:
        self._ref.set({"sync_requested_at": self._clock()}, merge=True)

    def sync_requested(self) -> datetime | None:
        """The stored request time, or `None` when there is none."""
        return self.read().get("sync_requested_at")

    def clear_sync_request(self, seen: datetime) -> bool:
        """Clear the sync request, only when it was made at or before
        `seen`. Returns whether it cleared.

        A webhook asking again while the requested sync is already running
        stores a NEW `sync_requested_at`, after `seen` -- the sweep that
        finishes that run must not clear a request it has not actually
        served yet, or the new request would be silently dropped.
        """
        from google.cloud import firestore

        @firestore.transactional
        def _clear(transaction):
            snapshot = self._ref.get(transaction=transaction)
            data = snapshot.to_dict() or {}
            requested_at = data.get("sync_requested_at")
            if requested_at is None or requested_at > seen:
                return False
            transaction.set(self._ref, {"sync_requested_at": None}, merge=True)
            return True

        return _clear(self._db.transaction())

    # --- require_approval override ------------------------------------------

    def require_approval(self, default: bool) -> bool:
        """The stored override when present, else `default`."""
        value = self.read().get("require_approval")
        return value if value is not None else default

    def set_require_approval(self, value: bool) -> None:
        self._ref.set({"require_approval": bool(value)}, merge=True)

    # --- budget snapshot -----------------------------------------------------

    def budget_snapshot(self) -> tuple[int, datetime] | None:
        """`(messages_24h, taken_at)` when both are stored, else `None`."""
        data = self.read()
        messages_24h = data.get("budget_messages_24h")
        taken_at = data.get("budget_taken_at")
        if messages_24h is None or taken_at is None:
            return None
        return (messages_24h, taken_at)

    def store_budget_snapshot(self, messages_24h: int, taken_at: datetime) -> None:
        self._ref.set(
            {"budget_messages_24h": messages_24h, "budget_taken_at": taken_at}, merge=True
        )
