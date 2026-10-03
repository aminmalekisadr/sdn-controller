"""SdnController (Phase 6): steady state on both topologies, KEEPALIVE, wiring, one sender."""

import re
from typing import Any

import pytest

from sdnctl.controller import SdnController
from sdnctl.interfaces import ControllerContext, IncidentOutcome
from sdnctl.ocs import NullOcsController, OcsMatrixController
from sdnctl.routing_engine import RoutingEngine
from sdnctl.sim import Simulator
from sdnctl.topology import steady_state_view
from sdnctl.types import IncidentState, LaneState
from tests.conftest import REPO_ROOT


def hosted(name: str, two_plus_two: bool, spares: int, ocs: bool) -> tuple[
    Simulator, SdnController
]:
    """A simulator hosting an SdnController."""
    made: list[SdnController] = []

    def factory(ctx: ControllerContext) -> SdnController:
        made.append(SdnController(ctx))
        return made[-1]

    sim = Simulator(controller=factory)
    sim.load_topology(name, two_plus_two, spares)
    sim.configure(ocs=ocs)
    return sim, made[-1]


STEADY = [
    ("topology1", True, 0, False),
    ("topology2", True, 1, True),
    ("topology2", False, 0, False),
]


@pytest.mark.parametrize(("name", "two_plus_two", "spares", "ocs"), STEADY)
def test_steady_state_tables_equal_compute(
    name: str, two_plus_two: bool, spares: int, ocs: bool, golden: dict[str, Any]
) -> None:
    """Exit check: every device table equals compute(); 0 looping and 0 blackholed pairs."""
    sim, ctl = hosted(name, two_plus_two, spares, ocs)
    engine = RoutingEngine()
    view = steady_state_view(sim.spec)
    expected = engine.compute(view)
    on_devices = {d.id: sim.device(d.id).tables for d in sim.spec.devices}
    for d, tables in on_devices.items():
        assert tables.version == 1, d
        assert tables.routes == expected[d].routes, d
        assert tables.groups == expected[d].groups, d
    report = engine.check_state(on_devices, view)
    assert (report.looping_pairs, report.blackholed_pairs) == (0, 0)
    key = "topology1" if name == "topology1" else f"topology2_spare{spares}"
    if "route_entries" in golden[key]:
        assert sum(len(t.routes) for t in on_devices.values()) == golden[key]["route_entries"]
    assert ctl.installed == expected  # the controller's copy matches what it installed


def test_keepalive_opens_the_session_and_is_not_counted() -> None:
    sim, _ = hosted("topology2", True, 1, True)
    sim.run(10)
    assert {sim.device(d.id).keepalive_epoch for d in sim.spec.devices} == {1}
    report = sim.report()
    assert report.messages == {}
    sends = [r for r in report.trace if r["kind"] == "send" and r["type"] == "KEEPALIVE"]
    assert len(sends) == len(sim.spec.devices)


@pytest.mark.parametrize(("ocs", "kind"), [(True, OcsMatrixController), (False, NullOcsController)])
def test_ocs_controller_follows_the_feature_flag(ocs: bool, kind: type) -> None:
    _, ctl = hosted("topology2", True, 1, ocs)
    assert isinstance(ctl.ocs, kind)
    assert ctl.ocs.enabled() is ocs


@pytest.mark.parametrize(("two_plus_two", "lost"), [(True, 0), (False, 4)])
def test_events_reach_the_modules_and_the_copy_follows_the_prune(
    two_plus_two: bool, lost: int
) -> None:
    """G1 (2+2) and G2 (2:1): the incident opens, and the controller's copy is pruned like the
    devices' tables."""
    sim, ctl = hosted("topology1", two_plus_two, 0, False)
    sim.inject(0, "module_fail", modules=["l1-1536.m0"])
    sim.run(2)  # planned, nothing sent yet
    [run] = ctl.scheduler.runs
    incident = run.incident
    assert incident.failed_device == "l1-1536" and len(incident.lost) == lost
    if not lost:  # G1: no reroute, so it parks as soon as planning ends
        assert ctl.outcomes() == (
            IncidentOutcome(1, IncidentState.DEGRADED_WAITING_REPAIR, 2000, "no_ocs"),
        )
    assert ctl.tm.view().lane_state("l1-1536.p12.0") is LaneState.DOWN
    for d in sim.spec.devices:
        assert ctl.installed[d.id].groups == sim.device(d.id).tables.groups, d.id
        assert ctl.installed[d.id].routes == sim.device(d.id).tables.routes, d.id


def test_only_the_scheduler_holds_the_adapters() -> None:
    """Safety rule 5: the Topology Manager, the Routing Engine and the OCS controller never
    call an adapter; they are never given one, and their code never names one."""
    pattern = re.compile(r"IDeviceAdapter|IOcsAdapter|ControllerContext|\.send\(|read_matrix\(")
    offenders = []
    for package in ("topology_manager", "routing_engine", "ocs", "topology"):
        for path in (REPO_ROOT / "src" / "sdnctl" / package).rglob("*.py"):
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path))
    assert offenders == []
