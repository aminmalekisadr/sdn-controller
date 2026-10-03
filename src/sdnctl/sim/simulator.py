"""The simulator (SPEC 7.6): devices, the OCS and a hosted controller on one event clock.

load_topology() and configure() can come in either order; once both are known the simulator
builds the devices, the OCS and the controller, and installs the version-1 tables. Calling either
again starts a fresh run. Without a hosted controller (Phase 2) the devices start with groups built
from the topology and no routes, and device events are counted and traced but not acted on.
"""

import dataclasses
from collections.abc import Iterable
from typing import Any

from sdnctl.config import ControllerConfig, Features, default_config
from sdnctl.interfaces import (
    ControllerContext,
    ControllerFactory,
    HostedController,
    SimReport,
)
from sdnctl.model import TopologySpec
from sdnctl.sim.adapters import Faults, MessageLog, SimDeviceAdapter, SimOcsAdapter
from sdnctl.sim.capacity import CapacityMonitor
from sdnctl.sim.clock import EventClock
from sdnctl.sim.device import SimDevice
from sdnctl.sim.injector import FailureInjector
from sdnctl.sim.ocs import SimOcs
from sdnctl.sim.physical import PhysicalLayer
from sdnctl.sim.trace import MemoryTrace, Tracer
from sdnctl.tables import DeviceTables, GroupEntry, Tables
from sdnctl.topology import build_topology, validate
from sdnctl.topology.view import SpecIndex, steady_state_view
from sdnctl.types import CrossConnect, DeviceId, LaneId, ModuleMapping

STARTUP_VERSION = 1

__all__ = [
    "STARTUP_VERSION",
    "ControllerContext",
    "ControllerFactory",
    "HostedController",
    "Simulator",
    "topology_tables",
]


def topology_tables(spec: TopologySpec, index: SpecIndex) -> Tables:
    """Startup tables straight from the topology: every ACTIVE lane in a group, no routes."""
    view = steady_state_view(spec, index)
    return {
        d.id: DeviceTables(
            d.id,
            STARTUP_VERSION,
            {},
            {n: GroupEntry(n, view.active_lanes(d.id, n)) for n in view.neighbors(d.id)},
        )
        for d in spec.devices
    }


class _Run:
    """Everything one configured run owns."""

    def __init__(
        self, spec: TopologySpec, config: ControllerConfig, factory: ControllerFactory | None
    ) -> None:
        timing = config.timing()
        self.spec = spec
        self.clock = EventClock()
        self.trace = MemoryTrace()
        tracer = Tracer(self.clock, self.trace)
        self.log = MessageLog()
        faults = Faults()
        index = SpecIndex(spec)
        self.device_adapter = SimDeviceAdapter(self.clock, timing, tracer, self.log, faults)
        self.ocs: SimOcs | None = None
        if spec.has_ocs:
            self.ocs = SimOcs(index.lane_at_ocs_port, spec.xconnects, self.clock, timing, tracer)
        self.devices: dict[DeviceId, SimDevice] = {
            d.id: SimDevice(
                d,
                index,
                self.clock,
                timing,
                tracer,
                self.device_adapter.emit,
                self._lanes_opened,
                self._service_changed,
            )
            for d in spec.devices
        }
        self.physical = PhysicalLayer(
            index, self.clock, timing, tracer, self.devices, self.ocs, self._service_changed
        )
        self.capacity = CapacityMonitor(index, self.devices, self.physical, self.clock)
        self.device_adapter.attach(self.devices)
        self.ocs_adapter: SimOcsAdapter | None = None
        if self.ocs is not None:
            self.ocs.on_disconnect = self.physical.ocs_disconnected
            self.ocs.on_settled = self.physical.ocs_settled
            self.ocs_adapter = SimOcsAdapter(self.clock, tracer, self.log, self.ocs)
        self.injector = FailureInjector(
            index, self.clock, tracer, self.physical, faults, self.ocs
        )
        self.controller: HostedController | None = None
        if factory is not None:
            self.controller = factory(
                ControllerContext(
                    spec, config, self.clock, self.device_adapter, self.ocs_adapter, self.trace
                )
            )
        tables = (
            self.controller.initial_tables()
            if self.controller is not None
            else topology_tables(spec, index)
        )
        for d, device in self.devices.items():
            t = tables.get(d)
            device.install(STARTUP_VERSION, t.routes if t else {}, t.groups if t else {})
        tracer.emit(
            "load",
            topology=spec.name,
            mapping=spec.mapping.value,
            spares=spec.spare_modules_per_domain,
            ocs=config.features.ocs,
            timing=config.timing_profile,
        )

    def _lanes_opened(self, lanes: list[LaneId]) -> None:
        self.physical.lanes_opened(lanes)

    def _service_changed(self, lanes: Iterable[LaneId]) -> None:
        self.capacity.touch(lanes)


class Simulator:
    """The simulator API of SPEC 7.6: load_topology, configure, inject, run, report."""

    def __init__(
        self, controller: ControllerFactory | None = None, config: ControllerConfig | None = None
    ) -> None:
        self._factory = controller
        self._base = config if config is not None else default_config()
        self._spec: TopologySpec | None = None
        self._settings: tuple[bool, str] | None = None
        self._run: _Run | None = None

    def load_topology(self, name: str, two_plus_two: bool, spares: int = 0) -> TopologySpec:
        """Build topology 1 or 2 and its devices, and install the steady-state tables."""
        spec = build_topology(name, two_plus_two, spares)
        validate(spec, Features(two_plus_two=two_plus_two, ocs=False))
        self._spec = spec
        self._start()
        return spec

    def configure(self, ocs: bool, timing: str = "typical") -> None:
        """OCS controller on or off; typical or worst timing profile."""
        if timing not in self._base.timing_us:
            raise ValueError(f"unknown timing profile {timing!r}")
        self._settings = (ocs, timing)
        self._start()

    def inject(self, t_ms: int, event: str, **args: Any) -> None:
        """Schedule module_fail, repair, drop_ack or ocs_never_answers at t_ms."""
        self._require().injector.inject(t_ms * 1000, event, args)

    def run(self, until_ms: int) -> None:
        """Run devices, OCS and controller until until_ms."""
        self._require().clock.run_until(until_ms * 1000)

    def report(self) -> SimReport:
        """Trace, capacity over time, message counts and incident end states."""
        run = self._require()
        return SimReport(
            trace=tuple(run.trace.records),
            capacity=tuple(run.capacity.samples),
            messages=dict(run.log.counts),
            incidents=run.controller.outcomes() if run.controller is not None else (),
        )

    # ------------------------------------------------------------------ inspection

    @property
    def spec(self) -> TopologySpec:
        """The loaded topology."""
        if self._spec is None:
            raise RuntimeError("call load_topology() first")
        return self._spec

    @property
    def controller(self) -> HostedController | None:
        """The hosted controller of the current run, if any."""
        return self._require().controller

    def now_us(self) -> int:
        """Current simulated time in microseconds."""
        return self._require().clock.now()

    def device(self, device: DeviceId) -> SimDevice:
        """One simulated device."""
        return self._require().devices[device]

    def capacity(self, key: str) -> float:
        """Capacity now, on the devices: 'a/b' a pair, '<dev>.d<k>' a domain, else a device."""
        return self._require().capacity.value(key)

    def ocs_matrix(self) -> list[CrossConnect]:
        """The OCS cross-connects as applied (empty without an OCS)."""
        ocs = self._require().ocs
        return ocs.read_matrix() if ocs is not None else []

    def _start(self) -> None:
        if self._spec is None or self._settings is None:
            return
        ocs, timing = self._settings
        features = Features(
            two_plus_two=self._spec.mapping is ModuleMapping.TWO_PLUS_TWO, ocs=ocs
        )
        config = dataclasses.replace(self._base, features=features, timing_profile=timing)
        validate(self._spec, features)
        self._run = _Run(self._spec, config, self._factory)

    def _require(self) -> _Run:
        if self._run is None:
            raise RuntimeError("call load_topology() and configure() first")
        return self._run
