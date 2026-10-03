"""Spare picker and repair planner (SPEC 7.2, step 2).

- Failed device: the one that reported MODULE_FAULT; without one the failed end is unclear.
- Spare: the lowest-numbered spare module of the failed domain on the failed device that is still
  in the spare pool and not failed.
- Dead lanes: the failed modules' cross-connected lanes, in module order, then lane order. The
  spare must have at least as many lanes; a partial restore is a later addition.
- OCS pairs: disconnect each dead lane's cross-connect and connect the next spare lane to the same
  peer lane. Targets: the failed device and the device of every peer lane.
- PREPARE: the failed device opens the spare lanes it will use and expects each new peer lane; each
  peer device expects its new peer lane. Headers carry seq 0 and time 0; the scheduler stamps them
  when it sends.
"""

from sdnctl.interfaces import NoRepair, RepairPlan
from sdnctl.messages import CONTROLLER, Header, Prepare
from sdnctl.model import Domain, Incident, Module, TopologyView
from sdnctl.ocs.matrix import OcsMatrix, joining
from sdnctl.types import CrossConnect, DeviceId, LaneId, ModuleId, MsgType, device_key, lane_key


def module_number(module: ModuleId) -> int:
    """The number of a module: npu-0.m2 -> 2."""
    return int(module.rsplit(".m", 1)[1])


def pick_spare(domain: Domain, view: TopologyView) -> ModuleId | None:
    """The lowest-numbered spare of a domain that is still free and not failed."""
    pool, failed = view.spare_pool(), view.failed_modules()
    free = [m for m in domain.spare_modules if m in pool and m not in failed]
    return min(free, key=module_number) if free else None


def plan_repair(
    incident: Incident,
    view: TopologyView,
    matrix: OcsMatrix,
    modules: dict[ModuleId, Module],
    domains: dict[str, Domain],
) -> RepairPlan | NoRepair:
    """The OCS repair plan for an incident, or why there is none."""
    device = incident.failed_device
    failed = sorted(
        (m for m in incident.failed_modules if modules[m].device == device), key=module_number
    )
    if device is None or not failed:
        return NoRepair("failed_end_unclear")
    spare = pick_spare(domains[modules[failed[0]].domain], view)
    if spare is None:
        return NoRepair("no_spare")
    dead = [
        x
        for m in failed
        for x in sorted(modules[m].lanes, key=lane_key)
        if x in matrix.port_of and matrix.peer_port(matrix.port_of[x]) is not None
    ]
    spare_lanes = sorted(modules[spare].lanes, key=lane_key)
    if not dead:
        return NoRepair("no_spare")  # nothing the OCS could move
    if len(spare_lanes) < len(dead):
        return NoRepair("not_enough_spare")

    disconnect: list[CrossConnect] = []
    connect: list[CrossConnect] = []
    new_peer: dict[LaneId, LaneId] = {}  # spare lane -> the peer lane it takes over
    for x, s in zip(dead, spare_lanes, strict=False):
        port, spare_port = matrix.port_of[x], matrix.port_of[s]
        peer_port = matrix.peer_port(port)
        assert peer_port is not None
        disconnect.append(joining(port, peer_port))
        connect.append(joining(spare_port, peer_port))
        new_peer[s] = matrix.lane_at[peer_port]

    lane_device = {x.id: x.device for x in view.spec.lanes if x.id in new_peer.values()}
    targets = {device} | {lane_device[y] for y in new_peer.values()}
    prepares: dict[DeviceId, Prepare] = {}
    for target in sorted(targets, key=device_key):
        header = Header(MsgType.PREPARE, 0, view.version, incident.id, CONTROLLER, target, 0)
        if target == device:
            prepares[target] = Prepare(header, tuple(new_peer), dict(new_peer))
        else:
            expect = {y: s for s, y in new_peer.items() if lane_device[y] == target}
            prepares[target] = Prepare(header, (), expect)
    return RepairPlan(
        failed_device=device,
        failed_modules=tuple(failed),
        spare_module=spare,
        disconnect=tuple(disconnect),
        connect=tuple(connect),
        targets=frozenset(targets),
        prepares=prepares,
    )
