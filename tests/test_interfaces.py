"""Minimal fakes for every Protocol; mypy checks that each one satisfies its interface."""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sdnctl.interfaces import (
    IClock,
    IDeviceAdapter,
    IncidentOutcome,
    IOcsAdapter,
    IOcsMatrixController,
    IRoutingEngine,
    IScheduler,
    ISimulator,
    ITopologyManager,
    ITraceSink,
    NoRepair,
    RepairPlan,
    SimReport,
)
from sdnctl.messages import (
    CONTROLLER,
    Ack,
    DeviceCommand,
    DeviceEvent,
    GroupSet,
    Header,
    Keepalive,
    LaneDown,
    LaneUp,
    OcsDone,
    OcsSet,
)
from sdnctl.model import CrossConnect, Incident, TopologySpec, TopologyView
from sdnctl.tables import AdjacencyChange, ForwardingReport, TableDiff, Tables, Wave
from sdnctl.types import (
    DeviceId,
    DomainId,
    IncidentState,
    LaneId,
    LaneState,
    ModuleId,
    MsgType,
    OcsPortId,
    SimTime,
    Version,
)
from tests.test_jsonio import tiny_spec


class FakeClock:
    """IClock that only queues callbacks."""

    def __init__(self) -> None:
        self.t: SimTime = 0
        self.queue: list[tuple[SimTime, Callable[[], None]]] = []

    def now(self) -> SimTime:
        return self.t

    def schedule(self, delay: SimTime, fn: Callable[[], None]) -> None:
        self.queue.append((self.t + delay, fn))


class ListTrace:
    """ITraceSink that keeps records in a list."""

    def __init__(self) -> None:
        self.records: list[Mapping[str, Any]] = []

    def record(self, event: Mapping[str, Any]) -> None:
        self.records.append(event)


class RecordingDeviceAdapter:
    """IDeviceAdapter that records what was sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[DeviceId, DeviceCommand]] = []
        self.sink: Callable[[DeviceEvent], None] | None = None

    def send(self, device: DeviceId, cmd: DeviceCommand) -> None:
        self.sent.append((device, cmd))

    def set_event_sink(self, sink: Callable[[DeviceEvent], None]) -> None:
        self.sink = sink


class StaticOcsAdapter:
    """IOcsAdapter whose matrix never changes."""

    def __init__(self, matrix: list[CrossConnect]) -> None:
        self.matrix = matrix
        self.applied: list[OcsSet] = []
        self.sink: Callable[[OcsDone], None] | None = None

    def apply(self, cmd: OcsSet) -> None:
        self.applied.append(cmd)

    def read_matrix(self) -> list[CrossConnect]:
        return list(self.matrix)

    def set_event_sink(self, sink: Callable[[OcsDone], None]) -> None:
        self.sink = sink


class NullOcs:
    """IOcsMatrixController with the OCS off."""

    def enabled(self) -> bool:
        return False

    def plan_repair(self, incident: Incident, view: TopologyView) -> RepairPlan | NoRepair:
        return NoRepair("no_ocs")

    def on_applied(self, cmd: OcsSet, read_back: Sequence[CrossConnect]) -> bool:
        return False


class EmptyRoutingEngine:
    """IRoutingEngine that computes nothing."""

    def compute(self, view: TopologyView) -> Tables:
        return {}

    def diff(self, installed: Tables, target: Tables) -> TableDiff:
        return TableDiff(routes={}, groups={})

    def order_waves(self, diff: TableDiff, change: AdjacencyChange) -> list[Wave]:
        return []

    def check_state(self, tables: Tables, view: TopologyView) -> ForwardingReport:
        return ForwardingReport(looping_pairs=0, blackholed_pairs=0)


class EventLog:
    """IScheduler that logs what it receives."""

    def __init__(self) -> None:
        self.events: list[DeviceEvent | OcsDone | SimTime] = []

    def on_device_event(self, ev: DeviceEvent) -> None:
        self.events.append(ev)

    def on_ocs_event(self, ev: OcsDone) -> None:
        self.events.append(ev)

    def on_timer(self, now: SimTime) -> None:
        self.events.append(now)


class StubView:
    """TopologyView with no state."""

    def __init__(self, spec: TopologySpec) -> None:
        self._spec = spec

    @property
    def spec(self) -> TopologySpec:
        return self._spec

    @property
    def version(self) -> Version:
        return 1

    def lane_state(self, lane: LaneId) -> LaneState:
        return LaneState.ACTIVE

    def xconnects(self) -> tuple[CrossConnect, ...]:
        return ()

    def failed_modules(self) -> frozenset[ModuleId]:
        return frozenset()

    def spare_pool(self) -> frozenset[ModuleId]:
        return frozenset()

    def neighbors(self, device: DeviceId) -> tuple[DeviceId, ...]:
        return ()

    def active_lanes(self, device: DeviceId, neighbor: DeviceId) -> tuple[LaneId, ...]:
        return ()

    def usable_lanes(self, device: DeviceId, neighbor: DeviceId) -> tuple[LaneId, ...]:
        return ()

    def peer_of(self, lane: LaneId) -> LaneId | None:
        return None

    def lane_by_ocs_port(self, port: OcsPortId) -> LaneId | None:
        return None

    def routed(self, device: DeviceId) -> bool:
        return True

    def device_capacity(self, device: DeviceId) -> float:
        return 1.0

    def pair_capacity(self, a: DeviceId, b: DeviceId) -> float:
        return 1.0

    def domain_capacity(self, domain: DomainId) -> float:
        return 1.0


class StubTopologyManager:
    """ITopologyManager that ignores every report."""

    def __init__(self, view: StubView) -> None:
        self._view = view

    def load(self, spec: TopologySpec) -> None:
        self._view = StubView(spec)

    def on_lane_down(self, msg: LaneDown, now: SimTime) -> None:
        return None

    def on_lane_up(self, msg: LaneUp, now: SimTime) -> None:
        return None

    def on_ocs_done(self, msg: OcsDone, matrix: Sequence[CrossConnect]) -> None:
        return None

    def poll_incidents(self, now: SimTime) -> list[Incident]:
        return []

    def correlation_deadline(self) -> SimTime | None:
        return None

    def on_groups_installed(self, device: DeviceId, groups: GroupSet, now: SimTime) -> None:
        return None

    def record_repair(self, plan: RepairPlan, now: SimTime) -> None:
        return None

    def view(self) -> TopologyView:
        return self._view

    def version(self) -> Version:
        return self._view.version


class StubSimulator:
    """ISimulator that reports an empty run."""

    def __init__(self, spec: TopologySpec) -> None:
        self._spec = spec

    def load_topology(self, name: str, two_plus_two: bool, spares: int = 0) -> TopologySpec:
        return self._spec

    def configure(self, ocs: bool, timing: str = "typical") -> None:
        return None

    def inject(self, t_ms: int, event: str, **args: Any) -> None:
        return None

    def run(self, until_ms: int) -> None:
        return None

    def report(self) -> SimReport:
        return SimReport(
            trace=(),
            capacity=(),
            messages={"LANE_DOWN": 2},
            incidents=(IncidentOutcome(1, IncidentState.DEGRADED_NO_SPARE, 2000),),
        )


def test_fakes_satisfy_protocols() -> None:
    spec = tiny_spec()
    view = StubView(spec)
    clock: IClock = FakeClock()
    trace: ITraceSink = ListTrace()
    devices: IDeviceAdapter = RecordingDeviceAdapter()
    ocs: IOcsAdapter = StaticOcsAdapter(list(spec.xconnects))
    ocs_ctl: IOcsMatrixController = NullOcs()
    engine: IRoutingEngine = EmptyRoutingEngine()
    scheduler: IScheduler = EventLog()
    topo: ITopologyManager = StubTopologyManager(view)
    sim: ISimulator = StubSimulator(spec)

    clock.schedule(5, lambda: None)
    trace.record({"event": "x"})
    devices.set_event_sink(scheduler.on_device_event)
    ocs.set_event_sink(scheduler.on_ocs_event)
    keepalive = Keepalive(Header(MsgType.KEEPALIVE, 1, 1, None, CONTROLLER, "npu-0", 0), "c", 1)
    devices.send("npu-0", DeviceCommand(keepalive=keepalive))
    scheduler.on_device_event(Ack(Header(MsgType.ACK, 2, 1, None, "npu-0", CONTROLLER, 0), 1, True))

    assert ocs.read_matrix() == list(spec.xconnects)
    assert not ocs_ctl.enabled()
    assert engine.compute(topo.view()) == {}
    assert topo.version() == 1
    assert sim.load_topology("tiny", True) is spec
    assert sim.report().messages_total() == 2
