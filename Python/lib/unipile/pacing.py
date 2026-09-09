"""How long to wait between calls.

LinkedIn does not only count actions -- it watches their rhythm. A fixed gap, or
a flat random one, is a recognisable machine signature: no person clicks every
8.0 seconds, and no person clicks 250 times without ever stopping for coffee.

So gaps here are drawn from a right-skewed distribution (mostly short, with a
reachable tail) and interrupted by an occasional long break. When LinkedIn does
start withholding data, ``back_off`` stretches every subsequent gap until a
clean response calls ``recovered``.
"""

import logging
import random
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


def humanize(seconds: float) -> str:
    """``95.0`` -> ``1m 35s``. Minutes matter to someone watching a notebook."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}m {rest:02d}s"

#: Beta(2, 5) over the [min, max] range: mode near a fifth of the way up, mean
#: near a third, and the upper bound still reachable. Drawing uniformly instead
#: would put as much weight on the slowest gap as on the most common one.
_SHAPE_A = 2.0
_SHAPE_B = 5.0


class HumanCadence:
    """Randomised pauses between rate-limited calls.

    ``wait`` is the whole interface: it sleeps and returns how long it slept.

    Every bound is required. This module sits below ``config`` and must not read
    it, but a default here would be a second copy of a number ``UnipileSettings``
    already owns -- and the two drifted apart once already. ``rng`` and ``sleep``
    keep their defaults: they are test seams, not policy.
    """

    #: Ceiling on the backoff multiplier, so a long throttled stretch cannot
    #: grow a single gap without bound.
    MAX_BACKOFF = 8.0

    def __init__(
        self,
        min_delay: float,
        max_delay: float,
        *,
        long_pause_every: int,
        long_pause_min: float,
        long_pause_max: float,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_delay = min_delay
        self._max_delay = max_delay
        self._long_pause_every = long_pause_every
        self._long_pause_min = long_pause_min
        self._long_pause_max = long_pause_max
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._backoff = 1.0

    # --- the throttling signal ------------------------------------------------

    def back_off(self) -> None:
        """LinkedIn is withholding data: stretch every gap from here on."""
        self._backoff = min(self._backoff * 2.0, self.MAX_BACKOFF)

    def recovered(self) -> None:
        """A clean response: return to the normal cadence."""
        self._backoff = 1.0

    @property
    def backoff(self) -> float:
        return self._backoff

    # --- waiting --------------------------------------------------------------

    def wait(self) -> float:
        """Sleep for one human-looking interval and return the seconds slept."""
        seconds = self._next_gap()
        self._sleep(seconds)
        return seconds

    def _next_gap(self) -> float:
        on_a_break = self._is_break_due()
        if on_a_break:
            base = self._rng.uniform(self._long_pause_min, self._long_pause_max)
        else:
            spread = self._max_delay - self._min_delay
            base = self._min_delay + spread * self._rng.betavariate(_SHAPE_A, _SHAPE_B)

        seconds = base * self._backoff

        # A multi-minute silence is indistinguishable from a hang, so every wait
        # long enough to worry about says what it is waiting for.
        if self._backoff > 1.0:
            log.warning(
                "LinkedIn is throttling -- waiting %s before the next call "
                "(pace slowed %.0fx).",
                humanize(seconds),
                self._backoff,
            )
        elif on_a_break:
            log.info("Pausing %s to keep a human cadence.", humanize(seconds))

        return seconds

    def _is_break_due(self) -> bool:
        """Breaks arrive at random, averaging one every ``long_pause_every``.

        Drawing each time rather than counting to a fixed number keeps the
        breaks from landing on a predictable stride of their own.
        """
        if self._long_pause_every <= 0:
            return False
        return self._rng.random() < 1.0 / self._long_pause_every
