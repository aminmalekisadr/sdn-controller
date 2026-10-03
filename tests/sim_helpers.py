"""Test helpers for the simulator: a hosted controller that records events, and trace queries."""

from collections.abc import Callable
from typing import Any

from sdnctl.interfaces import IncidentOutcome, SimReport
from sdnctl.messages import (
    CONTROLLER,
    OCS,
    Ack,
    DeviceCommand,
    DeviceEvent,
    GroupSet,
    Header,
    LaneDown,
    LaneUp,
    OcsDone,
)
from sdnctl.model import Incident
from sdnctl.sim import ControllerContext, Simulator
from sdnctl.sim.simulator import topology_tables
from sdnctl.tables import RouteEntry, Tables
from sdnctl.topology.view import SpecIndex
from sdnctl.topology_manager import TopologyManager
from sdnctl.types import DeviceId, MsgType


class CaptureController:
    """A hosted controller that only records what reaches it; tests send commands through ctx."""

    def __init__(
        self, ctx: ControllerContext, routes: dict[DeviceId, list[RouteEntry]] | None = None
    ) -> None:
        self.ctx = ctx
        self.events: list[tuple[int, DeviceEvent | OcsDone]] = []
        self.on_event: Callable[[DeviceEvent | OcsDone], None] = lambda ev: None
        self._routes = routes or {}
        self._seq = 0
        ctx.devices.set_event_sink(self._record)
        if ctx.ocs is not None:
            ctx.ocs.set_event_sink(self._record)

    def initial_tables(self) -> Tables:
        """Groups from the topology, plus any routes the test asked for."""
        tables = topology_tables(self.ctx.spec, SpecIndex(self.ctx.spec))
        for d, entries in self._routes.items():
            tables[d].routes.update({e.dest: e for e in entries})
        return tables

    def outcomes(self) -> tuple[IncidentOutcome, ...]:
        """No incidents: this controller never decides anything."""
        return ()

    def header(self, msg_type: MsgType, dst: str) -> Header:
        """A controller header for a message sent now."""
        self._seq += 1
        return Header(msg_type, self._seq, 2, 1, CONTROLLER, dst, self.ctx.clock.now())

    def ocs_header(self) -> Header:
        """A controller header for an OCS_SET sent now."""
        return self.header(MsgType.OCS_SET, OCS)

    def received(self, msg_type: MsgType) -> list[tuple[int, Any]]:
        """(time, message) of each received message of one type."""
        return [(t, ev) for t, ev in self.events if ev.header.type is msg_type]

    def _record(self, ev: DeviceEvent | OcsDone) -> None:
        self.events.append((self.ctx.clock.now(), ev))
        self.on_event(ev)


class TmController(CaptureController):
    """A CaptureController that feeds a real TopologyManager and polls its incidents on time."""

    def __init__(
        self, ctx: ControllerContext, routes: dict[DeviceId, list[RouteEntry]] | None = None
    ) -> None:
        super().__init__(ctx, routes)
        self.tm = TopologyManager(ctx.config.timing().correlation_window, ctx.trace)
        self.tm.load(ctx.spec)
        self.incidents: list[tuple[int, Incident]] = []  # (time handed over, incident)
        self.on_incident: Callable[[Incident], None] = lambda inc: None
        self._groups_in_flight: dict[int, tuple[DeviceId, GroupSet]] = {}

    def send_groups(self, device: DeviceId, entries: dict[DeviceId, tuple[str, ...]]) -> None:
        """Send a GROUP_SET; the TM hears about it when the ACK comes back ok."""
        msg = GroupSet(self.header(MsgType.GROUP_SET, device), entries)
        self._groups_in_flight[msg.header.seq] = (device, msg)
        self.ctx.devices.send(device, DeviceCommand(group_set=msg))

    def _record(self, ev: DeviceEvent | OcsDone) -> None:
        now = self.ctx.clock.now()
        if isinstance(ev, LaneDown):
            self.tm.on_lane_down(ev, now)
            deadline = self.tm.correlation_deadline()
            if deadline is not None:
                self.ctx.clock.schedule(deadline - now, self._poll)
        elif isinstance(ev, LaneUp):
            self.tm.on_lane_up(ev, now)
        elif isinstance(ev, Ack):
            sent = self._groups_in_flight.pop(ev.acked_seq, None)
            if sent is not None and ev.ok:
                self.tm.on_groups_installed(sent[0], sent[1], now)
        elif isinstance(ev, OcsDone):
            assert self.ctx.ocs is not None
            self.tm.on_ocs_done(ev, self.ctx.ocs.read_matrix())
        super()._record(ev)

    def _poll(self) -> None:
        now = self.ctx.clock.now()
        for incident in self.tm.poll_incidents(now):
            self.incidents.append((now, incident))
            self.on_incident(incident)


def captured(
    sim_routes: dict[DeviceId, list[RouteEntry]] | None = None,
    cls: type[CaptureController] = CaptureController,
) -> tuple[Simulator, list[Any]]:
    """A simulator hosting a cls controller; the list fills in when the run starts."""
    made: list[Any] = []

    def factory(ctx: ControllerContext) -> CaptureController:
        ctl = cls(ctx, sim_routes)
        made.append(ctl)
        return ctl

    return Simulator(controller=factory), made


def times(report: SimReport, kind: str, **match: Any) -> list[int]:
    """Times (us) of trace records of one kind whose fields equal match."""
    return [
        r["t"]
        for r in report.trace
        if r["kind"] == kind and all(r.get(k) == v for k, v in match.items())
    ]


def ms(t_us: int) -> int:
    """Microseconds to whole milliseconds; fails if t_us is not a whole millisecond."""
    assert t_us % 1000 == 0, t_us
    return t_us // 1000
