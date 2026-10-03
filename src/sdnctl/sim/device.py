"""SimDevice: the device agent (UBM) model of SPEC 7.5.

It holds versioned route and group tables, prunes dead lanes from its groups, runs the verification
HELLO and the error check when light returns, and reports LANE_DOWN and LANE_UP. Device-side lane
states: IDLE (unopened spare), DOWN, CONNECTING (opened or repaired, waiting for light), VERIFYING
(light; checks running or passed) and ACTIVE (in a group).
"""

import functools
import zlib
from collections.abc import Callable, Iterable

from sdnctl.config import Timing
from sdnctl.messages import (
    CONTROLLER,
    Ack,
    DeviceCommand,
    DeviceEvent,
    GroupSet,
    Header,
    LaneDown,
    LaneUp,
    Prepare,
    RouteSet,
)
from sdnctl.model import Device
from sdnctl.sim.clock import EventClock
from sdnctl.sim.trace import Tracer
from sdnctl.tables import DeviceTables, GroupEntry, RouteEntry
from sdnctl.topology.view import SpecIndex
from sdnctl.types import (
    DeviceId,
    DownCause,
    LaneId,
    LaneState,
    ModuleId,
    MsgType,
    lane_key,
)

HELLO_MISSES = 3  # a lane goes DOWN after 3 missed HELLOs; one HELLO interval is hello_check


class CommandRejected(Exception):
    """A command fails validation; the device applies none of it."""


class SimDevice:
    """One device agent: tables, lane states, local prune, HELLO and lane reports."""

    def __init__(
        self,
        device: Device,
        index: SpecIndex,
        clock: EventClock,
        timing: Timing,
        tracer: Tracer,
        emit: Callable[[DeviceEvent], None],
        lanes_opened: Callable[[list[LaneId]], None],
        service_changed: Callable[[Iterable[LaneId]], None],
    ) -> None:
        self.id: DeviceId = device.id
        self._index = index
        self._clock = clock
        self._timing = timing
        self._tracer = tracer
        self._emit = emit
        self._lanes_opened = lanes_opened
        self._service_changed = service_changed
        self.tables = DeviceTables(device.id, 0, {}, {})
        own = index.lanes_of_device[device.id]
        self.lane_state: dict[LaneId, LaneState] = {
            x: LaneState.IDLE if index.lanes[x].spare else LaneState.DOWN for x in own
        }
        self.expected_peer: dict[LaneId, LaneId] = {
            x: index.startup_peer[x] for x in own if x in index.startup_peer
        }
        self.hello_peer: dict[LaneId, LaneId] = {}  # the peer lane each lane saw in HELLO
        self.verified: set[LaneId] = set()  # HELLO and error check passed since light came back
        self.in_group: set[LaneId] = set()
        self.keepalive_epoch: int | None = None
        self._gen: dict[LaneId, int] = {x: 0 for x in own}  # bumped to cancel pending checks
        self._seq = 0
        self._pending_up: list[LaneId] = []

    # ------------------------------------------------------------------ startup

    def install(
        self, version: int, routes: dict[DeviceId, RouteEntry], groups: dict[DeviceId, GroupEntry]
    ) -> None:
        """Install startup tables on lit lanes; every lit lane counts as verified (not traced)."""
        self.tables = DeviceTables(self.id, version, dict(routes), dict(groups))
        for x, peer in self.expected_peer.items():
            self.hello_peer[x] = peer
            self.verified.add(x)
            self.lane_state[x] = LaneState.VERIFYING
        for entry in groups.values():
            for x in entry.lanes:
                if x not in self.verified:
                    raise ValueError(f"startup group {self.id}->{entry.neighbor} has dead lane {x}")
                self.in_group.add(x)
                self.lane_state[x] = LaneState.ACTIVE

    # ------------------------------------------------------------------ physical events

    def on_lanes_lost(self, lanes: list[LaneId], modules: list[ModuleId]) -> None:
        """Lanes lost light or their module: one LANE_DOWN now, prune after local_prune."""
        for x in lanes:
            self._gen[x] += 1
            self.verified.discard(x)
            self._set_state(x, LaneState.DOWN)
        cause = DownCause.MODULE_FAULT if modules else DownCause.LOSS_OF_LIGHT
        self._emit(
            LaneDown(
                self._header(MsgType.LANE_DOWN),
                device=self.id,
                lanes=tuple(lanes),
                cause=cause,
                modules=tuple(modules),
            )
        )
        self._clock.schedule(self._timing.local_prune, functools.partial(self._prune, lanes))

    def on_repair_start(self, lanes: list[LaneId]) -> None:
        """A failed module is repaired: its DOWN lanes wait for light."""
        for x in lanes:
            if self.lane_state[x] is LaneState.DOWN:
                self._set_state(x, LaneState.CONNECTING)

    def on_light(self, lane: LaneId, hello_from: LaneId) -> None:
        """Light is back on lane; the verification HELLO names the far-end lane."""
        self._gen[lane] += 1
        gen = self._gen[lane]
        self.hello_peer[lane] = hello_from
        self._set_state(lane, LaneState.VERIFYING)
        ok = self.expected_peer.get(lane) == hello_from
        self._tracer.emit(
            "hello",
            device=self.id,
            lane=lane,
            peer=hello_from,
            expected=self.expected_peer.get(lane),
            ok=ok,
        )
        if ok:
            self._clock.schedule(
                self._timing.hello_check, functools.partial(self._hello_ok, lane, gen)
            )
        else:
            self._clock.schedule(
                HELLO_MISSES * self._timing.hello_check,
                functools.partial(self._hello_timeout, lane, gen),
            )

    # ------------------------------------------------------------------ commands

    def handle(self, cmd: DeviceCommand) -> Ack | None:
        """Apply a command atomically (groups before routes); return the ACK, if any."""
        if cmd.version < self.tables.version:
            return self._ack(cmd, f"stale version {cmd.version} < {self.tables.version}")
        try:
            if cmd.prepare is not None:
                self._check_prepare(cmd.prepare)
            if cmd.group_set is not None:
                self._check_groups(cmd.group_set)
        except CommandRejected as err:
            return self._ack(cmd, str(err))
        if cmd.prepare is not None:
            self._apply_prepare(cmd.prepare)
        if cmd.group_set is not None:
            self._apply_groups(cmd.group_set)
        if cmd.route_set is not None:
            self._apply_routes(cmd.route_set)
        if cmd.keepalive is not None:
            self.keepalive_epoch = cmd.keepalive.epoch
        self.tables.version = cmd.version
        only_keepalive = cmd.parts() == (cmd.keepalive,)
        return None if only_keepalive else self._ack(cmd, "")

    def forward(self, dest: DeviceId, flow: str) -> tuple[DeviceId, LaneId] | None:
        """Next hop and lane for a flow (SPEC 5.1): ECMP over route neighbors with a group."""
        route = self.tables.routes.get(dest)
        if route is None:
            return None
        live = [n for n in route.neighbors if n in self.tables.groups]
        if not live:
            return None
        neighbor = live[zlib.crc32(f"{flow}|{self.id}".encode()) % len(live)]
        lanes = self.tables.groups[neighbor].lanes
        return neighbor, lanes[zlib.crc32(f"{flow}|{self.id}|{neighbor}".encode()) % len(lanes)]

    # ------------------------------------------------------------------ internals

    def _header(self, msg_type: MsgType, incident: int | None = None) -> Header:
        self._seq += 1
        version, now = self.tables.version, self._clock.now()
        return Header(msg_type, self._seq, version, incident, self.id, CONTROLLER, now)

    def _ack(self, cmd: DeviceCommand, error: str) -> Ack:
        header = self._header(MsgType.ACK, cmd.parts()[0].header.incident)
        return Ack(header, acked_seq=cmd.seq, ok=not error, error=error)

    def _set_state(self, lane: LaneId, state: LaneState) -> None:
        if self.lane_state[lane] is not state:
            self.lane_state[lane] = state
            self._tracer.emit("lane", device=self.id, lane=lane, state=state.value)

    def _prune(self, lanes: list[LaneId]) -> None:
        dead = {x for x in lanes if self.lane_state[x] is LaneState.DOWN and x in self.in_group}
        if not dead:
            return
        for neighbor in sorted(self.tables.groups):
            entry = self.tables.groups[neighbor]
            keep = tuple(x for x in entry.lanes if x not in dead)
            if keep == entry.lanes:
                continue
            if keep:
                self.tables.groups[neighbor] = GroupEntry(neighbor, keep)
            else:
                del self.tables.groups[neighbor]
            self._tracer.emit(
                "prune",
                device=self.id,
                neighbor=neighbor,
                before=list(entry.lanes),
                after=list(keep),
            )
        self.in_group -= dead
        self._service_changed(sorted(dead, key=lane_key))

    def _hello_ok(self, lane: LaneId, gen: int) -> None:
        if self._gen[lane] != gen:
            return
        self._tracer.emit("hello_ok", device=self.id, lane=lane)
        self._clock.schedule(
            self._timing.error_check, functools.partial(self._errors_clean, lane, gen)
        )

    def _errors_clean(self, lane: LaneId, gen: int) -> None:
        if self._gen[lane] != gen:
            return
        self.verified.add(lane)
        self._tracer.emit("errors_clean", device=self.id, lane=lane)
        if not self._pending_up:
            self._clock.schedule(0, self._flush_lane_up)  # one LANE_UP for lanes verified now
        self._pending_up.append(lane)

    def _flush_lane_up(self) -> None:
        lanes = sorted(self._pending_up, key=lane_key)
        self._pending_up = []
        self._emit(
            LaneUp(
                self._header(MsgType.LANE_UP),
                device=self.id,
                lanes=tuple(lanes),
                peer_lanes=tuple(self.hello_peer[x] for x in lanes),
            )
        )

    def _hello_timeout(self, lane: LaneId, gen: int) -> None:
        if self._gen[lane] != gen:
            return
        self._gen[lane] += 1
        self._set_state(lane, LaneState.DOWN)
        self._emit(
            LaneDown(
                self._header(MsgType.LANE_DOWN),
                device=self.id,
                lanes=(lane,),
                cause=DownCause.HELLO_TIMEOUT,
            )
        )

    def _check_prepare(self, msg: Prepare) -> None:
        for x in (*msg.open_lanes, *msg.expected_peer):
            if x not in self.lane_state:
                raise CommandRejected(f"PREPARE names lane {x}, which is not on {self.id}")

    def _check_groups(self, msg: GroupSet) -> None:
        for neighbor, lanes in msg.entries.items():
            for x in lanes:
                state = self.lane_state.get(x)
                if state is None:
                    raise CommandRejected(f"GROUP_SET names lane {x}, which is not on {self.id}")
                if not (state is LaneState.ACTIVE or x in self.verified):
                    raise CommandRejected(f"GROUP_SET names lane {x}, which is {state.value}")
                peer = self.hello_peer.get(x)
                if peer is None or self._index.lanes[peer].device != neighbor:
                    raise CommandRejected(f"lane {x} does not face {neighbor}")

    def _apply_prepare(self, msg: Prepare) -> None:
        self.expected_peer.update(msg.expected_peer)
        opened = []
        for x in msg.open_lanes:
            if self.lane_state[x] in (LaneState.IDLE, LaneState.DOWN):
                self._set_state(x, LaneState.CONNECTING)
                opened.append(x)
        if opened:
            self._lanes_opened(opened)

    def _apply_groups(self, msg: GroupSet) -> None:
        changed: set[LaneId] = set()
        for neighbor in sorted(msg.entries):
            new = tuple(sorted(msg.entries[neighbor], key=lane_key))
            old = self.tables.groups.get(neighbor)
            old_lanes = old.lanes if old is not None else ()
            for x in old_lanes:
                if x not in new:
                    self.in_group.discard(x)
                    if self.lane_state[x] is LaneState.ACTIVE:
                        self._set_state(x, LaneState.VERIFYING)
                    changed.add(x)
            for x in new:
                if x not in self.in_group:
                    self.in_group.add(x)
                    self._set_state(x, LaneState.ACTIVE)
                    changed.add(x)
            if new:
                self.tables.groups[neighbor] = GroupEntry(neighbor, new)
            else:
                self.tables.groups.pop(neighbor, None)
        if changed:
            self._service_changed(sorted(changed, key=lane_key))

    def _apply_routes(self, msg: RouteSet) -> None:
        for dest, entry in msg.entries.items():
            if entry.neighbors:
                self.tables.routes[dest] = RouteEntry(dest, entry.neighbors, entry.hops)
            else:
                self.tables.routes.pop(dest, None)
