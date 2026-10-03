"""The OCS restore path in the simulator, with the controller's part of G4 played by hand.

The test sends what the G4 plan in golden.json says, at the times SPEC 11.0 says the controller
sends it; every latency in between comes from the simulator. The timelines must equal golden.json.
"""

from typing import Any

import pytest

from sdnctl.messages import DeviceCommand, DeviceEvent, GroupSet, OcsDone, OcsSet, Prepare
from sdnctl.sim import Simulator
from sdnctl.types import CrossConnect, MsgType
from tests.sim_helpers import CaptureController, captured, ms, times


def run_g4(
    key: str,
    profile: str,
    golden: dict[str, Any],
    faults: list[dict[str, Any]] | None = None,
    pause_ms: int = 50,
) -> tuple[Simulator, CaptureController, dict[str, float]]:
    """Run G4a or G4b with a hand-played controller; return the capacities seen at pause_ms."""
    g = golden[key]
    sim, made = captured()
    sim.load_topology("topology2", key == "G4a", 1)
    sim.configure(ocs=True, timing=profile)
    for fault in faults or []:
        sim.inject(**fault)
    sim.inject(0, "module_fail", modules=["npu-0.m0"])
    ctl = made[-1]
    ocs = ctl.ctx.ocs
    assert ocs is not None
    timing = ctl.ctx.config.timing()
    lane_at = {x.ocs_port: x.id for x in sim.spec.lanes if x.ocs_port is not None}
    connect = tuple(CrossConnect(n, s) for n, s in g["ocs_connect"])
    disconnect = tuple(CrossConnect(n, s) for n, s in g["ocs_disconnect"])

    def plan_done() -> None:
        npu0 = {lane_at[xc.north]: lane_at[xc.south] for xc in connect}
        npu64 = {lane_at[xc.south]: lane_at[xc.north] for xc in connect}
        for dev, expected in (("npu-0", npu0), ("npu-64", npu64)):
            prepare = Prepare(ctl.header(MsgType.PREPARE, dev), tuple(expected), expected)
            ctl.ctx.devices.send(dev, DeviceCommand(prepare=prepare))
        ocs.apply(OcsSet(ctl.ocs_header(), disconnect, connect))

    lane_ups: set[str] = set()

    def on_event(ev: DeviceEvent | OcsDone) -> None:
        if ev.header.type is MsgType.LANE_DOWN and len(ctl.received(MsgType.LANE_DOWN)) == 1:
            ctl.ctx.clock.schedule(timing.plan, plan_done)  # a MODULE_FAULT plans at once
        if ev.header.type is MsgType.LANE_UP:
            lane_ups.add(ev.header.src)
            if lane_ups == {"npu-0", "npu-64"}:
                for entry in g["group_set"]:
                    dev = entry["device"]
                    msg = GroupSet(
                        ctl.header(MsgType.GROUP_SET, dev),
                        {entry["neighbor"]: tuple(entry["lanes"])},
                    )
                    ctl.ctx.devices.send(dev, DeviceCommand(group_set=msg))

    ctl.on_event = on_event
    sim.run(pause_ms)
    during = {k: round(sim.capacity(k), 6) for k in g["capacity_during"]}
    sim.run(6000)
    return sim, ctl, during


def timeline(sim: Simulator, ctl: CaptureController) -> dict[str, int]:
    """The G4 timeline (ms) as seen in the trace and by the controller."""
    report = sim.report()
    acks = [t for t, _ in ctl.received(MsgType.ACK)]
    return {
        "lane_down_at_controller": ms(min(t for t, _ in ctl.received(MsgType.LANE_DOWN))),
        "plan_done": ms(min(times(report, "send", type="PREPARE"))),
        "prepare_acked": ms(acks[1]),
        "ocs_applied": ms(min(times(report, "ocs_apply"))),
        "ocs_done": ms(ctl.received(MsgType.OCS_DONE)[0][0]),
        "light": ms(min(times(report, "light", on=True))),
        "hello_ok": ms(max(times(report, "hello_ok"))),
        "errors_clean": ms(max(times(report, "errors_clean"))),
        "lane_up_at_controller": ms(max(t for t, _ in ctl.received(MsgType.LANE_UP))),
        "groups_acked": ms(max(times(report, "apply"))),
    }


@pytest.mark.parametrize("profile", ["typical", "worst"])
@pytest.mark.parametrize("key", ["G4a", "G4b"])
def test_g4_restore_matches_golden(key: str, profile: str, golden: dict[str, Any]) -> None:
    g = golden[key]
    sim, ctl, during = run_g4(key, profile, golden, pause_ms=50 if profile == "typical" else 2000)
    assert timeline(sim, ctl) == g[f"timeline_{profile}"]
    assert during == g["capacity_during"]
    for k, value in g["capacity_final"].items():
        assert sim.capacity(k) == value, k
    report = sim.report()
    assert report.messages == {
        "LANE_DOWN": 2,
        "PREPARE": 2,
        "ACK": 4,
        "OCS_SET": 1,
        "OCS_DONE": 1,
        "LANE_UP": 2,
        "GROUP_SET": 2,
    }
    assert report.messages_total() == g["messages_total"]
    for entry in g["group_set"]:
        groups = sim.device(entry["device"]).tables.groups
        assert list(groups[entry["neighbor"]].lanes) == entry["lanes"]
    gone = {CrossConnect(n, s) for n, s in g["ocs_disconnect"]}
    expected = [xc for xc in sim.spec.xconnects if xc not in gone]
    expected += [CrossConnect(n, s) for n, s in g["ocs_connect"]]
    assert sim.ocs_matrix() == sorted(expected, key=lambda xc: int(xc.north[1:]))
    done = ctl.received(MsgType.OCS_DONE)[0][1]
    assert isinstance(done, OcsDone) and done.ok


def test_dropped_ack_is_counted_but_not_delivered(golden: dict[str, Any]) -> None:
    drop = {"t_ms": 0, "event": "drop_ack", "device": "npu-64", "command": "GROUP_SET", "count": 1}
    sim, ctl, _ = run_g4("G4a", "typical", golden, faults=[drop])
    acks = ctl.received(MsgType.ACK)
    assert [ev.header.src for _, ev in acks] == ["npu-0", "npu-64", "npu-0"]
    report = sim.report()
    lost = [r for r in report.trace if r["kind"] == "send" and r.get("lost")]
    assert [(r["src"], ms(r["t"])) for r in lost] == [("npu-64", golden["G8"]["devices_applied"])]
    assert report.messages["ACK"] == 4  # a lost message still counts
    assert sim.capacity("npu-0/npu-64") == 1.0  # both devices applied their GROUP_SET


def test_ocs_never_answers(golden: dict[str, Any]) -> None:
    fault = {"t_ms": 0, "event": "ocs_never_answers"}
    sim, ctl, _ = run_g4("G4a", "typical", golden, faults=[fault])
    report = sim.report()
    assert times(report, "ocs_apply") == []
    assert ctl.received(MsgType.OCS_DONE) == []
    assert sim.ocs_matrix() == list(sim.spec.xconnects)
    assert sim.capacity("npu-0/npu-64") == 0.5
    assert report.messages == {"LANE_DOWN": 2, "PREPARE": 2, "ACK": 2, "OCS_SET": 1}
