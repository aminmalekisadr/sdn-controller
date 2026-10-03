"""The SDN Scheduler (SPEC 7.1-7.4): runs each incident and is the only sender of commands.

Event loop: device and OCS events go to the Topology Manager (OCS_DONE with the matrix read back),
each LANE_DOWN's local prune is mirrored in the controller's copy of the tables, incident
correlation is polled on time, and KEEPALIVE goes to every device at startup.

Incident runner (SPEC 7.2, timing rules of 11.0), one active incident at a time:
1. Plan takes `plan`; the OCS controller plans a repair (or says why not).
2. A lost neighbor: the Routing Engine takes `compute`, then down-waves (transit, then endpoints).
3. A repair: PREPARE to the targets and OCS_SET, both when planning ends; then the read-back check.
4. No repair: after any reroute the incident parks as DEGRADED_WAITING_REPAIR or DEGRADED_NO_SPARE
   and frees the active slot until its lanes come back.
5. Activation, once every new lane is up at both ends: a returning neighbor takes `compute` and
   restore waves (endpoints batched group+route, then transit); otherwise one GROUP_SET wave.
6. Close when the last ACK arrives: record the repair; CLOSED if every touched pair is back to
   capacity 1.0, else DEGRADED.
Timeouts (SPEC 7.3): ACK and OCS_DONE with one resend, verify after OCS_DONE; on failure the
incident ends FAILED_NEEDS_OPERATOR and nothing is rolled back.
"""

import dataclasses
import functools
from collections import deque
from collections.abc import Callable, Sequence

from sdnctl.interfaces import (
    ControllerContext,
    IncidentOutcome,
    IOcsMatrixController,
    IRoutingEngine,
    ITopologyManager,
    RepairPlan,
)
from sdnctl.messages import (
    CONTROLLER,
    OCS,
    Ack,
    DeviceCommand,
    DeviceEvent,
    GroupSet,
    Header,
    Keepalive,
    LaneDown,
    LaneUp,
    OcsDone,
    OcsSet,
    Prepare,
    RouteSet,
)
from sdnctl.scheduler.commands import (
    GroupEntries,
    RouteEntries,
    apply_to_copy,
    full_group_entries,
    full_route_entries,
    group_entries,
    route_entries,
)
from sdnctl.scheduler.incident import IncidentRun
from sdnctl.scheduler.sender import CommandSender
from sdnctl.tables import AdjacencyChange, GroupEntry, TableDiff, Tables, Wave
from sdnctl.types import CrossConnect, DeviceId, IncidentState, LaneId, MsgType, SimTime

CONTROLLER_ID = "sdnctl"
EPOCH = 1  # v1 has one controller session
WAITING_REASONS = frozenset({"no_ocs", "failed_end_unclear"})


class Scheduler:
    """IScheduler: runs incidents, sends every command, and keeps the controller's table copy."""

    def __init__(
        self,
        ctx: ControllerContext,
        tm: ITopologyManager,
        routing: IRoutingEngine,
        ocs: IOcsMatrixController,
        installed: Tables,
    ) -> None:
        self._ctx = ctx
        self._tm = tm
        self._routing = routing
        self._ocs = ocs
        self._timing = ctx.config.timing()
        self._timeouts = ctx.config.timeouts_us
        self._limits = ctx.config.limits
        self.installed = installed  # the controller's copy of every device's tables
        self.table_version = max((t.version for t in installed.values()), default=1)
        self.runs: list[IncidentRun] = []
        self._queue: deque[IncidentRun] = deque()
        self._active: IncidentRun | None = None
        self._lane_device = {x.id: x.device for x in ctx.spec.lanes}
        self._seq = 0
        self._sender = CommandSender(
            ctx.devices,
            ctx.clock,
            self._timeouts.ack,
            self._limits.ack_resends,
            self._acked,
            self._emit,
        )

    # ------------------------------------------------------------------ event loop

    def start(self) -> None:
        """Take events from the adapters and open the session with every device (KEEPALIVE)."""
        self._ctx.devices.set_event_sink(self.on_device_event)
        if self._ctx.ocs is not None:
            self._ctx.ocs.set_event_sink(self.on_ocs_event)
        for d in self._ctx.spec.devices:
            keepalive = Keepalive(self._header(MsgType.KEEPALIVE, d.id, None), CONTROLLER_ID, EPOCH)
            self._ctx.devices.send(d.id, DeviceCommand(keepalive=keepalive))
        self._emit("controller_start", devices=len(self._ctx.spec.devices),
                   ocs=self._ocs.enabled())

    def on_device_event(self, ev: DeviceEvent) -> None:
        """LANE_DOWN, LANE_UP or ACK from a device."""
        now = self._ctx.clock.now()
        if isinstance(ev, LaneDown):
            self._tm.on_lane_down(ev, now)
            self._prune_copy(ev)
            deadline = self._tm.correlation_deadline()
            if deadline is not None:
                self._ctx.clock.schedule(max(0, deadline - now), self._poll)
        elif isinstance(ev, LaneUp):
            self._tm.on_lane_up(ev, now)
            self._lanes_came_up()
        elif isinstance(ev, Ack):
            self._sender.on_ack(ev)

    def on_ocs_event(self, ev: OcsDone) -> None:
        """OCS_DONE: the Topology Manager and the OCS controller take the matrix read back."""
        assert self._ctx.ocs is not None
        read_back = self._ctx.ocs.read_matrix()
        self._tm.on_ocs_done(ev, read_back)
        run = self._active
        if run is not None and run.ocs_cmd is not None and not run.ocs_applied:
            if ev.header.incident == run.incident.id:
                self._ocs_check(run, read_back, answered=True)

    def on_timer(self, now: SimTime) -> None:
        """Correlation windows: hand new incidents to the runner."""
        for incident in self._tm.poll_incidents(now):
            run = IncidentRun(incident)
            self.runs.append(run)
            self._emit("incident_open", incident=incident.id,
                       failed_device=incident.failed_device, reports=len(incident.reports))
            self._queue.append(run)
        self._next()

    def outcomes(self) -> tuple[IncidentOutcome, ...]:
        """Each incident's end state, or its state so far."""
        return tuple(
            IncidentOutcome(r.incident.id, r.state, r.changed_at, r.incident.reason)
            for r in self.runs
        )

    # ------------------------------------------------------------------ incident runner

    def _next(self) -> None:
        """Start the next queued incident when no incident is active."""
        while self._active is None and self._queue:
            run = self._queue.popleft()
            if run.finished():
                continue
            self._active = run
            if run.ready:
                self._activate(run)
            else:
                self._ctx.clock.schedule(self._timing.plan, functools.partial(self._planned, run))

    def _planned(self, run: IncidentRun) -> None:
        """Planning is done: reroute if a neighbor was lost, repair through the OCS if possible."""
        view = self._tm.view()
        run.touched = frozenset(
            (self._lane_device[x], self._lane_device[y])
            for x in run.incident.lanes
            if (y := view.peer_of(x)) is not None
        )
        result = self._ocs.plan_repair(run.incident, view)
        self._emit("plan_done", incident=run.incident.id,
                   plan="repair" if isinstance(result, RepairPlan) else result.reason)
        if run.incident.lost:
            run.rerouting = True
            self._set_state(run, IncidentState.REROUTING)
            self._ctx.clock.schedule(self._timing.compute, functools.partial(self._reroute, run))
        if isinstance(result, RepairPlan):
            run.plan = result
            run.new_lanes = frozenset(x for p in result.prepares.values() for x in p.expected_peer)
            self._set_state(run, IncidentState.REPAIRING)
            self._prepare(run)
            self._send_ocs(run)
        else:
            run.no_repair = result
            if not run.rerouting:
                self._park(run)

    def _reroute(self, run: IncidentRun) -> None:
        """Compute is done: push the new routes in down-waves."""
        if run.finished():
            return
        self._emit("compute_done", incident=run.incident.id)
        change = AdjacencyChange(lost=run.incident.lost, restored=frozenset())
        self._push(run, change, functools.partial(self._rerouted, run))

    def _rerouted(self, run: IncidentRun) -> None:
        run.rerouting = False
        if run.no_repair is not None:
            self._park(run)
        else:
            self._maybe_activate(run)

    def _park(self, run: IncidentRun) -> None:
        """No repair: wait, without holding the active slot, until the lanes come back."""
        assert run.no_repair is not None
        reason = run.no_repair.reason
        run.incident.reason = reason
        run.new_lanes = run.incident.lanes
        state = (
            IncidentState.DEGRADED_WAITING_REPAIR
            if reason in WAITING_REASONS
            else IncidentState.DEGRADED_NO_SPARE
        )
        self._set_state(run, state)
        self._release(run)

    def _lanes_came_up(self) -> None:
        """After a LANE_UP: activate the active incident or a parked one whose lanes are all up."""
        if self._active is not None:
            self._maybe_activate(self._active)
        for run in self.runs:
            if run.parked() and not run.ready and self._lanes_up(run.new_lanes):
                run.ready = True
                self._emit("incident_ready", incident=run.incident.id)
                self._queue.append(run)
        self._next()

    def _maybe_activate(self, run: IncidentRun) -> None:
        if run.activating or run.rerouting or run.finished() or run.parked():
            return
        if run.plan is not None and not run.ocs_applied:
            return
        if self._lanes_up(run.new_lanes):
            self._activate(run)

    def _activate(self, run: IncidentRun) -> None:
        """Every new lane is up at both ends: restore groups (and routes, if a neighbor is back)."""
        run.activating = True
        run.timer += 1  # the verify timeout no longer applies
        self._set_state(run, IncidentState.ACTIVATING)
        view = self._tm.view()
        restored = frozenset((v, u) for v, u in run.incident.lost if u in view.neighbors(v))
        change = AdjacencyChange(lost=frozenset(), restored=restored)
        delay = self._timing.compute if restored else 0
        self._ctx.clock.schedule(delay, functools.partial(self._push_restore, run, change))

    def _push_restore(self, run: IncidentRun, change: AdjacencyChange) -> None:
        if change.restored:
            self._emit("compute_done", incident=run.incident.id)
        self._push(run, change, functools.partial(self._close, run))

    def _close(self, run: IncidentRun) -> None:
        """The last ACK is in: record the repair; CLOSED if every touched pair is back to 1.0."""
        now = self._ctx.clock.now()
        if run.plan is not None:
            self._tm.record_repair(run.plan, now)
        view = self._tm.view()
        capacity = min((view.pair_capacity(a, b) for a, b in run.touched), default=1.0)
        if capacity >= 1.0:
            self._finish(run, IncidentState.CLOSED, "")
        else:
            self._finish(run, IncidentState.DEGRADED, f"pair capacity {capacity:.6f}")

    def _fail(self, run: IncidentRun, reason: str) -> None:
        if not run.finished():
            self._finish(run, IncidentState.FAILED_NEEDS_OPERATOR, reason)

    def _finish(self, run: IncidentRun, state: IncidentState, reason: str) -> None:
        run.incident.reason = reason
        run.timer += 1
        self._set_state(run, state)
        self._release(run)

    def _release(self, run: IncidentRun) -> None:
        if self._active is run:
            self._active = None
            self._next()

    def _set_state(self, run: IncidentRun, state: IncidentState) -> None:
        run.incident.state = state
        if run.parked() or run.finished():
            run.changed_at = self._ctx.clock.now()
        self._emit("incident_state", incident=run.incident.id, state=state.value,
                   reason=run.incident.reason)

    def _lanes_up(self, lanes: frozenset[LaneId]) -> bool:
        """True when every lane is usable: up and verified at both ends."""
        view = self._tm.view()
        for x in lanes:
            y = view.peer_of(x)
            if y is None:
                return False
            if x not in view.usable_lanes(self._lane_device[x], self._lane_device[y]):
                return False
        return True

    # ------------------------------------------------------------------ waves

    def _push(self, run: IncidentRun, change: AdjacencyChange, done: Callable[[], None]) -> None:
        """Compute target tables and send the differences in waves; call done after the last."""
        target = self._routing.compute(self._tm.view())
        diff = self._routing.diff(self.installed, target)
        waves = self._routing.order_waves(diff, change)
        self.table_version += 1
        self._emit("waves", incident=run.incident.id, sizes=[len(w.devices) for w in waves],
                   route_entries=diff.route_entries(), group_entries=diff.group_entries())
        self._wave(run, target, diff, waves, 0, done)

    def _wave(self, run: IncidentRun, target: Tables, diff: TableDiff, waves: list[Wave],
              i: int, done: Callable[[], None]) -> None:
        if run.finished():
            return
        if i == len(waves):
            done()
            return
        wave = waves[i]
        pending = set(wave.devices)
        self._emit("wave_sent", incident=run.incident.id, wave=wave.index,
                   devices=len(wave.devices))

        def acked(device: DeviceId, _: DeviceCommand) -> None:
            pending.discard(device)
            if not pending and not run.finished():
                self._emit("wave_acked", incident=run.incident.id, wave=wave.index)
                self._wave(run, target, diff, waves, i + 1, done)

        for d in wave.devices:
            groups = group_entries(diff.groups.get(d, [])) or None
            routes = route_entries(diff.routes.get(d, [])) or None
            self._sender.send(
                d,
                self._tables_command(d, groups, routes, run.incident.id),
                on_ok=functools.partial(acked, d),
                on_fail=functools.partial(self._fail, run),
                resend=functools.partial(self._full_tables, d, target, run.incident.id),
            )

    def _full_tables(self, device: DeviceId, target: Tables, incident: int) -> DeviceCommand:
        """The device's full target tables as one batched command (the ACK-timeout resend)."""
        copy = self.installed.get(device)
        return self._tables_command(
            device,
            full_group_entries(target[device], copy),
            full_route_entries(target[device], copy),
            incident,
        )

    def _acked(self, device: DeviceId, cmd: DeviceCommand) -> None:
        """A device applied a command: update the copy; installed groups make lanes ACTIVE."""
        copy = self.installed.get(device)
        if copy is not None:
            apply_to_copy(copy, cmd)
        if cmd.group_set is not None:
            self._tm.on_groups_installed(device, cmd.group_set, self._ctx.clock.now())

    # ------------------------------------------------------------------ OCS repair

    def _prepare(self, run: IncidentRun) -> None:
        """PREPARE to every target: open the spare lanes, expect the new peer lanes."""
        assert run.plan is not None
        for device, msg in run.plan.prepares.items():
            make = functools.partial(self._prepare_command, device, msg, run.incident.id)
            self._sender.send(device, make(), on_ok=lambda cmd: None,
                              on_fail=functools.partial(self._fail, run), resend=make)

    def _prepare_command(self, device: DeviceId, msg: Prepare, incident: int) -> DeviceCommand:
        header = self._header(MsgType.PREPARE, device, incident)
        return DeviceCommand(prepare=dataclasses.replace(msg, header=header))

    def _send_ocs(self, run: IncidentRun) -> None:
        """Send OCS_SET (or resend it) and start its OCS_DONE timeout."""
        assert run.plan is not None and self._ctx.ocs is not None
        cmd = OcsSet(self._header(MsgType.OCS_SET, OCS, run.incident.id),
                     run.plan.disconnect, run.plan.connect)
        run.ocs_cmd = cmd
        run.ocs_sends += 1
        run.timer += 1
        self._ctx.ocs.apply(cmd)
        self._ctx.clock.schedule(self._timeouts.ocs_done,
                                 functools.partial(self._ocs_timeout, run, run.timer))

    def _ocs_timeout(self, run: IncidentRun, timer: int) -> None:
        """No OCS_DONE in time: read the matrix back and decide."""
        if run.timer != timer or run.finished() or run.ocs_applied:
            return
        assert self._ctx.ocs is not None
        self._emit("ocs_timeout", incident=run.incident.id, sends=run.ocs_sends)
        self._ocs_check(run, self._ctx.ocs.read_matrix(), answered=False)

    def _ocs_check(
        self, run: IncidentRun, read_back: Sequence[CrossConnect], answered: bool
    ) -> None:
        """Read-back check: continue if applied, else resend once, else give up."""
        assert run.ocs_cmd is not None
        applied = self._ocs.on_applied(run.ocs_cmd, read_back)
        self._emit("ocs_check", incident=run.incident.id, applied=applied, answered=answered)
        if applied:
            if not answered:  # OCS_DONE was lost: the TM still needs the new matrix
                self._tm.on_ocs_done(self._synthetic_done(run), read_back)
            run.ocs_applied = True
            run.timer += 1
            self._ctx.clock.schedule(self._timeouts.verify,
                                     functools.partial(self._verify_timeout, run, run.timer))
            self._maybe_activate(run)
        elif run.ocs_sends <= self._limits.ocs_resends:
            self._emit("resend", incident=run.incident.id, what="OCS_SET")
            self._send_ocs(run)
        else:
            self._fail(run, f"OCS change not applied after {run.ocs_sends} OCS_SET")

    def _verify_timeout(self, run: IncidentRun, timer: int) -> None:
        if run.timer == timer and not run.activating and not run.finished():
            self._fail(run, "new lanes not verified in time")

    def _synthetic_done(self, run: IncidentRun) -> OcsDone:
        header = Header(MsgType.OCS_DONE, 0, self.table_version, run.incident.id, OCS,
                        CONTROLLER, self._ctx.clock.now())
        return OcsDone(header, (), (), matrix_version=0)

    # ------------------------------------------------------------------ helpers

    def _poll(self) -> None:
        self.on_timer(self._ctx.clock.now())

    def _prune_copy(self, msg: LaneDown) -> None:
        """The device prunes the lanes it lost; do the same to the controller's copy."""
        tables = self.installed.get(msg.device)
        if tables is None:
            return
        dead = set(msg.lanes)
        for neighbor, entry in list(tables.groups.items()):
            keep = tuple(x for x in entry.lanes if x not in dead)
            if not keep:
                del tables.groups[neighbor]
            elif keep != entry.lanes:
                tables.groups[neighbor] = GroupEntry(neighbor, keep)

    def _tables_command(self, device: DeviceId, groups: GroupEntries | None,
                        routes: RouteEntries | None, incident: int) -> DeviceCommand:
        """GROUP_SET and/or ROUTE_SET for one device, batched under one seq."""
        self._seq += 1
        now = self._ctx.clock.now()

        def header(t: MsgType) -> Header:
            return Header(t, self._seq, self.table_version, incident, CONTROLLER, device, now)

        return DeviceCommand(
            group_set=GroupSet(header(MsgType.GROUP_SET), groups) if groups is not None else None,
            route_set=RouteSet(header(MsgType.ROUTE_SET), routes) if routes is not None else None,
        )

    def _header(self, msg_type: MsgType, dst: str, incident: int | None) -> Header:
        self._seq += 1
        return Header(msg_type, self._seq, self.table_version, incident, CONTROLLER, dst,
                      self._ctx.clock.now())

    def _emit(self, kind: str, **fields: object) -> None:
        self._ctx.trace.record({"t": self._ctx.clock.now(), "kind": kind, **fields})
