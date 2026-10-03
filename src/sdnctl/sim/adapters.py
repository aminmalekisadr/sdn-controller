"""Simulator adapters: deliver every message with the latencies of SPEC 11.0, count and trace it.

Down: a device command is applied, and its ACK reaches the controller, device_write after it is
sent (rule 4). Up: LANE_DOWN takes report, LANE_UP takes lane_up_report (rules 1 and 6). The OCS
applies OCS_SET ocs_command after it is sent; OCS_DONE reaches the controller when the mirrors
settle (rule 5). Every counted message is counted when sent, even if the network then loses it.
"""

import functools
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sdnctl.config import Timing
from sdnctl.jsonio import encode_message, to_jsonable
from sdnctl.messages import DeviceCommand, DeviceEvent, OcsDone, OcsSet
from sdnctl.sim.clock import EventClock
from sdnctl.sim.device import SimDevice
from sdnctl.sim.ocs import SimOcs
from sdnctl.sim.trace import Tracer
from sdnctl.types import CrossConnect, DeviceId, MsgType


class MessageLog:
    """Counts messages by type (a batch as one, with a key like 'GROUP_SET+ROUTE_SET')."""

    UNCOUNTED = frozenset({MsgType.HELLO.value, MsgType.KEEPALIVE.value})

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def count(self, key: str) -> None:
        """Count one message unless its type is never counted (HELLO, KEEPALIVE)."""
        if key not in self.UNCOUNTED:
            self.counts[key] += 1


def command_key(cmd: DeviceCommand) -> str:
    """Count key of a command: its part types joined by '+', keepalive left out when batched."""
    types = [p.header.type.value for p in cmd.parts()]
    counted = [t for t in types if t != MsgType.KEEPALIVE.value]
    return "+".join(counted) if counted else MsgType.KEEPALIVE.value


@dataclass
class _AckDrop:
    device: DeviceId
    command: MsgType
    remaining: int


class Faults:
    """Armed test faults that act on messages: dropped ACKs."""

    def __init__(self) -> None:
        self._drops: list[_AckDrop] = []

    def arm_drop_ack(self, device: DeviceId, command: MsgType, count: int) -> None:
        """Lose the next count ACKs from device for commands that contain command."""
        self._drops.append(_AckDrop(device, command, count))

    def take_ack_drop(self, device: DeviceId, cmd: DeviceCommand) -> bool:
        """True if this ACK is lost; uses up one armed drop."""
        types = {p.header.type for p in cmd.parts()}
        for drop in self._drops:
            if drop.device == device and drop.command in types and drop.remaining > 0:
                drop.remaining -= 1
                return True
        return False


class SimDeviceAdapter:
    """IDeviceAdapter of the simulator, plus emit() for events the devices send up."""

    def __init__(
        self,
        clock: EventClock,
        timing: Timing,
        tracer: Tracer,
        log: MessageLog,
        faults: Faults,
    ) -> None:
        self._clock = clock
        self._timing = timing
        self._tracer = tracer
        self._log = log
        self._faults = faults
        self._devices: Mapping[DeviceId, SimDevice] = {}
        self._sink: Callable[[DeviceEvent], None] = lambda ev: None
        self._latency = {
            MsgType.LANE_DOWN: timing.report,
            MsgType.LANE_UP: timing.lane_up_report,
            MsgType.ACK: 0,
        }

    def attach(self, devices: Mapping[DeviceId, SimDevice]) -> None:
        """Connect the adapter to the simulated devices."""
        self._devices = devices

    def send(self, device: DeviceId, cmd: DeviceCommand) -> None:
        """Send a command; the device applies it device_write later."""
        if device not in self._devices:
            raise KeyError(f"no device {device}")
        if cmd.device != device:
            raise ValueError(f"command for {cmd.device} sent to {device}")
        key = command_key(cmd)
        self._log.count(key)
        self._tracer.emit("send", type=key, src=cmd.parts()[0].header.src, dst=device,
                          seq=cmd.seq, msg=to_jsonable(cmd))
        self._clock.schedule(
            self._timing.device_write, functools.partial(self._apply, device, cmd)
        )

    def set_event_sink(self, sink: Callable[[DeviceEvent], None]) -> None:
        """Where LANE_DOWN, LANE_UP and ACK from devices are delivered."""
        self._sink = sink

    def emit(self, ev: DeviceEvent, lost: bool = False) -> None:
        """A device sends an event now; it reaches the controller after its latency."""
        h = ev.header
        self._log.count(h.type.value)
        self._tracer.emit("send", type=h.type.value, src=h.src, dst=h.dst, seq=h.seq,
                          lost=lost, msg=encode_message(ev))
        if not lost:
            self._clock.schedule(
                self._latency[h.type], functools.partial(self._deliver, ev)
            )

    def _apply(self, device: DeviceId, cmd: DeviceCommand) -> None:
        ack = self._devices[device].handle(cmd)
        self._tracer.emit("apply", device=device, seq=cmd.seq, version=cmd.version,
                          ok=ack.ok if ack is not None else True)
        if ack is not None:
            self.emit(ack, lost=self._faults.take_ack_drop(device, cmd))

    def _deliver(self, ev: DeviceEvent) -> None:
        h = ev.header
        self._tracer.emit("deliver", type=h.type.value, src=h.src, dst=h.dst, seq=h.seq)
        self._sink(ev)


class SimOcsAdapter:
    """IOcsAdapter of the simulator."""

    def __init__(self, clock: EventClock, tracer: Tracer, log: MessageLog, ocs: SimOcs) -> None:
        self._clock = clock
        self._tracer = tracer
        self._log = log
        self._ocs = ocs
        self._sink: Callable[[OcsDone], None] = lambda msg: None
        ocs.send_done = self._done

    def apply(self, cmd: OcsSet) -> None:
        """Send OCS_SET."""
        self._log.count(MsgType.OCS_SET.value)
        h = cmd.header
        self._tracer.emit("send", type=h.type.value, src=h.src, dst=h.dst, seq=h.seq,
                          msg=encode_message(cmd))
        self._ocs.receive(cmd)

    def read_matrix(self) -> list[CrossConnect]:
        """Read the applied cross-connects back; an adapter call, not a counted message."""
        return self._ocs.read_matrix()

    def set_event_sink(self, sink: Callable[[OcsDone], None]) -> None:
        """Where OCS_DONE is delivered."""
        self._sink = sink

    def _done(self, msg: OcsDone) -> None:
        h = msg.header
        self._log.count(h.type.value)
        self._tracer.emit("send", type=h.type.value, src=h.src, dst=h.dst, seq=h.seq,
                          msg=encode_message(msg))
        self._clock.schedule(0, functools.partial(self._deliver, msg))

    def _deliver(self, msg: OcsDone) -> None:
        h = msg.header
        self._tracer.emit("deliver", type=h.type.value, src=h.src, dst=h.dst, seq=h.seq)
        self._sink(msg)
