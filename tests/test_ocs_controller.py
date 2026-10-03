"""OCS Matrix Controller (Phase 5): repair plans for G4a/G4b, G5, G6b, the read-back check,
and the planned repair carried out in the simulator."""

import dataclasses
from typing import Any

import pytest

from sdnctl.interfaces import NoRepair, RepairPlan
from sdnctl.messages import (
    CONTROLLER,
    DeviceCommand,
    DeviceEvent,
    Header,
    LaneDown,
    OcsDone,
    OcsSet,
)
from sdnctl.model import Incident
from sdnctl.ocs import NullOcsController, OcsMatrixController
from sdnctl.topology_manager import TopologyManager
from sdnctl.types import CrossConnect, DownCause, LaneState, MsgType
from tests.conftest import built
from tests.sim_helpers import TmController, captured, ms
from tests.tm_helpers import report_failure


def setup(two_plus_two: bool, spares: int, modules: tuple[str, ...]) -> tuple[
    TopologyManager, Incident, OcsMatrixController
]:
    """Topology 2 with modules failed and reported; a fresh OCS controller."""
    spec = built("topology2", two_plus_two, spares)
    tm = TopologyManager(correlation_window=5000)
    tm.load(spec)
    incident, _ = report_failure(tm, spec, modules)
    return tm, incident, OcsMatrixController(spec)


def xcs(pairs: list[list[str]]) -> tuple[CrossConnect, ...]:
    """golden.json pairs as cross-connects."""
    return tuple(CrossConnect(n, s) for n, s in pairs)


@pytest.mark.parametrize(("key", "two_plus_two"), [("G4a", True), ("G4b", False)])
def test_g4_plan_matches_golden(key: str, two_plus_two: bool, golden: dict[str, Any]) -> None:
    g = golden[key]
    tm, incident, ocs = setup(two_plus_two, 1, ("npu-0.m0",))
    plan = ocs.plan_repair(incident, tm.view())
    assert isinstance(plan, RepairPlan)
    assert plan.failed_device == "npu-0" and plan.failed_modules == ("npu-0.m0",)
    assert plan.spare_module == g["spare"]
    assert plan.disconnect == xcs(g["ocs_disconnect"])
    assert plan.connect == xcs(g["ocs_connect"])
    assert plan.targets == {"npu-0", "npu-64"}
    view = tm.view()
    peers = [view.lane_by_ocs_port(s) for _, s in g["ocs_connect"]]
    npu0, npu64 = plan.prepares["npu-0"], plan.prepares["npu-64"]
    assert list(npu0.open_lanes) == g["spare_lanes"]
    assert npu0.expected_peer == dict(zip(g["spare_lanes"], peers, strict=True))
    assert npu64.open_lanes == ()
    assert npu64.expected_peer == dict(zip(peers, g["spare_lanes"], strict=True))
    for dev, msg in plan.prepares.items():
        assert msg.header.type is MsgType.PREPARE and msg.header.dst == dev
        assert msg.header.incident == incident.id


def test_g5_no_spare(golden: dict[str, Any]) -> None:
    tm, incident, ocs = setup(True, 0, ("npu-0.m0",))
    assert golden["G5"]["spares"] == []
    assert ocs.plan_repair(incident, tm.view()) == NoRepair("no_spare")


def test_g6b_not_enough_spare(golden: dict[str, Any]) -> None:
    tm, incident, ocs = setup(True, 1, ("npu-0.m0", "npu-0.m1"))
    assert golden["G6"]["variant_b_ocs_on_one_spare"]["plan"] == "NoRepair(not_enough_spare)"
    assert ocs.plan_repair(incident, tm.view()) == NoRepair("not_enough_spare")


def test_failed_end_unclear_without_module_fault() -> None:
    spec = built("topology2", True, 1)
    tm = TopologyManager(correlation_window=5000)
    tm.load(spec)
    header = Header(MsgType.LANE_DOWN, 1, 1, None, "npu-64", CONTROLLER, 1000)
    tm.on_lane_down(LaneDown(header, "npu-64", ("npu-64.p13.0",), DownCause.LOSS_OF_LIGHT), 1000)
    [incident] = tm.poll_incidents(6000)
    assert OcsMatrixController(spec).plan_repair(incident, tm.view()) == NoRepair(
        "failed_end_unclear"
    )


def test_null_controller() -> None:
    tm, incident, _ = setup(True, 1, ("npu-0.m0",))
    null = NullOcsController()
    assert not null.enabled()
    assert null.plan_repair(incident, tm.view()) == NoRepair("no_ocs")
    with pytest.raises(RuntimeError):
        null.on_applied(OcsSet(Header(MsgType.OCS_SET, 1, 1, 1, CONTROLLER, "ocs", 0), (), ()), [])


def test_spare_picker_takes_the_lowest_free_spare() -> None:
    spec = built("topology2", True, 2)
    tm = TopologyManager(correlation_window=5000)
    tm.load(spec)
    ocs = OcsMatrixController(spec)
    first, _ = report_failure(tm, spec, ("npu-0.m0",))
    plan = ocs.plan_repair(first, tm.view())
    assert isinstance(plan, RepairPlan) and plan.spare_module == "npu-0.m2"
    assert plan.connect == (CrossConnect("N257", "S1"), CrossConnect("N258", "S3"))
    tm.record_repair(plan, 100_000)  # npu-0.m2 leaves the pool
    second, _ = report_failure(tm, spec, ("npu-0.m1",), now=200_000)
    plan = ocs.plan_repair(second, tm.view())
    assert isinstance(plan, RepairPlan) and plan.spare_module == "npu-0.m3"
    assert plan.connect == (CrossConnect("N259", "S2"), CrossConnect("N260", "S4"))


def test_failure_on_the_south_side() -> None:
    tm, incident, ocs = setup(True, 1, ("npu-64.m0",))
    plan = ocs.plan_repair(incident, tm.view())
    assert isinstance(plan, RepairPlan) and plan.spare_module == "npu-64.m2"
    assert plan.disconnect == (CrossConnect("N1", "S1"), CrossConnect("N3", "S3"))
    assert plan.connect == (CrossConnect("N1", "S257"), CrossConnect("N3", "S258"))
    assert plan.prepares["npu-64"].open_lanes == ("npu-64.p15.0", "npu-64.p15.1")
    assert plan.prepares["npu-0"].expected_peer == {
        "npu-0.p13.0": "npu-64.p15.0",
        "npu-0.p14.0": "npu-64.p15.1",
    }


def test_read_back_check() -> None:
    tm, incident, ocs = setup(True, 1, ("npu-0.m0",))
    plan = ocs.plan_repair(incident, tm.view())
    assert isinstance(plan, RepairPlan)
    cmd = OcsSet(Header(MsgType.OCS_SET, 1, 2, 1, CONTROLLER, "ocs", 2000),
                 plan.disconnect, plan.connect)
    before = sorted(ocs.matrix.xconnects(), key=lambda xc: int(xc.north[1:]))
    expected = ocs.matrix.after(plan.disconnect, plan.connect)
    assert not ocs.on_applied(cmd, before)  # nothing applied
    half = (set(before) - {plan.disconnect[0]}) | {plan.connect[0]}
    assert not ocs.on_applied(cmd, sorted(half, key=lambda xc: int(xc.north[1:])))
    ocs.matrix.replace(before)
    assert ocs.on_applied(cmd, sorted(expected, key=lambda xc: int(xc.north[1:])))
    assert ocs.matrix.xconnects() == expected
    assert ocs.on_applied(cmd, sorted(expected, key=lambda xc: int(xc.north[1:])))  # idempotent


class PlannedRestore:
    """Plays the scheduler's part of an OCS restore, using the OCS controller's own plan."""

    def __init__(self, ctl: TmController) -> None:
        self.ctl = ctl
        self.ocs = OcsMatrixController(ctl.ctx.spec)
        self.plan: RepairPlan | None = None
        self.cmd: OcsSet | None = None
        self.applied: list[bool] = []
        ctl.on_incident = self.on_incident
        ctl.on_event = self.on_event

    def on_incident(self, incident: Incident) -> None:
        plan = self.ocs.plan_repair(incident, self.ctl.tm.view())
        assert isinstance(plan, RepairPlan)
        self.plan = plan
        self.ctl.ctx.clock.schedule(self.ctl.ctx.config.timing().plan, self.send)

    def send(self) -> None:
        assert self.plan is not None and self.ctl.ctx.ocs is not None
        for dev, msg in self.plan.prepares.items():
            stamped = dataclasses.replace(msg, header=self.ctl.header(MsgType.PREPARE, dev))
            self.ctl.ctx.devices.send(dev, DeviceCommand(prepare=stamped))
        self.cmd = OcsSet(self.ctl.ocs_header(), self.plan.disconnect, self.plan.connect)
        self.ctl.ctx.ocs.apply(self.cmd)
        timeout = self.ctl.ctx.config.timeouts_us.ocs_done
        self.ctl.ctx.clock.schedule(timeout, self.read_back)

    def read_back(self) -> None:
        if not self.applied:  # no OCS_DONE came: read the matrix back
            assert self.cmd is not None and self.ctl.ctx.ocs is not None
            self.applied.append(self.ocs.on_applied(self.cmd, self.ctl.ctx.ocs.read_matrix()))

    def on_event(self, ev: DeviceEvent | OcsDone) -> None:
        if self.plan is None:
            return  # the LANE_DOWN reports come before the incident is planned
        view = self.ctl.tm.view()
        if isinstance(ev, OcsDone):
            assert self.cmd is not None and self.ctl.ctx.ocs is not None
            self.applied.append(self.ocs.on_applied(self.cmd, self.ctl.ctx.ocs.read_matrix()))
        elif ev.header.type is MsgType.LANE_UP and len(self.ctl.received(MsgType.LANE_UP)) == 2:
            for dev, nbr in (("npu-0", "npu-64"), ("npu-64", "npu-0")):
                self.ctl.send_groups(dev, {nbr: view.usable_lanes(dev, nbr)})
        elif ev.header.type is MsgType.ACK and all(
            view.lane_state(x) is LaneState.ACTIVE
            for prep in self.plan.prepares.values()
            for x in prep.expected_peer
        ):
            self.ctl.tm.record_repair(self.plan, self.ctl.ctx.clock.now())


@pytest.mark.parametrize(("key", "two_plus_two"), [("G4a", True), ("G4b", False)])
def test_planned_repair_restores_capacity_in_the_simulator(
    key: str, two_plus_two: bool, golden: dict[str, Any]
) -> None:
    g = golden[key]
    sim, made = captured(cls=TmController)
    sim.load_topology("topology2", two_plus_two, 1)
    sim.configure(ocs=True)
    sim.inject(0, "module_fail", modules=["npu-0.m0"])
    driver = PlannedRestore(made[-1])
    sim.run(2000)
    assert driver.applied == [True]  # the read-back after OCS_DONE matches the plan
    assert ms(max(t for t, _ in driver.ctl.received(MsgType.ACK))) == g["timeline_typical"][
        "groups_acked"
    ]
    assert sim.report().messages_total() == g["messages_total"]
    assert sim.capacity("npu-0/npu-64") == 1.0
    assert set(sim.ocs_matrix()) == driver.ocs.matrix.xconnects()
    view = driver.ctl.tm.view()
    assert view.failed_modules() == {"npu-0.m0"}
    assert g["spare"] not in view.spare_pool()


def test_read_back_shows_nothing_when_the_ocs_never_answers(golden: dict[str, Any]) -> None:
    sim, made = captured(cls=TmController)
    sim.load_topology("topology2", True, 1)
    sim.configure(ocs=True)
    sim.inject(0, "ocs_never_answers")
    sim.inject(0, "module_fail", modules=["npu-0.m0"])
    driver = PlannedRestore(made[-1])
    sim.run(1500)
    assert driver.applied == [False]
    assert golden["G7"]["timeout_1"] == 1002
    assert sim.capacity("npu-0/npu-64") == 0.5
    assert set(sim.ocs_matrix()) == driver.ocs.matrix.xconnects()
