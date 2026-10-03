"""The Topology Manager's lane state machine (SPEC 5.2): one test per transition."""

import pytest

from sdnctl.interfaces import RepairPlan
from sdnctl.messages import CONTROLLER, OCS, GroupSet, Header, LaneDown, LaneUp, OcsDone
from sdnctl.topology_manager import LaneStateError, TopologyManager
from sdnctl.topology_manager.lanes import check_transition
from sdnctl.types import CrossConnect, DownCause, LaneState, MsgType
from tests.conftest import built

S = LaneState


def tm() -> TopologyManager:
    """A Topology Manager on topology 2 (2+2, one spare)."""
    manager = TopologyManager(correlation_window=5000)
    manager.load(built("topology2", True, 1))
    return manager


def down(device: str, *lanes: str, cause: DownCause = DownCause.LOSS_OF_LIGHT) -> LaneDown:
    """A LANE_DOWN from device."""
    modules = ("npu-0.m0",) if cause is DownCause.MODULE_FAULT else ()
    return LaneDown(Header(MsgType.LANE_DOWN, 1, 1, None, device, CONTROLLER, 0), device,
                    lanes, cause, modules)


def up(device: str, pairs: dict[str, str]) -> LaneUp:
    """A LANE_UP from device: lane -> peer lane seen in HELLO."""
    return LaneUp(Header(MsgType.LANE_UP, 2, 1, None, device, CONTROLLER, 0), device,
                  tuple(pairs), tuple(pairs.values()))


def groups(device: str, neighbor: str, *lanes: str) -> GroupSet:
    """A GROUP_SET for one neighbor."""
    return GroupSet(Header(MsgType.GROUP_SET, 3, 2, 1, CONTROLLER, device, 0), {neighbor: lanes})


def rewire(manager: TopologyManager, gone: CrossConnect, new: CrossConnect) -> None:
    """Deliver OCS_DONE with the matrix read back after replacing one cross-connect."""
    matrix = [xc for xc in manager.view().xconnects() if xc != gone] + [new]
    done = OcsDone(Header(MsgType.OCS_DONE, 1, 1, 1, OCS, CONTROLLER, 32_000), (), (), 2)
    manager.on_ocs_done(done, matrix)


def failed_and_rewired() -> TopologyManager:
    """npu-0.m0 failed (2+2) and N257-S1 replaced N1-S1."""
    manager = tm()
    manager.on_lane_down(down("npu-0", "npu-0.p13.0", "npu-0.p14.0",
                              cause=DownCause.MODULE_FAULT), 1000)
    manager.on_lane_down(down("npu-64", "npu-64.p13.0", "npu-64.p14.0"), 1000)
    rewire(manager, CrossConnect("N1", "S1"), CrossConnect("N257", "S1"))
    return manager


def state(manager: TopologyManager, lane: str) -> LaneState:
    """A lane's state in the current view."""
    return manager.view().lane_state(lane)


def test_load_is_steady_state() -> None:
    manager = tm()
    view = manager.view()
    assert manager.version() == 1
    assert view.lane_state("npu-0.p13.0") is S.ACTIVE
    assert view.lane_state("npu-0.p15.0") is S.IDLE
    assert len(view.spare_pool()) == 128 and view.failed_modules() == frozenset()
    assert view.pair_capacity("npu-0", "npu-64") == 1.0


def test_active_to_down() -> None:
    manager = tm()
    manager.on_lane_down(down("npu-64", "npu-64.p13.0"), 1000)
    assert state(manager, "npu-64.p13.0") is S.DOWN
    assert manager.version() == 2


def test_down_and_idle_to_connecting_on_ocs_done() -> None:
    manager = failed_and_rewired()
    assert state(manager, "npu-64.p13.0") is S.CONNECTING
    assert state(manager, "npu-0.p15.0") is S.CONNECTING
    assert state(manager, "npu-0.p13.0") is S.DOWN  # its cross-connect is gone; it stays down
    assert manager.view().peer_of("npu-0.p15.0") == "npu-64.p13.0"
    assert manager.view().peer_of("npu-0.p13.0") is None


def test_connecting_to_verifying_and_usable_after_both_ends() -> None:
    manager = failed_and_rewired()
    manager.on_lane_up(up("npu-0", {"npu-0.p15.0": "npu-64.p13.0"}), 95_000)
    assert state(manager, "npu-0.p15.0") is S.VERIFYING
    assert "npu-0.p15.0" not in manager.view().usable_lanes("npu-0", "npu-64")
    manager.on_lane_up(up("npu-64", {"npu-64.p13.0": "npu-0.p15.0"}), 95_000)
    assert "npu-0.p15.0" in manager.view().usable_lanes("npu-0", "npu-64")
    assert "npu-64.p13.0" in manager.view().usable_lanes("npu-64", "npu-0")


def test_down_to_verifying_after_repair_without_ocs() -> None:
    manager = tm()
    manager.on_lane_down(down("npu-0", "npu-0.p0.0"), 1000)
    manager.on_lane_up(up("npu-0", {"npu-0.p0.0": "npu-1.p0.0"}), 10_063_000)
    assert state(manager, "npu-0.p0.0") is S.VERIFYING


def test_verifying_to_active_when_group_installed() -> None:
    manager = failed_and_rewired()
    manager.on_lane_up(up("npu-0", {"npu-0.p15.0": "npu-64.p13.0"}), 95_000)
    manager.on_lane_up(up("npu-64", {"npu-64.p13.0": "npu-0.p15.0"}), 95_000)
    lanes = ("npu-0.p13.1", "npu-0.p14.1", "npu-0.p15.0")
    manager.on_groups_installed("npu-0", groups("npu-0", "npu-64", *lanes), 98_000)
    assert state(manager, "npu-0.p15.0") is S.ACTIVE
    assert manager.view().active_lanes("npu-0", "npu-64") == lanes


def test_installing_an_unusable_lane_is_an_error() -> None:
    manager = failed_and_rewired()
    manager.on_lane_up(up("npu-0", {"npu-0.p15.0": "npu-64.p13.0"}), 95_000)  # one end only
    with pytest.raises(LaneStateError, match="not usable"):
        manager.on_groups_installed("npu-0", groups("npu-0", "npu-64", "npu-0.p15.0"), 98_000)


def test_active_to_verifying_when_left_out_of_a_group() -> None:
    manager = tm()
    manager.on_groups_installed("npu-0", groups("npu-0", "npu-1", "npu-0.p0.1"), 1000)
    assert state(manager, "npu-0.p0.0") is S.VERIFYING
    assert state(manager, "npu-0.p0.1") is S.ACTIVE


def test_verifying_to_down_on_hello_timeout() -> None:
    manager = failed_and_rewired()
    manager.on_lane_up(up("npu-0", {"npu-0.p15.0": "npu-64.p13.0"}), 95_000)
    manager.on_lane_down(down("npu-0", "npu-0.p15.0", cause=DownCause.HELLO_TIMEOUT), 96_000)
    assert state(manager, "npu-0.p15.0") is S.DOWN


def test_connecting_to_down() -> None:
    manager = failed_and_rewired()
    manager.on_lane_down(down("npu-64", "npu-64.p13.0"), 50_000)
    assert state(manager, "npu-64.p13.0") is S.DOWN


def test_down_to_failed_on_record_repair_and_failed_is_final() -> None:
    manager = failed_and_rewired()
    plan = RepairPlan("npu-0", ("npu-0.m0",), "npu-0.m2", (), (), frozenset(), {})
    manager.record_repair(plan, 100_000)
    view = manager.view()
    assert view.lane_state("npu-0.p13.0") is S.FAILED
    assert view.lane_state("npu-0.p14.0") is S.FAILED
    assert view.failed_modules() == {"npu-0.m0"}
    assert "npu-0.m2" not in view.spare_pool() and len(view.spare_pool()) == 127
    manager.on_lane_down(down("npu-0", "npu-0.p13.0"), 200_000)
    assert state(manager, "npu-0.p13.0") is S.FAILED


def test_lane_up_is_ignored_for_idle_lanes_and_wrong_peers() -> None:
    manager = tm()
    manager.on_lane_up(up("npu-0", {"npu-0.p15.0": "npu-64.p13.0"}), 1000)
    assert state(manager, "npu-0.p15.0") is S.IDLE
    manager.on_lane_down(down("npu-0", "npu-0.p0.0"), 1000)
    manager.on_lane_up(up("npu-0", {"npu-0.p0.0": "npu-2.p0.0"}), 2000)
    assert state(manager, "npu-0.p0.0") is S.DOWN


def test_reports_must_name_the_senders_lanes() -> None:
    with pytest.raises(ValueError, match="not on it"):
        tm().on_lane_down(down("npu-0", "npu-1.p0.0"), 1000)


@pytest.mark.parametrize(
    ("old", "new"),
    [(S.IDLE, S.ACTIVE), (S.DOWN, S.ACTIVE), (S.CONNECTING, S.ACTIVE), (S.FAILED, S.DOWN),
     (S.ACTIVE, S.FAILED), (S.IDLE, S.VERIFYING)],
)
def test_illegal_transitions_raise(old: LaneState, new: LaneState) -> None:
    with pytest.raises(LaneStateError):
        check_transition("npu-0.p0.0", old, new)
