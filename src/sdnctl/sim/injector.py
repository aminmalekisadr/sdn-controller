"""Failure injector: module failures, repairs and the two test faults (SPEC 7.6, 11)."""

from collections.abc import Callable, Mapping
from typing import Any

from sdnctl.sim.adapters import Faults
from sdnctl.sim.clock import EventClock
from sdnctl.sim.ocs import SimOcs
from sdnctl.sim.physical import PhysicalLayer
from sdnctl.sim.trace import Tracer
from sdnctl.topology.view import SpecIndex
from sdnctl.types import ModuleId, MsgType, SimTime

EVENT_ARGS: dict[str, frozenset[str]] = {
    "module_fail": frozenset({"modules"}),
    "repair": frozenset({"modules"}),
    "drop_ack": frozenset({"device", "command", "count"}),
    "ocs_never_answers": frozenset(),
}
DROPPABLE_COMMANDS = ("PREPARE", "GROUP_SET", "ROUTE_SET")


class FailureInjector:
    """Checks injected events when they are injected, and runs them at their time."""

    def __init__(
        self,
        index: SpecIndex,
        clock: EventClock,
        tracer: Tracer,
        physical: PhysicalLayer,
        faults: Faults,
        ocs: SimOcs | None,
    ) -> None:
        self._index = index
        self._clock = clock
        self._tracer = tracer
        self._physical = physical
        self._faults = faults
        self._ocs = ocs

    def inject(self, t: SimTime, event: str, args: Mapping[str, Any]) -> None:
        """Schedule one event at time t (us); raise ValueError if it is malformed."""
        if event not in EVENT_ARGS:
            raise ValueError(f"unknown event {event!r}; known: {', '.join(EVENT_ARGS)}")
        if set(args) != EVENT_ARGS[event]:
            want = ", ".join(sorted(EVENT_ARGS[event])) or "none"
            raise ValueError(f"{event} takes args: {want}; got {sorted(args)}")
        if t < self._clock.now():
            raise ValueError(f"cannot inject {event} at {t} us; it is already {self._clock.now()}")
        action = self._action(event, args)
        record = {"event": event, **{k: list(v) if k == "modules" else v for k, v in args.items()}}

        def fire() -> None:
            self._tracer.emit("inject", **record)
            action()

        self._clock.schedule(t - self._clock.now(), fire)

    def _action(self, event: str, args: Mapping[str, Any]) -> Callable[[], None]:
        if event in ("module_fail", "repair"):
            modules = self._modules(args["modules"])
            if event == "module_fail":
                return lambda: self._physical.fail_modules(modules)
            return lambda: self._physical.repair_modules(modules)
        if event == "drop_ack":
            device, command, count = args["device"], args["command"], args["count"]
            if device not in self._index.devices:
                raise ValueError(f"drop_ack: no device {device!r}")
            if command not in DROPPABLE_COMMANDS:
                known = ", ".join(DROPPABLE_COMMANDS)
                raise ValueError(f"drop_ack: command must be one of {known}")
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise ValueError("drop_ack: count must be a positive integer")
            msg_type = MsgType(command)
            return lambda: self._faults.arm_drop_ack(device, msg_type, count)
        ocs = self._ocs
        if ocs is None:
            raise ValueError("ocs_never_answers: this topology has no OCS")

        def never_answer() -> None:
            ocs.never_answers = True

        return never_answer

    def _modules(self, value: Any) -> list[ModuleId]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
            raise ValueError("modules must be a non-empty list of module ids")
        unknown = [m for m in value if m not in self._index.modules]
        if unknown:
            raise ValueError(f"unknown modules: {unknown}")
        return list(value)
