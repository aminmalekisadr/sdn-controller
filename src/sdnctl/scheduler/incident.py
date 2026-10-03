"""One incident as the scheduler runs it: its plan, its progress flags and its timers."""

from dataclasses import dataclass

from sdnctl.interfaces import NoRepair, RepairPlan
from sdnctl.messages import OcsSet
from sdnctl.model import Incident
from sdnctl.types import DeviceId, IncidentState, LaneId, SimTime

PARKED = frozenset({IncidentState.DEGRADED_WAITING_REPAIR, IncidentState.DEGRADED_NO_SPARE})
FINAL = frozenset({IncidentState.CLOSED, IncidentState.DEGRADED,
                   IncidentState.FAILED_NEEDS_OPERATOR})


@dataclass
class IncidentRun:
    """The scheduler's working state for one incident."""

    incident: Incident
    touched: frozenset[tuple[DeviceId, DeviceId]] = frozenset()  # (device, peer) of its lanes
    plan: RepairPlan | None = None
    no_repair: NoRepair | None = None
    rerouting: bool = False
    ocs_cmd: OcsSet | None = None
    ocs_sends: int = 0
    ocs_applied: bool = False
    new_lanes: frozenset[LaneId] = frozenset()  # must be up at both ends before activation
    activating: bool = False
    ready: bool = False  # parked, its lanes are back, waiting for the active slot
    changed_at: SimTime | None = None  # when it last entered a parked or final state
    timer: int = 0  # bumped to cancel pending OCS and verify timers

    @property
    def state(self) -> IncidentState:
        """The incident's current state."""
        return self.incident.state

    def finished(self) -> bool:
        """True once the incident reached a final state."""
        return self.incident.state in FINAL

    def parked(self) -> bool:
        """True while it waits for a repair (DEGRADED_WAITING_REPAIR or DEGRADED_NO_SPARE)."""
        return self.incident.state in PARKED
