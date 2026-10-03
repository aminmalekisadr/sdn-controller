"""Capacity measured on the simulated devices (SPEC 5.3), sampled whenever a value changes.

A lane is in service when it and the lane it physically faces are lit and both are in their
devices' installed groups. Startup counts come from SpecIndex, as in the view (D15).
"""

from collections.abc import Iterable, Mapping

from sdnctl.interfaces import CapacitySample
from sdnctl.sim.clock import EventClock
from sdnctl.sim.device import SimDevice
from sdnctl.sim.physical import PhysicalLayer
from sdnctl.topology.view import SpecIndex
from sdnctl.types import DeviceId, DomainId, LaneId, device_key


def pair_key(a: DeviceId, b: DeviceId) -> str:
    """Capacity key of a device pair, e.g. 'l1-1536/l2-2304' (ends in device_key order)."""
    return f"{a}/{b}" if device_key(a) <= device_key(b) else f"{b}/{a}"


class CapacityMonitor:
    """Domain, device-pair and device capacity over time; every key starts at 1.0."""

    def __init__(
        self,
        index: SpecIndex,
        devices: Mapping[DeviceId, SimDevice],
        physical: PhysicalLayer,
        clock: EventClock,
    ) -> None:
        self._index = index
        self._devices = devices
        self._physical = physical
        self._clock = clock
        self._lane_domain: dict[LaneId, DomainId] = {
            x: dom for dom, lanes in index.domain_lanes.items() for x in lanes
        }
        self._last: dict[str, float] = {}
        self.samples: list[CapacitySample] = []

    def in_service(self, x: LaneId) -> bool:
        """True when lane x and the lane it faces are lit and both are in their groups."""
        y = self._physical.peer(x)
        if y is None or x not in self._physical.lit or y not in self._physical.lit:
            return False
        lanes = self._index.lanes
        return (
            x in self._devices[lanes[x].device].in_group
            and y in self._devices[lanes[y].device].in_group
        )

    def device(self, d: DeviceId) -> float:
        """In-service lanes of d / its startup lanes."""
        now = sum(1 for x in self._index.lanes_of_device[d] if self.in_service(x))
        return now / self._index.startup_device[d]

    def pair(self, a: DeviceId, b: DeviceId) -> float:
        """In-service lanes of a that face b / the startup count."""
        lanes = self._index.lanes
        now = 0
        for x in self._index.lanes_of_device[a]:
            y = self._physical.peer(x)
            if y is not None and lanes[y].device == b and self.in_service(x):
                now += 1
        return now / self._index.startup_pair[(a, b)]

    def domain(self, dom: DomainId) -> float:
        """In-service lanes of a domain (spares included) / its startup lanes."""
        now = sum(1 for x in self._index.domain_lanes[dom] if self.in_service(x))
        return now / self._index.startup_domain[dom]

    def value(self, key: str) -> float:
        """Capacity by key: 'a/b' a pair, '<device>.d<k>' a domain, else a device."""
        if "/" in key:
            a, b = key.split("/")
            return self.pair(a, b)
        if "." in key:
            return self.domain(key)
        return self.device(key)

    def touch(self, lanes: Iterable[LaneId]) -> None:
        """Lanes changed: sample every key whose value changed among the keys of these lanes and
        of the lanes they face (a lane's service also depends on the lane it faces)."""
        keys: set[str] = set()
        for x in lanes:
            d = self._index.lanes[x].device
            faced = [y for y in (self._physical.peer(x), self._index.startup_peer.get(x)) if y]
            for y in faced:
                other = self._index.lanes[y].device
                if self._index.startup_pair[(d, other)]:
                    keys.add(pair_key(d, other))
            for z in (x, *faced):
                keys.add(self._index.lanes[z].device)
                dom = self._lane_domain.get(z)
                if dom is not None:
                    keys.add(dom)
        now = self._clock.now()
        for key in sorted(keys):
            value = self.value(key)
            if value != self._last.get(key, 1.0):
                self._last[key] = value
                self.samples.append(CapacitySample(now, key, value))
