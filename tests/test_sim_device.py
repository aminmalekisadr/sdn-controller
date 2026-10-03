"""SimDevice behavior (SPEC 7.5): versions, safety checks, atomic batches, forwarding, HELLO."""

from sdnctl.messages import (
    CONTROLLER,
    DeviceCommand,
    GroupSet,
    Header,
    Keepalive,
    LaneDown,
    OcsSet,
    Prepare,
    RouteSet,
    RouteSetEntry,
)
from sdnctl.sim import Simulator
from sdnctl.sim.device import SimDevice
from sdnctl.types import CrossConnect, DownCause, LaneState, MsgType
from tests.sim_helpers import captured


def hdr(t: MsgType, dst: str = "npu-0", version: int = 2, seq: int = 1) -> Header:
    """A controller header."""
    return Header(t, seq, version, None, CONTROLLER, dst, 0)


def npu0() -> tuple[Simulator, SimDevice]:
    """Topology 2 (2+2, one spare) in steady state, and its npu-0."""
    sim = Simulator()
    sim.load_topology("topology2", True, 1)
    sim.configure(ocs=True)
    return sim, sim.device("npu-0")


def test_steady_state_groups_from_topology() -> None:
    _, dev = npu0()
    assert dev.tables.version == 1
    assert dev.tables.groups["npu-64"].lanes == (
        "npu-0.p13.0",
        "npu-0.p13.1",
        "npu-0.p14.0",
        "npu-0.p14.1",
    )
    assert dev.tables.routes == {}
    assert dev.lane_state["npu-0.p15.0"] is LaneState.IDLE
    assert dev.lane_state["npu-0.p13.0"] is LaneState.ACTIVE


def test_older_version_is_ignored() -> None:
    _, dev = npu0()
    cmd = DeviceCommand(group_set=GroupSet(hdr(MsgType.GROUP_SET, version=0), {"npu-64": ()}))
    ack = dev.handle(cmd)
    assert ack is not None and not ack.ok and "stale" in ack.error
    assert "npu-64" in dev.tables.groups


def test_group_set_rejects_unverified_and_misdirected_lanes() -> None:
    _, dev = npu0()
    idle = DeviceCommand(
        group_set=GroupSet(hdr(MsgType.GROUP_SET), {"npu-64": ("npu-0.p15.0",)})
    )
    ack = dev.handle(idle)
    assert ack is not None and not ack.ok and "idle" in ack.error
    wrong = DeviceCommand(group_set=GroupSet(hdr(MsgType.GROUP_SET), {"npu-1": ("npu-0.p13.0",)}))
    ack = dev.handle(wrong)
    assert ack is not None and not ack.ok and "does not face npu-1" in ack.error
    assert dev.tables.version == 1


def test_batch_is_atomic_and_applies_groups_before_routes() -> None:
    _, dev = npu0()
    routes = RouteSet(hdr(MsgType.ROUTE_SET), {"npu-8": RouteSetEntry(("npu-64",), 3)})
    bad = GroupSet(hdr(MsgType.GROUP_SET), {"npu-64": ("npu-0.p15.0",)})
    ack = dev.handle(DeviceCommand(group_set=bad, route_set=routes))
    assert ack is not None and not ack.ok
    assert dev.tables.routes == {}

    good = GroupSet(hdr(MsgType.GROUP_SET), {"npu-64": ("npu-0.p13.1", "npu-0.p14.1")})
    ack = dev.handle(DeviceCommand(group_set=good, route_set=routes))
    assert ack is not None and ack.ok and ack.acked_seq == 1
    assert dev.tables.groups["npu-64"].lanes == ("npu-0.p13.1", "npu-0.p14.1")
    assert dev.tables.routes["npu-8"].neighbors == ("npu-64",)
    assert dev.tables.version == 2
    assert dev.lane_state["npu-0.p13.0"] is LaneState.VERIFYING  # out of the group, still lit


def test_keepalive_only_is_not_acknowledged() -> None:
    _, dev = npu0()
    keepalive = Keepalive(hdr(MsgType.KEEPALIVE), controller_id="ctl", epoch=7)
    assert dev.handle(DeviceCommand(keepalive=keepalive)) is None
    assert dev.keepalive_epoch == 7


def test_forwarding_uses_ecmp_over_neighbors_with_a_group() -> None:
    _, dev = npu0()
    routes = RouteSet(hdr(MsgType.ROUTE_SET), {"npu-9": RouteSetEntry(("npu-1", "npu-2"), 2)})
    dev.handle(DeviceCommand(route_set=routes))
    picks = {dev.forward("npu-9", f"flow-{i}") for i in range(32)}
    assert None not in picks
    assert {p[0] for p in picks if p is not None} == {"npu-1", "npu-2"}
    assert dev.forward("npu-9", "flow-1") == dev.forward("npu-9", "flow-1")
    drop1 = GroupSet(hdr(MsgType.GROUP_SET, version=3), {"npu-1": ()})
    dev.handle(DeviceCommand(group_set=drop1))
    assert {dev.forward("npu-9", f"flow-{i}") for i in range(32)} <= {
        ("npu-2", "npu-0.p1.0"),
        ("npu-2", "npu-0.p1.1"),
    }
    drop2 = GroupSet(hdr(MsgType.GROUP_SET, version=4), {"npu-2": ()})
    dev.handle(DeviceCommand(group_set=drop2))
    assert dev.forward("npu-9", "flow-1") is None
    assert dev.forward("npu-77", "flow-1") is None  # no route


def test_wrong_hello_peer_times_out() -> None:
    """Spare lanes come up facing a peer nobody told them to expect: 3 missed HELLOs, then DOWN."""
    sim, made = captured()
    sim.load_topology("topology2", True, 1)
    sim.configure(ocs=True)
    sim.inject(0, "module_fail", modules=["npu-0.m0"])
    sim.run(2)
    ctl = made[-1]
    assert ctl.ctx.ocs is not None
    # wrong expectation on npu-0, and no PREPARE at all for npu-64
    prepare = Prepare(
        ctl.header(MsgType.PREPARE, "npu-0"),
        ("npu-0.p15.0",),
        {"npu-0.p15.0": "npu-64.p14.0"},
    )
    ctl.ctx.devices.send("npu-0", DeviceCommand(prepare=prepare))
    xc = CrossConnect("N257", "S1")
    ctl.ctx.ocs.apply(OcsSet(ctl.ocs_header(), (CrossConnect("N1", "S1"),), (xc,)))
    sim.run(200)
    downs = [
        (t, ev) for t, ev in ctl.received(MsgType.LANE_DOWN) if isinstance(ev, LaneDown)
        and ev.cause is DownCause.HELLO_TIMEOUT
    ]
    timing = ctl.ctx.config.timing()
    light = 2000 + timing.ocs_command + timing.mirror_move + timing.lane_bringup
    sent = light + 3 * timing.hello_check
    assert [(t, ev.device, ev.lanes) for t, ev in downs] == [
        (sent + timing.report, "npu-0", ("npu-0.p15.0",)),
        (sent + timing.report, "npu-64", ("npu-64.p13.0",)),
    ]
    assert ctl.received(MsgType.LANE_UP) == []
