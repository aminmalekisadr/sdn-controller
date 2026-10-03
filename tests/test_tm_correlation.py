"""Incident correlation (SPEC 11.0 rule 2) in the Topology Manager."""

from sdnctl.messages import CONTROLLER, Header, LaneDown
from sdnctl.topology_manager import TopologyManager
from sdnctl.types import DownCause, IncidentState, MsgType
from tests.conftest import built

FAULT = DownCause.MODULE_FAULT
LOS = DownCause.LOSS_OF_LIGHT


def tm() -> TopologyManager:
    """A Topology Manager on topology 2 (2+2, one spare) with a 5 ms window."""
    manager = TopologyManager(correlation_window=5000)
    manager.load(built("topology2", True, 1))
    return manager


def report(device: str, lanes: tuple[str, ...], cause: DownCause,
           modules: tuple[str, ...] = ()) -> LaneDown:
    """A LANE_DOWN."""
    header = Header(MsgType.LANE_DOWN, 1, 1, None, device, CONTROLLER, 0)
    return LaneDown(header, device, lanes, cause, modules)


NPU0_M0 = report("npu-0", ("npu-0.p13.0", "npu-0.p14.0"), FAULT, ("npu-0.m0",))
NPU64_LOS = report("npu-64", ("npu-64.p13.0", "npu-64.p14.0"), LOS)


def test_module_fault_plans_at_once_and_same_time_reports_join() -> None:
    manager = tm()
    assert manager.correlation_deadline() is None
    manager.on_lane_down(NPU0_M0, 1000)
    manager.on_lane_down(NPU64_LOS, 1000)
    assert manager.correlation_deadline() == 1000
    [incident] = manager.poll_incidents(1000)
    assert incident.id == 1
    assert incident.opened_at == 1000
    assert [r.device for r in incident.reports] == ["npu-0", "npu-64"]
    assert incident.failed_device == "npu-0"
    assert incident.failed_modules == ("npu-0.m0",)
    assert incident.lanes == {"npu-0.p13.0", "npu-0.p14.0", "npu-64.p13.0", "npu-64.p14.0"}
    assert incident.lost == frozenset()  # 2+2: each side keeps a lane
    assert incident.state is IncidentState.OPEN
    assert manager.correlation_deadline() is None
    assert manager.poll_incidents(1000) == []


def test_loss_of_light_waits_for_the_window() -> None:
    manager = tm()
    manager.on_lane_down(NPU64_LOS, 1000)
    assert manager.correlation_deadline() == 6000
    assert manager.poll_incidents(5999) == []
    manager.on_lane_down(report("npu-0", ("npu-0.p13.0",), LOS), 3000)
    [incident] = manager.poll_incidents(6000)
    assert len(incident.reports) == 2
    assert incident.failed_device is None  # no MODULE_FAULT: the failed end is unclear
    assert incident.failed_modules == ()


def test_a_late_module_fault_closes_the_window() -> None:
    manager = tm()
    manager.on_lane_down(NPU64_LOS, 1000)
    manager.on_lane_down(NPU0_M0, 3000)
    assert manager.correlation_deadline() == 3000
    [incident] = manager.poll_incidents(3000)
    assert incident.opened_at == 1000 and incident.failed_device == "npu-0"


def test_reports_after_hand_off_open_a_new_incident() -> None:
    manager = tm()
    manager.on_lane_down(NPU0_M0, 1000)
    [first] = manager.poll_incidents(1000)
    manager.on_lane_down(report("npu-1", ("npu-1.p0.0",), LOS), 2000)
    [second] = manager.poll_incidents(7000)
    assert (first.id, second.id) == (1, 2)


def test_lost_adjacencies_when_a_group_empties() -> None:
    manager = tm()
    all_lanes = ("npu-0.p13.0", "npu-0.p13.1", "npu-0.p14.0", "npu-0.p14.1")
    peer_lanes = tuple(x.replace("npu-0.", "npu-64.") for x in all_lanes)
    manager.on_lane_down(report("npu-0", all_lanes, FAULT, ("npu-0.m0", "npu-0.m1")), 1000)
    manager.on_lane_down(report("npu-64", peer_lanes, LOS), 1000)
    [incident] = manager.poll_incidents(1000)
    assert incident.lost == {("npu-0", "npu-64"), ("npu-64", "npu-0")}
    assert incident.failed_modules == ("npu-0.m0", "npu-0.m1")
