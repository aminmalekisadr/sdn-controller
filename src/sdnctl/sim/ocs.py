"""SimOcs: the optical circuit switch model (SPEC 11.0 timing rule 5).

An OCS_SET is applied ocs_command after it is sent: disconnects first, then connects. A
disconnected path goes dark at once. The mirrors of new cross-connects settle mirror_move later;
only then is the path physically there, and OCS_DONE goes out at that moment.
"""

import functools
from collections.abc import Callable, Iterable

from sdnctl.config import Timing
from sdnctl.messages import CONTROLLER, OCS, Header, OcsDone, OcsPairResult, OcsSet
from sdnctl.sim.clock import EventClock
from sdnctl.sim.trace import Tracer
from sdnctl.types import CrossConnect, MsgType, OcsPortId


def _port_number(port: OcsPortId) -> int:
    return int(port[1:])


class SimOcs:
    """The OCS: its matrix, the settled light paths and the never-answers test fault."""

    def __init__(
        self,
        ports: Iterable[OcsPortId],
        xconnects: Iterable[CrossConnect],
        clock: EventClock,
        timing: Timing,
        tracer: Tracer,
    ) -> None:
        self._ports = frozenset(ports)
        self._clock = clock
        self._timing = timing
        self._tracer = tracer
        self.matrix: dict[OcsPortId, OcsPortId] = {}  # North -> South, as applied
        self._south_used: dict[OcsPortId, OcsPortId] = {}
        self.settled: dict[OcsPortId, OcsPortId] = {}  # both directions; light can pass
        for xc in xconnects:
            self.matrix[xc.north] = xc.south
            self._south_used[xc.south] = xc.north
            self.settled[xc.north] = xc.south
            self.settled[xc.south] = xc.north
        self.matrix_version = 1
        self.never_answers = False
        self._seq = 0
        self.on_disconnect: Callable[[list[CrossConnect]], None] = lambda xcs: None
        self.on_settled: Callable[[list[CrossConnect]], None] = lambda xcs: None
        self.send_done: Callable[[OcsDone], None] = lambda msg: None

    def receive(self, cmd: OcsSet) -> None:
        """An OCS_SET arrives; it is applied ocs_command later, unless the OCS never answers."""
        if self.never_answers:
            self._tracer.emit("ocs_ignored", seq=cmd.header.seq)
            return
        self._clock.schedule(self._timing.ocs_command, functools.partial(self._apply, cmd))

    def read_matrix(self) -> list[CrossConnect]:
        """The applied cross-connects, by North port number."""
        return [
            CrossConnect(n, s)
            for n, s in sorted(self.matrix.items(), key=lambda kv: _port_number(kv[0]))
        ]

    def _apply(self, cmd: OcsSet) -> None:
        disconnected: list[OcsPairResult] = []
        removed: list[CrossConnect] = []
        for xc in cmd.disconnect:
            if self.matrix.get(xc.north) == xc.south:
                del self.matrix[xc.north]
                del self._south_used[xc.south]
                self.settled.pop(xc.north, None)
                self.settled.pop(xc.south, None)
                removed.append(xc)
                disconnected.append(OcsPairResult(xc, ok=True))
            else:
                disconnected.append(OcsPairResult(xc, ok=False, error="not connected"))
        connected: list[OcsPairResult] = []
        added: list[CrossConnect] = []
        for xc in cmd.connect:
            error = ""
            if xc.north not in self._ports or xc.south not in self._ports:
                error = "unknown port"
            elif xc.north in self.matrix or xc.south in self._south_used:
                error = "port busy"
            if error:
                connected.append(OcsPairResult(xc, ok=False, error=error))
                continue
            self.matrix[xc.north] = xc.south
            self._south_used[xc.south] = xc.north
            added.append(xc)
            connected.append(OcsPairResult(xc, ok=True))
        self.matrix_version += 1
        self._tracer.emit(
            "ocs_apply",
            seq=cmd.header.seq,
            matrix_version=self.matrix_version,
            removed=[[xc.north, xc.south] for xc in removed],
            added=[[xc.north, xc.south] for xc in added],
        )
        if removed:
            self.on_disconnect(removed)
        self._clock.schedule(
            self._timing.mirror_move,
            functools.partial(self._settle, cmd, added, tuple(disconnected), tuple(connected)),
        )

    def _settle(
        self,
        cmd: OcsSet,
        added: list[CrossConnect],
        disconnected: tuple[OcsPairResult, ...],
        connected: tuple[OcsPairResult, ...],
    ) -> None:
        settled = [xc for xc in added if self.matrix.get(xc.north) == xc.south]
        for xc in settled:
            self.settled[xc.north] = xc.south
            self.settled[xc.south] = xc.north
        self._tracer.emit("ocs_settle", added=[[xc.north, xc.south] for xc in settled])
        if settled:
            self.on_settled(settled)
        self._seq += 1
        header = Header(
            MsgType.OCS_DONE,
            self._seq,
            cmd.header.version,
            cmd.header.incident,
            OCS,
            CONTROLLER,
            self._clock.now(),
        )
        self.send_done(OcsDone(header, disconnected, connected, self.matrix_version))
