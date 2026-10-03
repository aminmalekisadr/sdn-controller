"""The interfaces between the controller modules, the adapters and the simulator (SPEC 9.4).

Ownership rules (SPEC 7.1): the scheduler is the only component that calls an adapter. The
Topology Manager only listens and keeps state, the Routing Engine is pure computation, and the
OCS Matrix Controller only plans.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from sdnctl.config import ControllerConfig
from sdnctl.messages import (
    DeviceCommand,
    DeviceEvent,
    GroupSet,
    LaneDown,
    LaneUp,
    OcsDone,
    OcsSet,
    Prepare,
)
from sdnctl.model import Incident, TopologySpec, TopologyView
from sdnctl.tables import AdjacencyChange, ForwardingReport, TableDiff, Tables, Wave
from sdnctl.types import (
    CrossConnect,
    DeviceId,
    IncidentId,
    IncidentState,
    ModuleId,
    SimTime,
    Version,
)


@dataclass(frozen=True)
class RepairPlan:
    """How the OCS restores one incident: spare module, OCS changes and devices to prepare."""

    failed_device: DeviceId
    failed_modules: tuple[ModuleId, ...]
    spare_module: ModuleId
    disconnect: tuple[CrossConnect, ...]
    connect: tuple[CrossConnect, ...]
    targets: frozenset[DeviceId]
    prepares: Mapping[DeviceId, Prepare]


@dataclass(frozen=True)
class NoRepair:
    """Why no OCS repair is possible for an incident."""

    REASONS: ClassVar[tuple[str, ...]] = (
        "no_ocs",
        "no_spare",
        "not_enough_spare",
        "failed_end_unclear",
    )
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in self.REASONS:
            raise ValueError(f"unknown NoRepair reason {self.reason!r}")


@dataclass(frozen=True)
class CapacitySample:
    """One capacity reading on the simulated devices."""

    time: SimTime
    key: str  # a domain "l1-1536.d0", a pair "l1-1536/l2-2304" or a device "l1-1536"
    value: float


@dataclass(frozen=True)
class IncidentOutcome:
    """How one incident ended."""

    incident: IncidentId
    state: IncidentState
    ended_at: SimTime | None  # None while the incident is still open
    reason: str = ""


@dataclass(frozen=True)
class SimReport:
    """Result of a simulator run: trace, capacity over time, message counts, incident end states."""

    trace: tuple[Mapping[str, Any], ...]
    capacity: tuple[CapacitySample, ...]
    messages: Mapping[str, int]  # counted messages by type (SPEC 11.0, "Counting messages")
    incidents: tuple[IncidentOutcome, ...]

    def messages_total(self) -> int:
        """Total number of counted messages."""
        return sum(self.messages.values())


class IClock(Protocol):
    """Simulated time and the event queue."""

    def now(self) -> SimTime:
        """Current simulated time."""

    def schedule(self, delay: SimTime, fn: Callable[[], None]) -> None:
        """Run fn after delay microseconds."""


class ITraceSink(Protocol):
    """Receives every message and state change as one JSON-lines record."""

    def record(self, event: Mapping[str, Any]) -> None:
        """Append one trace record."""


class IDeviceAdapter(Protocol):
    """Southbound to the device agents (UBMs)."""

    def send(self, device: DeviceId, cmd: DeviceCommand) -> None:
        """Send PREPARE, GROUP_SET, ROUTE_SET (or a batch), or KEEPALIVE to a device."""

    def set_event_sink(self, sink: Callable[[DeviceEvent], None]) -> None:
        """Where LANE_DOWN, LANE_UP and ACK from devices are delivered."""


class IOcsAdapter(Protocol):
    """Southbound to the OCS."""

    def apply(self, cmd: OcsSet) -> None:
        """Send OCS_SET."""

    def read_matrix(self) -> list[CrossConnect]:
        """Read the current cross-connects back; an adapter call, not a counted message."""

    def set_event_sink(self, sink: Callable[[OcsDone], None]) -> None:
        """Where OCS_DONE is delivered."""


class ITopologyManager(Protocol):
    """Single source of truth for lane, module and capacity state; creates incidents."""

    def load(self, spec: TopologySpec) -> None:
        """Load a topology; every working lane starts ACTIVE, every spare lane IDLE."""

    def on_lane_down(self, msg: LaneDown, now: SimTime) -> None:
        """Apply a LANE_DOWN report and feed incident correlation."""

    def on_lane_up(self, msg: LaneUp, now: SimTime) -> None:
        """Apply a LANE_UP report."""

    def on_ocs_done(self, msg: OcsDone, matrix: Sequence[CrossConnect]) -> None:
        """Apply an OCS result and the matrix read back."""

    def poll_incidents(self, now: SimTime) -> list[Incident]:
        """Close due correlation windows and return the new incidents."""

    def correlation_deadline(self) -> SimTime | None:
        """When poll_incidents() will next return an incident, or None if no window is open."""

    def on_groups_installed(self, device: DeviceId, groups: GroupSet, now: SimTime) -> None:
        """A device applied a GROUP_SET; its lanes become ACTIVE."""

    def record_repair(self, plan: RepairPlan, now: SimTime) -> None:
        """Mark the failed modules FAILED and take the used spare out of the pool."""

    def view(self) -> TopologyView:
        """Read-only snapshot of the current state."""

    def version(self) -> Version:
        """Current topology version."""


class IRoutingEngine(Protocol):
    """Pure computation of tables, diffs, wave order and forwarding checks."""

    def compute(self, view: TopologyView) -> Tables:
        """Target route and group tables for every device, from usable lanes."""

    def diff(self, installed: Tables, target: Tables) -> TableDiff:
        """Only the entries that differ between installed and target tables."""

    def order_waves(self, diff: TableDiff, change: AdjacencyChange) -> list[Wave]:
        """Safe install order: transit first when a neighbor is lost, endpoints first when back."""

    def check_state(self, tables: Tables, view: TopologyView) -> ForwardingReport:
        """Looping and blackholed NPU pairs; tables may mix old and new per device."""


class IOcsMatrixController(Protocol):
    """Plans OCS repairs and tracks the matrix; never sends commands."""

    def enabled(self) -> bool:
        """False for the null controller (OCS off)."""

    def plan_repair(self, incident: Incident, view: TopologyView) -> RepairPlan | NoRepair:
        """Pick a spare and the OCS changes for an incident, or say why there is none."""

    def on_applied(self, cmd: OcsSet, read_back: Sequence[CrossConnect]) -> bool:
        """Take the read-back as the matrix; True if it shows cmd fully applied."""


class IScheduler(Protocol):
    """Runs incidents and is the only sender of commands."""

    def on_device_event(self, ev: DeviceEvent) -> None:
        """LANE_DOWN, LANE_UP or ACK from a device."""

    def on_ocs_event(self, ev: OcsDone) -> None:
        """OCS_DONE from the OCS."""

    def on_timer(self, now: SimTime) -> None:
        """Timeouts and correlation windows."""


@dataclass(frozen=True)
class ControllerContext:
    """What a host (the simulator now; ns-3 or hardware later) hands to the controller."""

    spec: TopologySpec
    config: ControllerConfig
    clock: IClock
    devices: IDeviceAdapter
    ocs: IOcsAdapter | None  # None when the topology has no OCS
    trace: ITraceSink


class HostedController(Protocol):
    """A controller a host can run; it registers its own event sinks and timers."""

    def initial_tables(self) -> Tables:
        """Tables to install on the devices at version 1 (the startup install is not counted)."""

    def outcomes(self) -> tuple[IncidentOutcome, ...]:
        """Each incident's end state, or its state so far."""


ControllerFactory = Callable[[ControllerContext], HostedController]


class ISimulator(Protocol):
    """The simulator API of SPEC Section 7.6."""

    def load_topology(self, name: str, two_plus_two: bool, spares: int = 0) -> TopologySpec:
        """Build topology 1 or 2 and its devices, and install the steady-state tables."""

    def configure(self, ocs: bool, timing: str = "typical") -> None:
        """OCS controller on or off; typical or worst timing profile."""

    def inject(self, t_ms: int, event: str, **args: Any) -> None:
        """Schedule module_fail, repair, drop_ack or ocs_never_answers at t_ms."""

    def run(self, until_ms: int) -> None:
        """Run devices, OCS and controller until until_ms."""

    def report(self) -> SimReport:
        """Trace, capacity over time, message counts and incident end states."""
