"""Topology data model: the physical spec, the read-only view of its state, and incidents."""

from dataclasses import dataclass
from typing import Protocol

from sdnctl.messages import LaneDown
from sdnctl.types import (
    CrossConnect,
    DeviceId,
    DomainId,
    IncidentId,
    IncidentState,
    LaneId,
    LaneState,
    LinkKind,
    ModuleId,
    ModuleMapping,
    OcsPortId,
    PortId,
    Role,
    SimTime,
    Tier,
    Version,
)

__all__ = [
    "CrossConnect",
    "Device",
    "Domain",
    "Incident",
    "Lane",
    "Link",
    "Module",
    "Port",
    "TopologySpec",
    "TopologyView",
]


@dataclass(frozen=True)
class Device:
    """An NPU, L1 switch or L2 switch."""

    id: DeviceId
    tier: Tier
    role: Role
    rack: int | None  # None for L2
    plane: int | None  # None for NPUs
    board: int | None  # NPUs only
    script_id: int  # node id in the topology script (for an NPU, its david id)


@dataclass(frozen=True)
class Port:
    """One 400G interface of a device."""

    id: PortId
    device: DeviceId
    index: int
    kind: LinkKind
    spare: bool  # spare ports belong to spare modules and have no link


@dataclass(frozen=True)
class Link:
    """Two ports joined directly or, in topology 2, through the OCS."""

    a: PortId
    b: PortId
    kind: LinkKind


@dataclass(frozen=True)
class Lane:
    """One 200G channel of a port."""

    id: LaneId
    device: DeviceId
    port: PortId
    index: int  # 0 or 1
    module: ModuleId | None  # None on copper and d2d links
    ocs_port: OcsPortId | None
    spare: bool


@dataclass(frozen=True)
class Module:
    """An optical module: it owns lanes and is the failure unit."""

    id: ModuleId
    device: DeviceId
    domain: DomainId
    lanes: tuple[LaneId, ...]
    spare: bool


@dataclass(frozen=True)
class Domain:
    """The ports served by one pair of working modules (A, B), plus its spare modules."""

    id: DomainId
    device: DeviceId
    ports: tuple[PortId, ...]
    modules: tuple[ModuleId, ModuleId]  # (A, B)
    spare_modules: tuple[ModuleId, ...]
    mapping: ModuleMapping


@dataclass(frozen=True)
class TopologySpec:
    """Physical facts of one topology; feature flags live in the config."""

    name: str  # "topology1" or "topology2"
    mapping: ModuleMapping
    spare_modules_per_domain: int
    devices: tuple[Device, ...]
    ports: tuple[Port, ...]
    links: tuple[Link, ...]
    lanes: tuple[Lane, ...]
    modules: tuple[Module, ...]
    domains: tuple[Domain, ...]
    xconnects: tuple[CrossConnect, ...]  # empty when there is no OCS
    has_ocs: bool
    routed_tiers: frozenset[Tier]  # {NPU, L1, L2} or {NPU, L1}


class TopologyView(Protocol):
    """Read-only, versioned snapshot of the spec plus lane states, cross-connects and failures."""

    @property
    def spec(self) -> TopologySpec:
        """The physical topology this view is built on."""

    @property
    def version(self) -> Version:
        """Topology version of this snapshot."""

    def lane_state(self, lane: LaneId) -> LaneState:
        """Current state of a lane."""

    def xconnects(self) -> tuple[CrossConnect, ...]:
        """Current OCS cross-connects."""

    def failed_modules(self) -> frozenset[ModuleId]:
        """Modules recorded as failed."""

    def spare_pool(self) -> frozenset[ModuleId]:
        """Spare modules still free to use."""

    def neighbors(self, device: DeviceId) -> tuple[DeviceId, ...]:
        """Devices with at least one usable lane to device, sorted by device_key."""

    def active_lanes(self, device: DeviceId, neighbor: DeviceId) -> tuple[LaneId, ...]:
        """ACTIVE lanes of device toward neighbor, sorted by lane_key."""

    def usable_lanes(self, device: DeviceId, neighbor: DeviceId) -> tuple[LaneId, ...]:
        """Usable lanes of device toward neighbor: ACTIVE, or VERIFYING with both LANE_UPs."""

    def peer_of(self, lane: LaneId) -> LaneId | None:
        """The lane this lane faces now, or None if it faces nothing."""

    def lane_by_ocs_port(self, port: OcsPortId) -> LaneId | None:
        """The lane on an OCS port, or None if the port is unused."""

    def routed(self, device: DeviceId) -> bool:
        """True if device is in the routing domain."""

    def device_capacity(self, device: DeviceId) -> float:
        """In-service lanes of device / its lanes in service at startup."""

    def pair_capacity(self, a: DeviceId, b: DeviceId) -> float:
        """In-service lane pairs between a and b / the startup count."""

    def domain_capacity(self, domain: DomainId) -> float:
        """In-service lanes of domain (spares included) / its lanes in service at startup."""


@dataclass
class Incident:
    """One failure as the controller sees it; several correlated reports form one incident."""

    id: IncidentId
    opened_at: SimTime
    reports: list[LaneDown]
    failed_device: DeviceId | None
    failed_modules: tuple[ModuleId, ...]
    lanes: frozenset[LaneId]  # every lane the reports name
    lost: frozenset[tuple[DeviceId, DeviceId]]  # (device, neighbor) whose group became empty
    state: IncidentState
    reason: str = ""  # why it ended DEGRADED or FAILED_NEEDS_OPERATOR
