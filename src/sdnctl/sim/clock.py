"""The event clock: one heapq drives everything; time is integer microseconds."""

import heapq
from collections.abc import Callable

from sdnctl.types import SimTime


class EventClock:
    """IClock over one event queue; events at the same time run in the order they were scheduled."""

    def __init__(self) -> None:
        self._now: SimTime = 0
        self._seq = 0
        self._queue: list[tuple[SimTime, int, Callable[[], None]]] = []

    def now(self) -> SimTime:
        """Current simulated time."""
        return self._now

    def schedule(self, delay: SimTime, fn: Callable[[], None]) -> None:
        """Run fn after delay microseconds (0 runs it after the events already due now)."""
        if delay < 0:
            raise ValueError(f"cannot schedule {delay} us in the past")
        self._seq += 1
        heapq.heappush(self._queue, (self._now + delay, self._seq, fn))

    def run_until(self, until: SimTime) -> None:
        """Run every event due at or before until, then move the clock to until."""
        while self._queue and self._queue[0][0] <= until:
            t, _, fn = heapq.heappop(self._queue)
            self._now = t
            fn()
        self._now = max(self._now, until)

    def pending(self) -> int:
        """Number of events still queued."""
        return len(self._queue)
