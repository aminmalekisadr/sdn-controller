"""Device tables (routes and groups), their differences, waves and the forwarding check result."""

from dataclasses import dataclass

from sdnctl.types import DeviceId, LaneId, Version


@dataclass(frozen=True)
class RouteEntry:
    """Route to one destination NPU: every routed neighbor one hop closer (ECMP)."""

    dest: DeviceId  # always an NPU
    neighbors: tuple[DeviceId, ...]  # sorted by device_key
    hops: int


@dataclass(frozen=True)
class GroupEntry:
    """Neighbor group: the ACTIVE lanes toward one neighbor, equal weights."""

    neighbor: DeviceId
    lanes: tuple[LaneId, ...]  # ACTIVE lanes only, sorted by lane_key; never empty


@dataclass
class DeviceTables:
    """Route and group tables of one device, with their version."""

    device: DeviceId
    version: Version
    routes: dict[DeviceId, RouteEntry]
    groups: dict[DeviceId, GroupEntry]


Tables = dict[DeviceId, DeviceTables]


@dataclass(frozen=True)
class RouteChange:
    """One route entry that differs; old None means added, new None means removed."""

    dest: DeviceId
    old: RouteEntry | None
    new: RouteEntry | None


@dataclass(frozen=True)
class GroupChange:
    """One group entry that differs; old None means added, new None means removed."""

    neighbor: DeviceId
    old: GroupEntry | None
    new: GroupEntry | None


@dataclass
class TableDiff:
    """Per-device changes between installed and target tables."""

    routes: dict[DeviceId, list[RouteChange]]
    groups: dict[DeviceId, list[GroupChange]]

    def route_entries(self) -> int:
        """Number of route entries that differ."""
        return sum(len(changes) for changes in self.routes.values())

    def group_entries(self) -> int:
        """Number of group entries that differ."""
        return sum(len(changes) for changes in self.groups.values())

    def devices(self) -> set[DeviceId]:
        """Devices with at least one changed route or group entry."""
        changed = {d for d, changes in self.routes.items() if changes}
        changed.update(d for d, changes in self.groups.items() if changes)
        return changed


@dataclass(frozen=True)
class AdjacencyChange:
    """Adjacencies (device, neighbor) whose group became empty, or came back."""

    lost: frozenset[tuple[DeviceId, DeviceId]]
    restored: frozenset[tuple[DeviceId, DeviceId]]


@dataclass(frozen=True)
class Wave:
    """One step of an ordered table update; the next wave starts after every ACK of this one."""

    index: int
    devices: tuple[DeviceId, ...]


@dataclass(frozen=True)
class ForwardingReport:
    """Pair counts from the loop checker, over ordered (source NPU, destination NPU) pairs."""

    looping_pairs: int
    blackholed_pairs: int
