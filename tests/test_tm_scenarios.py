"""Phase 3 exit checks with the simulator: G2's incident, G1's capacities, G6's lost neighbors,
and the Topology Manager tracking a whole G4a OCS restore."""

from typing import Any

from sdnctl.interfaces import RepairPlan
from sdnctl.messages import DeviceCommand, DeviceEvent, OcsDone, OcsSet, Prepare
from sdnctl.model import Incident
from sdnctl.types import CrossConnect, LaneState, MsgType
from tests.sim_helpers import TmController, captured, ms
from tests.test_view import capacity


def run_failure(name: str, two_plus_two: bool, spares: int, ocs: bool,
                modules: list[str]) -> tuple[Any, TmController]:
    """Fail modules at t=0 under a TmController and run 20 ms."""
    sim, made = captured(cls=TmController)
    sim.load_topology(name, two_plus_two, spares)
    sim.configure(ocs=ocs)
    sim.inject(0, "module_fail", modules=modules)
    sim.run(20)
    ctl: TmController = made[-1]
    return sim, ctl


def pairs(incident: Incident) -> list[list[str]]:
    """Lost adjacencies in golden.json's format and order."""
    return sorted([list(p) for p in incident.lost])


def test_g2_three_reports_become_one_incident(golden: dict[str, Any]) -> None:
    """Exit check: 3 reports -> 1 incident, failed device l1-1536."""
    _, ctl = run_failure("topology1", False, 0, False, ["l1-1536.m0"])
    g2 = golden["G2"]
    [(handed_over, incident)] = ctl.incidents
    assert ms(handed_over) == g2["timeline_typical"]["down"]["lane_down_at_controller"]
    assert sorted(r.device for r in incident.reports) == g2["reporters"]
    assert incident.failed_device == "l1-1536"
    assert incident.failed_modules == ("l1-1536.m0",)
    assert incident.lanes >= set(g2["module_lanes"])
    assert len(incident.lanes) == 8  # 4 own lanes and the 4 lanes facing them
    assert pairs(incident) == sorted(g2["lost_adjacencies"])


def test_g1_capacities_in_the_topology_manager(golden: dict[str, Any]) -> None:
    """Exit check: G1's capacities, from the controller's own state."""
    _, ctl = run_failure("topology1", True, 0, False, ["l1-1536.m0"])
    [(_, incident)] = ctl.incidents
    assert len(incident.reports) == 5 and incident.failed_device == "l1-1536"
    assert incident.lost == frozenset()
    view = ctl.tm.view()
    for key, value in golden["G1"]["capacity"].items():
        assert round(capacity(view, key), 6) == value, key


def test_g6_neighbor_lost(golden: dict[str, Any]) -> None:
    _, ctl = run_failure("topology2", True, 0, False, ["npu-0.m0", "npu-0.m1"])
    g6 = golden["G6"]
    [(_, incident)] = ctl.incidents
    assert sorted(r.device for r in incident.reports) == g6["reporters"]
    assert incident.failed_modules == ("npu-0.m0", "npu-0.m1")
    assert pairs(incident) == sorted(g6["lost_adjacencies"])


def test_g4a_restore_tracked_by_the_topology_manager(golden: dict[str, Any]) -> None:
    g = golden["G4a"]
    sim, made = captured(cls=TmController)
    sim.load_topology("topology2", True, 1)
    sim.configure(ocs=True)
    sim.inject(0, "module_fail", modules=["npu-0.m0"])
    ctl: TmController = made[-1]
    ocs = ctl.ctx.ocs
    assert ocs is not None
    lane_at = {x.ocs_port: x.id for x in sim.spec.lanes if x.ocs_port is not None}
    connect = tuple(CrossConnect(n, s) for n, s in g["ocs_connect"])
    disconnect = tuple(CrossConnect(n, s) for n, s in g["ocs_disconnect"])
    plan = RepairPlan("npu-0", ("npu-0.m0",), g["spare"], disconnect, connect,
                      frozenset({"npu-0", "npu-64"}), {})
    sent: list[dict[str, Any]] = []

    def plan_done() -> None:
        for dev, expected in (
            ("npu-0", {lane_at[xc.north]: lane_at[xc.south] for xc in connect}),
            ("npu-64", {lane_at[xc.south]: lane_at[xc.north] for xc in connect}),
        ):
            msg = Prepare(ctl.header(MsgType.PREPARE, dev), tuple(expected), expected)
            ctl.ctx.devices.send(dev, DeviceCommand(prepare=msg))
        ocs.apply(OcsSet(ctl.ocs_header(), disconnect, connect))

    def on_incident(incident: Incident) -> None:
        assert incident.failed_device == "npu-0"
        ctl.ctx.clock.schedule(ctl.ctx.config.timing().plan, plan_done)

    def on_event(ev: DeviceEvent | OcsDone) -> None:
        view = ctl.tm.view()
        if ev.header.type is MsgType.LANE_UP and len(ctl.received(MsgType.LANE_UP)) == 2:
            for dev, nbr in (("npu-0", "npu-64"), ("npu-64", "npu-0")):
                lanes = view.usable_lanes(dev, nbr)  # the TM decides what goes in the group
                sent.append({"device": dev, "neighbor": nbr, "lanes": list(lanes)})
                ctl.send_groups(dev, {nbr: lanes})
        restored = [*g["spare_lanes"], "npu-64.p13.0", "npu-64.p14.0"]
        if ev.header.type is MsgType.ACK and all(
            view.lane_state(x) is LaneState.ACTIVE for x in restored
        ):
            ctl.tm.record_repair(plan, ctl.ctx.clock.now())  # close: both groups installed

    ctl.on_incident = on_incident
    ctl.on_event = on_event
    sim.run(50)
    view = ctl.tm.view()
    for key, value in g["capacity_during"].items():
        assert round(capacity(view, key), 6) == value, key
    assert view.lane_state("npu-0.p15.0") is LaneState.CONNECTING  # OCS_DONE came at 32 ms

    sim.run(500)
    assert sent == g["group_set"]
    view = ctl.tm.view()
    assert view.pair_capacity("npu-0", "npu-64") == sim.capacity("npu-0/npu-64") == 1.0
    assert view.domain_capacity("npu-0.d0") == sim.capacity("npu-0.d0") == 1.0
    assert view.device_capacity("npu-0") == sim.capacity("npu-0") == 1.0
    assert view.failed_modules() == {"npu-0.m0"}
    assert g["spare"] not in view.spare_pool()
    assert [view.lane_state(x) for x in g["module_lanes"]] == [LaneState.FAILED] * 2
    assert list(view.xconnects()) == sim.ocs_matrix()
    for dev in ("npu-0", "npu-64"):
        device = sim.device(dev)
        tm_active = {x for x in device.lane_state if view.lane_state(x) is LaneState.ACTIVE}
        assert tm_active == device.in_group, dev
