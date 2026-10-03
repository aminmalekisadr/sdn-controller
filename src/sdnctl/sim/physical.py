"""PhysicalLayer: which lanes face each other, which modules work, and where there is light.

A lane pair has light when both lanes transmit (module working, lane not an unopened spare) and
the path between them exists: a link, or a settled OCS cross-connect. Light comes back
lane_bringup after the last of these conditions is met; it goes out at once when one breaks.
"""

import functools
import itertools
from collections.abc import Callable, Iterable, Mapping

from sdnctl.config import Timing
from sdnctl.sim.clock import EventClock
from sdnctl.sim.device import SimDevice
from sdnctl.sim.ocs import SimOcs
from sdnctl.sim.trace import Tracer
from sdnctl.topology.view import SpecIndex
from sdnctl.types import CrossConnect, DeviceId, LaneId, LaneState, ModuleId, device_key, lane_key


class PhysicalLayer:
    """Module health, light paths and light on lane pairs; tells devices when light changes."""

    def __init__(
        self,
        index: SpecIndex,
        clock: EventClock,
        timing: Timing,
        tracer: Tracer,
        devices: Mapping[DeviceId, SimDevice],
        ocs: SimOcs | None,
        service_changed: Callable[[Iterable[LaneId]], None],
    ) -> None:
        self._index = index
        self._clock = clock
        self._timing = timing
        self._tracer = tracer
        self._devices = devices
        self._ocs = ocs
        self._service_changed = service_changed
        self.failed: set[ModuleId] = set()
        self.lit: set[LaneId] = set(index.startup_peer)  # the steady state: every built pair lit
        # lane -> (token, partner lane) of its scheduled bring-up
        self._bringing_up: dict[LaneId, tuple[int, LaneId]] = {}
        self._tokens = itertools.count(1)

    def peer(self, lane: LaneId) -> LaneId | None:
        """The lane this lane physically faces now, or None."""
        linked = self._index.link_peer.get(lane)
        if linked is not None or self._ocs is None:
            return linked
        port = self._index.lanes[lane].ocs_port
        far = self._ocs.settled.get(port) if port is not None else None
        return self._index.lane_at_ocs_port.get(far) if far is not None else None

    def fail_modules(self, modules: list[ModuleId]) -> None:
        """Modules fail now: their lanes and the lanes facing them go dark (SPEC 11.0 rule 1)."""
        lost: dict[DeviceId, set[LaneId]] = {}
        own: dict[DeviceId, list[ModuleId]] = {}
        for m in modules:
            self.failed.add(m)
            module = self._index.modules[m]
            own.setdefault(module.device, []).append(m)
            for x in module.lanes:
                self._cancel_bring_up(x)
                state = self._devices[module.device].lane_state[x]
                if state not in (LaneState.IDLE, LaneState.DOWN):
                    lost.setdefault(module.device, set()).add(x)
                y = self.peer(x)
                if y is not None and y in self.lit:
                    lost.setdefault(self._index.lanes[y].device, set()).add(y)
        self._tracer.emit("module_fail", modules=list(modules))
        self._go_dark(lost, own)

    def repair_modules(self, modules: list[ModuleId]) -> None:
        """Modules work again; their lanes come back on the same ports with the same peers."""
        self._tracer.emit("module_repair", modules=list(modules))
        for m in modules:
            self.failed.discard(m)
            module = self._index.modules[m]
            self._devices[module.device].on_repair_start(list(module.lanes))
        for m in modules:
            for x in self._index.modules[m].lanes:
                self._try_bring_up(x)

    def lanes_opened(self, lanes: list[LaneId]) -> None:
        """A device opened lanes (PREPARE); bring up any whose path is ready."""
        for x in lanes:
            self._try_bring_up(x)

    def ocs_disconnected(self, xconnects: list[CrossConnect]) -> None:
        """Cross-connects were removed: lit lanes on them lose light."""
        lost: dict[DeviceId, set[LaneId]] = {}
        for xc in xconnects:
            for port in (xc.north, xc.south):
                x = self._index.lane_at_ocs_port.get(port)
                if x is None:
                    continue
                self._cancel_bring_up(x)
                if x in self.lit:
                    lost.setdefault(self._index.lanes[x].device, set()).add(x)
        self._go_dark(lost, {})

    def ocs_settled(self, xconnects: list[CrossConnect]) -> None:
        """New cross-connects settled: bring up their lanes if both ends transmit."""
        for xc in xconnects:
            x = self._index.lane_at_ocs_port.get(xc.north)
            if x is not None:
                self._try_bring_up(x)

    def _transmits(self, lane: LaneId) -> bool:
        info = self._index.lanes[lane]
        if info.module is not None and info.module in self.failed:
            return False
        return self._devices[info.device].lane_state[lane] is not LaneState.IDLE

    def _try_bring_up(self, x: LaneId) -> None:
        y = self.peer(x)
        if y is None or x in self.lit or x in self._bringing_up or y in self._bringing_up:
            return
        if not (self._transmits(x) and self._transmits(y)):
            return
        token = next(self._tokens)
        self._bringing_up[x] = (token, y)
        self._bringing_up[y] = (token, x)
        self._clock.schedule(
            self._timing.lane_bringup, functools.partial(self._light, x, y, token)
        )

    def _cancel_bring_up(self, x: LaneId) -> None:
        pending = self._bringing_up.pop(x, None)
        if pending is not None:
            self._bringing_up.pop(pending[1], None)

    def _light(self, x: LaneId, y: LaneId, token: int) -> None:
        if self._bringing_up.get(x) != (token, y):
            return  # cancelled
        self._cancel_bring_up(x)
        if self.peer(x) != y or not (self._transmits(x) and self._transmits(y)):
            return
        self.lit.update((x, y))
        self._tracer.emit("light", lanes=[x, y], on=True)
        self._devices[self._index.lanes[x].device].on_light(x, y)
        self._devices[self._index.lanes[y].device].on_light(y, x)
        self._service_changed((x, y))

    def _go_dark(
        self, lost: dict[DeviceId, set[LaneId]], own: dict[DeviceId, list[ModuleId]]
    ) -> None:
        """Lanes lose light; each device reports once for this fault event."""
        lanes = sorted((x for xs in lost.values() for x in xs), key=lane_key)
        for x in lanes:
            self.lit.discard(x)
            self._cancel_bring_up(x)
        if lanes:
            self._tracer.emit("light", lanes=lanes, on=False)
        for dev in sorted(lost, key=device_key):
            self._devices[dev].on_lanes_lost(sorted(lost[dev], key=lane_key), own.get(dev, []))
        self._service_changed(lanes)
