"""Incident correlation (SPEC 11.0 rule 2): many LANE_DOWN reports, one incident.

The first report opens a window. If any report so far is MODULE_FAULT, the window closes at once,
but reports that arrive at the same time still join (the caller polls after all of them). With no
MODULE_FAULT, the window closes correlation_window after the first report.
"""

from dataclasses import dataclass, field

from sdnctl.messages import LaneDown
from sdnctl.types import DownCause, SimTime


@dataclass
class _Window:
    opened_at: SimTime
    closes_at: SimTime
    reports: list[LaneDown] = field(default_factory=list)


class IncidentCorrelator:
    """Collects reports into one open window at a time."""

    def __init__(self, window: SimTime) -> None:
        self._window = window
        self._open: _Window | None = None

    def add(self, msg: LaneDown, now: SimTime) -> None:
        """Add a report that arrived at now."""
        if self._open is None:
            self._open = _Window(opened_at=now, closes_at=now + self._window)
        if msg.cause is DownCause.MODULE_FAULT:
            self._open.closes_at = min(self._open.closes_at, now)
        self._open.reports.append(msg)

    def deadline(self) -> SimTime | None:
        """When the open window closes, or None if none is open."""
        return self._open.closes_at if self._open is not None else None

    def take(self, now: SimTime) -> tuple[SimTime, list[LaneDown]] | None:
        """If the window has closed by now: (opened_at, reports), and the window is cleared."""
        if self._open is None or self._open.closes_at > now:
            return None
        window, self._open = self._open, None
        return window.opened_at, window.reports
