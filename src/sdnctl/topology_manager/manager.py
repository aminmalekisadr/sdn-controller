"""The Topology Manager (SPEC 7.1): the single source of truth. It only listens and keeps state.

It holds every lane's state, the current cross-connects, the verified lanes, failed modules and
the spare pool; it turns LANE_DOWN reports into incidents; and it hands out read-only, versioned
snapshots (TopologySnapshot). Every state change bumps the version and goes to the trace.
"""

from collections.abc import Sequence
from typing import Any

from sdnctl.interfaces import ITraceSink, RepairPlan
from sdnctl.messages import GroupSet, LaneDown, LaneUp, OcsDone
from sdnctl.model import Incident, TopologySpec
from sdnctl.topology.view import SpecIndex, TopologySnapshot, steady_lane_states
from sdnctl.topology_manager.correlator import IncidentCorrelator
from sdnctl.topology_manager.lanes import LaneStateError, check_transition
from sdnctl.types import (
    CrossConnect,
    DeviceId,
    DownCause,
    IncidentId,
    IncidentState,
    LaneId,
    LaneState,
    ModuleId,
    SimTime,
    Version,
    device_key,
)

_LIVE = (LaneState.ACTIVE, LaneState.VERIFYING, LaneState.CONNECTING)


class TopologyManager:
    """ITopologyManager: lane, module, spare and capacity state, and incident correlation."""

    def __init__(self, correlation_window: SimTime, trace: ITraceSink | None = None) -> None:
        self._correlation_window = correlation_window
        self._trace = trace
        self._index: SpecIndex | None = None

    def load(self, spec: TopologySpec) -> None:
        """Load a topology; every working lane starts ACTIVE, every spare lane IDLE."""
        index = SpecIndex(spec)
        self._index = index
        self._states: dict[LaneId, LaneState] = steady_lane_states(index)
        self._xconnects: tuple[CrossConnect, ...] = spec.xconnects
        self._peer: dict[LaneId, LaneId] = index.peers(self._xconnects)
        self._verified: set[LaneId] = {
            x for x, s in self._states.items() if s is LaneState.ACTIVE
        }
        self._failed: set[ModuleId] = set()
        self._pool: set[ModuleId] = {m.id for m in spec.modules if m.spare}
        self._version: Version = 1
        self._correlator = IncidentCorrelator(self._correlation_window)
        self._next_incident: IncidentId = 1
        self._snapshot: TopologySnapshot | None = None

    # ------------------------------------------------------------------ events

    def on_lane_down(self, msg: LaneDown, now: SimTime) -> None:
        """Apply a LANE_DOWN report and feed incident correlation."""
        index = self._require()
        for x in msg.lanes:
            if x not in index.lanes or index.lanes[x].device != msg.device:
                raise ValueError(f"LANE_DOWN from {msg.device} names lane {x}, not on it")
            if self._states[x] is not LaneState.FAILED:
                self._set(x, LaneState.DOWN, now, msg.cause.value)
            self._verified.discard(x)
        self._correlator.add(msg, now)
        self._changed()

    def on_lane_up(self, msg: LaneUp, now: SimTime) -> None:
        """Apply a LANE_UP report: verified lanes whose HELLO named the expected peer."""
        index = self._require()
        for x, seen in zip(msg.lanes, msg.peer_lanes, strict=True):
            if x not in index.lanes or index.lanes[x].device != msg.device:
                raise ValueError(f"LANE_UP from {msg.device} names lane {x}, not on it")
            state = self._states[x]
            if self._peer.get(x) != seen:
                self._emit(now, "tm_ignored", lane=x, why=f"HELLO saw {seen}, expected "
                           f"{self._peer.get(x)}")
                continue
            if state in (LaneState.IDLE, LaneState.FAILED):
                self._emit(now, "tm_ignored", lane=x, why=f"LANE_UP for a {state.value} lane")
                continue
            if state in (LaneState.DOWN, LaneState.CONNECTING):
                self._set(x, LaneState.VERIFYING, now, "lane_up")
            self._verified.add(x)
        self._changed()

    def on_ocs_done(self, msg: OcsDone, matrix: Sequence[CrossConnect]) -> None:
        """Take the matrix read back as the truth; lanes on new cross-connects are CONNECTING."""
        index = self._require()
        now = msg.header.time
        before, after = set(self._xconnects), set(matrix)
        self._xconnects = tuple(sorted(after, key=lambda xc: int(xc.north[1:])))
        self._peer = index.peers(self._xconnects)
        for xc in sorted(before - after, key=lambda xc: int(xc.north[1:])):
            for port in (xc.north, xc.south):
                x = index.lane_at_ocs_port.get(port)
                if x is not None and self._states[x] in _LIVE:
                    self._set(x, LaneState.DOWN, now, "disconnected")
                    self._verified.discard(x)
        for xc in sorted(after - before, key=lambda xc: int(xc.north[1:])):
            for port in (xc.north, xc.south):
                x = index.lane_at_ocs_port.get(port)
                if x is not None and self._states[x] in (LaneState.IDLE, LaneState.DOWN):
                    self._set(x, LaneState.CONNECTING, now, "ocs_done")
        self._emit(now, "tm_matrix", matrix_version=msg.matrix_version,
                   removed=_pairs(before - after), added=_pairs(after - before))
        self._changed()

    def on_groups_installed(self, device: DeviceId, groups: GroupSet, now: SimTime) -> None:
        """A device applied a GROUP_SET; its lanes become ACTIVE (they must have been usable)."""
        index = self._require()
        view = self.view()
        for neighbor, lanes in groups.entries.items():
            wanted = set(lanes)
            for x in lanes:
                if self._states[x] is not LaneState.ACTIVE and x not in view.usable_lanes(
                    device, neighbor
                ):
                    raise LaneStateError(
                        f"{device} installed lane {x} toward {neighbor}, but it is not usable"
                    )
            for x in index.lanes_of_device[device]:
                y = self._peer.get(x)
                if y is None or index.lanes[y].device != neighbor:
                    continue
                if x in wanted:
                    self._set(x, LaneState.ACTIVE, now, "group_installed")
                elif self._states[x] is LaneState.ACTIVE:
                    self._set(x, LaneState.VERIFYING, now, "left_group")
        self._changed()

    def record_repair(self, plan: RepairPlan, now: SimTime) -> None:
        """The failed modules' DOWN lanes become FAILED; the used spare leaves the pool."""
        index = self._require()
        for m in plan.failed_modules:
            self._failed.add(m)
            for x in index.modules[m].lanes:
                if self._states[x] is LaneState.DOWN:
                    self._set(x, LaneState.FAILED, now, "replaced")
        self._pool.discard(plan.spare_module)
        self._emit(now, "tm_repair", failed=list(plan.failed_modules), spare=plan.spare_module)
        self._changed()

    # ------------------------------------------------------------------ incidents

    def poll_incidents(self, now: SimTime) -> list[Incident]:
        """Close due correlation windows and return the new incidents."""
        taken = self._correlator.take(now)
        if taken is None:
            return []
        opened_at, reports = taken
        incident = self._incident(opened_at, reports)
        self._emit(now, "tm_incident", incident=incident.id, reports=len(reports),
                   failed_device=incident.failed_device, lost=_pairs_list(incident.lost))
        return [incident]

    def correlation_deadline(self) -> SimTime | None:
        """When poll_incidents() will next return an incident, or None if no window is open."""
        return self._correlator.deadline()

    # ------------------------------------------------------------------ queries

    def view(self) -> TopologySnapshot:
        """Read-only snapshot of the current state (cached until the next change)."""
        index = self._require()
        if self._snapshot is None:
            self._snapshot = TopologySnapshot(
                index,
                self._version,
                self._states,
                self._xconnects,
                self._failed,
                self._verified,
                self._pool,
            )
        return self._snapshot

    def version(self) -> Version:
        """Current topology version."""
        self._require()
        return self._version

    # ------------------------------------------------------------------ internals

    def _incident(self, opened_at: SimTime, reports: list[LaneDown]) -> Incident:
        index = self._require()
        faults = [r for r in reports if r.cause is DownCause.MODULE_FAULT]
        fault_devices = sorted({r.device for r in faults}, key=device_key)
        lanes = frozenset(x for r in reports for x in r.lanes)
        view = self.view()
        lost: set[tuple[DeviceId, DeviceId]] = set()
        for x in lanes:
            y = self._peer.get(x) or index.startup_peer.get(x)
            if y is None:
                continue
            d, n = index.lanes[x].device, index.lanes[y].device
            if not view.active_lanes(d, n):
                lost.add((d, n))
        incident = Incident(
            id=self._next_incident,
            opened_at=opened_at,
            reports=list(reports),
            failed_device=fault_devices[0] if len(fault_devices) == 1 else None,
            failed_modules=tuple(sorted({m for r in faults for m in r.modules})),
            lanes=lanes,
            lost=frozenset(lost),
            state=IncidentState.OPEN,
        )
        self._next_incident += 1
        return incident

    def _set(self, lane: LaneId, state: LaneState, now: SimTime, why: str) -> None:
        old = self._states[lane]
        check_transition(lane, old, state)
        if old is not state:
            self._states[lane] = state
            self._emit(now, "tm_lane", lane=lane, old=old.value, new=state.value, why=why)

    def _changed(self) -> None:
        self._version += 1
        self._snapshot = None

    def _emit(self, now: SimTime, kind: str, **fields: Any) -> None:
        if self._trace is not None:
            self._trace.record({"t": now, "kind": kind, **fields})

    def _require(self) -> SpecIndex:
        if self._index is None:
            raise RuntimeError("load() a topology first")
        return self._index


def _pairs(xcs: set[CrossConnect]) -> list[list[str]]:
    return [[xc.north, xc.south] for xc in sorted(xcs, key=lambda xc: int(xc.north[1:]))]


def _pairs_list(pairs: frozenset[tuple[DeviceId, DeviceId]]) -> list[list[str]]:
    return [list(p) for p in sorted(pairs, key=lambda p: (device_key(p[0]), device_key(p[1])))]
