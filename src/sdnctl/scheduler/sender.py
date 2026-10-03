"""Command sender (SPEC 7.3, safety rule 8): at most one outstanding command per device.

A command to a busy device waits its turn. Each command has an ACK timeout, counted from when it
is sent; on expiry it is resent once (the caller says what to resend), and if that is not
acknowledged either, the caller is told it failed. An ACK for anything but the outstanding command
is ignored.
"""

import functools
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from sdnctl.interfaces import IClock, IDeviceAdapter
from sdnctl.messages import Ack, DeviceCommand
from sdnctl.types import DeviceId, SimTime


@dataclass
class _Command:
    cmd: DeviceCommand
    on_ok: Callable[[DeviceCommand], None]
    on_fail: Callable[[str], None]
    resend: Callable[[], DeviceCommand]
    sends: int = 0
    token: int = 0


@dataclass
class _Device:
    current: _Command | None = None
    waiting: deque[_Command] = field(default_factory=deque)


class CommandSender:
    """Sends device commands one at a time per device, with an ACK timeout and one resend."""

    def __init__(
        self,
        devices: IDeviceAdapter,
        clock: IClock,
        ack_timeout: SimTime,
        resends: int,
        on_acked: Callable[[DeviceId, DeviceCommand], None],
        emit: Callable[..., None],
    ) -> None:
        self._devices = devices
        self._clock = clock
        self._ack_timeout = ack_timeout
        self._resends = resends
        self._on_acked = on_acked
        self._emit = emit
        self._state: dict[DeviceId, _Device] = {}
        self._tokens = 0

    def send(
        self,
        device: DeviceId,
        cmd: DeviceCommand,
        on_ok: Callable[[DeviceCommand], None],
        on_fail: Callable[[str], None],
        resend: Callable[[], DeviceCommand],
    ) -> None:
        """Send cmd now, or as soon as device has no outstanding command."""
        state = self._state.setdefault(device, _Device())
        command = _Command(cmd, on_ok, on_fail, resend)
        if state.current is None:
            self._transmit(device, state, command)
        else:
            state.waiting.append(command)
            self._emit("queued", device=device, seq=cmd.seq)

    def busy(self, device: DeviceId) -> bool:
        """True while device has an outstanding command."""
        state = self._state.get(device)
        return state is not None and state.current is not None

    def on_ack(self, ack: Ack) -> None:
        """An ACK arrived: finish the outstanding command it acknowledges."""
        device = ack.header.src
        state = self._state.get(device)
        command = state.current if state is not None else None
        if state is None or command is None or command.cmd.seq != ack.acked_seq:
            self._emit("ack_ignored", device=device, acked_seq=ack.acked_seq)
            return
        self._finish(device, state)
        if ack.ok:
            self._on_acked(device, command.cmd)
            command.on_ok(command.cmd)
        else:
            command.on_fail(f"{device} rejected seq {ack.acked_seq}: {ack.error}")

    def _transmit(self, device: DeviceId, state: _Device, command: _Command) -> None:
        state.current = command
        command.sends += 1
        self._tokens += 1
        command.token = self._tokens
        self._devices.send(device, command.cmd)
        self._clock.schedule(
            self._ack_timeout, functools.partial(self._timeout, device, command.token)
        )

    def _timeout(self, device: DeviceId, token: int) -> None:
        state = self._state[device]
        command = state.current
        if command is None or command.token != token:
            return  # acknowledged in time
        self._emit("ack_timeout", device=device, seq=command.cmd.seq, sends=command.sends)
        if command.sends <= self._resends:
            command.cmd = command.resend()
            self._emit("resend", device=device, seq=command.cmd.seq)
            state.current = None
            self._transmit(device, state, command)
            return
        self._finish(device, state)
        command.on_fail(f"no ACK from {device} after {command.sends} sends")

    def _finish(self, device: DeviceId, state: _Device) -> None:
        state.current = None
        if state.waiting:
            self._transmit(device, state, state.waiting.popleft())
