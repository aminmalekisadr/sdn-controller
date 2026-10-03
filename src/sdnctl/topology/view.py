"""The concrete TopologyView: a read-only snapshot of lane states and cross-connects over a spec.

Peers: a lane with an OCS port faces whichever lane the current cross-connect points to; any
other lane faces the lane with the same index on the far end of its link.

Capacity (SPEC 5.3): a lane is in service when it and the lane it faces are both ACTIVE. The
startup count of a lane set is its working lanes that faced a lane when the topology was built.
"""

from collections import Counter
from collections.abc import Iterable, Mapping

from sdnctl.model import Device, Lane, Module, TopologySpec
from sdnctl.types import (
    CrossConnect,
    DeviceId,
    DomainId,
    LaneId,
    LaneState,
    ModuleId,
    OcsPortId,
    Version,
    device_key,
    lane_key,
)


class SpecIndex:
    """Lookups over one TopologySpec, built once and shared by every snapshot of it."""

    def __init__(self, spec: TopologySpec) -> None:
        self.spec = spec
        self.devices: dict[DeviceId, Device] = {d.id: d for d in spec.devices}
        self.lanes: dict[LaneId, Lane] = {x.id: x for x in spec.lanes}
        self.modules: dict[ModuleId, Module] = {m.id: m for m in spec.modules}
        lanes_of: dict[DeviceId, list[LaneId]] = {d: [] for d in self.devices}
        lanes_of_port: dict[str, list[LaneId]] = {}
        for x in spec.lanes:
            lanes_of[x.device].append(x.id)
            lanes_of_port.setdefault(x.port, []).append(x.id)
        self.lanes_of_device: dict[DeviceId, tuple[LaneId, ...]] = {
            d: tuple(sorted(v, key=lane_key)) for d, v in lanes_of.items()
        }
        port_peer: dict[str, str] = {}
        for link in spec.links:
            port_peer[link.a] = link.b
            port_peer[link.b] = link.a
        self.link_peer: dict[LaneId, LaneId] = {
            x.id: f"{port_peer[x.port]}.{x.index}"
            for x in spec.lanes
            if x.ocs_port is None and x.port in port_peer
        }
        self.lane_at_ocs_port: dict[OcsPortId, LaneId] = {
            x.ocs_port: x.id for x in spec.lanes if x.ocs_port is not None
        }
        self.domain_lanes: dict[DomainId, tuple[LaneId, ...]] = {
            dom.id: tuple(x for p in dom.ports for x in lanes_of_port.get(p, ()))
            + tuple(x for m in dom.spare_modules for x in self.modules[m].lanes)
            for dom in spec.domains
        }
        # startup: working lanes that faced a lane when the topology was built
        self.startup_peer: dict[LaneId, LaneId] = {
            x: y for x, y in self.peers(spec.xconnects).items() if not self.lanes[x].spare
        }
        self.startup_device: Counter[DeviceId] = Counter(
            self.lanes[x].device for x in self.startup_peer
        )
        self.startup_pair: Counter[tuple[DeviceId, DeviceId]] = Counter(
            (self.lanes[x].device, self.lanes[y].device) for x, y in self.startup_peer.items()
        )
        self.startup_domain: dict[DomainId, int] = {
            dom: sum(1 for x in lanes if x in self.startup_peer)
            for dom, lanes in self.domain_lanes.items()
        }

    def peers(self, xconnects: Iterable[CrossConnect]) -> dict[LaneId, LaneId]:
        """Lane -> lane it faces under these cross-connects; lanes facing nothing are absent."""
        out = dict(self.link_peer)
        for xc in xconnects:
            north = self.lane_at_ocs_port.get(xc.north)
            south = self.lane_at_ocs_port.get(xc.south)
            if north is not None and south is not None:
                out[north] = south
                out[south] = north
        return out


class TopologySnapshot:
    """Read-only TopologyView: one version of lane states, cross-connects and LANE_UPs."""

    def __init__(
        self,
        index: SpecIndex,
        version: Version,
        lane_states: Mapping[LaneId, LaneState],
        xconnects: Iterable[CrossConnect],
        failed_modules: Iterable[ModuleId] = (),
        lane_up: Iterable[LaneId] = (),
        spare_pool: Iterable[ModuleId] | None = None,
    ) -> None:
        self._index = index
        self._version = version
        self._states = dict(lane_states)
        if self._states.keys() != index.lanes.keys():
            raise ValueError("lane_states must give a state for every lane of the topology")
        self._xconnects = tuple(xconnects)
        self._failed = frozenset(failed_modules)
        self._lane_up = frozenset(lane_up)  # lanes verified by LANE_UP (or at startup)
        self._pool = (
            frozenset(spare_pool)
            if spare_pool is not None
            else frozenset(m for m, mod in index.modules.items() if mod.spare) - self._failed
        )
        self._peer = index.peers(self._xconnects)
        self._neighbors: dict[DeviceId, tuple[DeviceId, ...]] = {}

    @property
    def spec(self) -> TopologySpec:
        """The physical topology this view is built on."""
        return self._index.spec

    @property
    def version(self) -> Version:
        """Topology version of this snapshot."""
        return self._version

    def lane_state(self, lane: LaneId) -> LaneState:
        """Current state of a lane."""
        return self._states[lane]

    def xconnects(self) -> tuple[CrossConnect, ...]:
        """Current OCS cross-connects."""
        return self._xconnects

    def failed_modules(self) -> frozenset[ModuleId]:
        """Modules recorded as failed."""
        return self._failed

    def spare_pool(self) -> frozenset[ModuleId]:
        """Spare modules still free to use."""
        return self._pool

    def neighbors(self, device: DeviceId) -> tuple[DeviceId, ...]:
        """Devices with at least one usable lane to device, sorted by device_key."""
        cached = self._neighbors.get(device)
        if cached is None:
            found = {
                self._index.lanes[self._peer[x]].device
                for x in self._index.lanes_of_device[device]
                if self._usable(x)
            }
            cached = tuple(sorted(found, key=device_key))
            self._neighbors[device] = cached
        return cached

    def active_lanes(self, device: DeviceId, neighbor: DeviceId) -> tuple[LaneId, ...]:
        """ACTIVE lanes of device toward neighbor, sorted by lane_key."""
        return tuple(
            x
            for x in self._toward(device, neighbor)
            if self._states[x] is LaneState.ACTIVE
        )

    def usable_lanes(self, device: DeviceId, neighbor: DeviceId) -> tuple[LaneId, ...]:
        """Usable lanes of device toward neighbor: ACTIVE, or VERIFYING with both LANE_UPs."""
        return tuple(x for x in self._toward(device, neighbor) if self._usable(x))

    def peer_of(self, lane: LaneId) -> LaneId | None:
        """The lane this lane faces now, or None if it faces nothing."""
        if lane not in self._states:
            raise KeyError(lane)
        return self._peer.get(lane)

    def lane_by_ocs_port(self, port: OcsPortId) -> LaneId | None:
        """The lane on an OCS port, or None if the port is unused."""
        return self._index.lane_at_ocs_port.get(port)

    def routed(self, device: DeviceId) -> bool:
        """True if device is in the routing domain."""
        return self._index.devices[device].tier in self._index.spec.routed_tiers

    def device_capacity(self, device: DeviceId) -> float:
        """In-service lanes of device / its lanes in service at startup."""
        now = sum(1 for x in self._index.lanes_of_device[device] if self._in_service(x))
        return _ratio(now, self._index.startup_device[device], device)

    def pair_capacity(self, a: DeviceId, b: DeviceId) -> float:
        """In-service lane pairs between a and b / the startup count."""
        now = sum(1 for x in self._toward(a, b) if self._in_service(x))
        return _ratio(now, self._index.startup_pair[(a, b)], f"{a}/{b}")

    def domain_capacity(self, domain: DomainId) -> float:
        """In-service lanes of domain (spares included) / its lanes in service at startup."""
        now = sum(1 for x in self._index.domain_lanes[domain] if self._in_service(x))
        return _ratio(now, self._index.startup_domain[domain], domain)

    def _toward(self, device: DeviceId, neighbor: DeviceId) -> list[LaneId]:
        """Lanes of device that face a lane of neighbor now, sorted by lane_key."""
        lanes = self._index.lanes
        return [
            x
            for x in self._index.lanes_of_device[device]
            if (y := self._peer.get(x)) is not None and lanes[y].device == neighbor
        ]

    def _usable(self, x: LaneId) -> bool:
        y = self._peer.get(x)
        if y is None:
            return False
        state = self._states[x]
        if state is LaneState.ACTIVE:
            return True
        return state is LaneState.VERIFYING and x in self._lane_up and y in self._lane_up

    def _in_service(self, x: LaneId) -> bool:
        y = self._peer.get(x)
        return (
            y is not None
            and self._states[x] is LaneState.ACTIVE
            and self._states[y] is LaneState.ACTIVE
        )


def steady_lane_states(index: SpecIndex) -> dict[LaneId, LaneState]:
    """Startup states: spare lanes IDLE, working lanes ACTIVE if they face a lane, else DOWN."""
    states: dict[LaneId, LaneState] = {}
    for x, lane in index.lanes.items():
        if lane.spare:
            states[x] = LaneState.IDLE
        elif x in index.startup_peer:
            states[x] = LaneState.ACTIVE
        else:
            states[x] = LaneState.DOWN
    return states


def steady_state_view(spec: TopologySpec, index: SpecIndex | None = None) -> TopologySnapshot:
    """The version-1 view of a freshly built topology, with its cross-connects as built."""
    index = index if index is not None else SpecIndex(spec)
    if index.spec is not spec:
        raise ValueError("index was built for another spec")
    return TopologySnapshot(index, 1, steady_lane_states(index), spec.xconnects)


def _ratio(now: int, startup: int, what: str) -> float:
    if startup == 0:
        raise ValueError(f"{what} had no lanes in service at startup")
    return now / startup
