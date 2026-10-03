"""Phase 2 exit check (G1 local prune) and failure/repair timing without a controller."""

from typing import Any

import pytest

from sdnctl.messages import LaneDown
from sdnctl.tables import GroupEntry, RouteEntry
from sdnctl.types import DownCause, MsgType
from tests.sim_helpers import captured, ms, times

L2 = ("l2-2304", "l2-2305", "l2-2306", "l2-2307")
ROUTES = {
    "l1-1536": [RouteEntry("npu-64", L2, 4), RouteEntry("npu-1", ("npu-1",), 1)],
    "l2-2304": [RouteEntry("npu-0", ("l1-1536",), 2)],
}


def group_diff(
    before: dict[str, dict[str, GroupEntry]], after: dict[str, dict[str, GroupEntry]]
) -> list[dict[str, Any]]:
    """Group entries that changed, in golden.json's prune format."""
    out = []
    for dev in before:
        for nbr in sorted(set(before[dev]) | set(after[dev])):
            old, new = before[dev].get(nbr), after[dev].get(nbr)
            if old != new:
                out.append(
                    {
                        "device": dev,
                        "neighbor": nbr,
                        "before": list(old.lanes) if old else [],
                        "after": list(new.lanes) if new else [],
                    }
                )
    return out


def test_g1_local_prune(golden: dict[str, Any]) -> None:
    """Exit check: 8 group entries on 5 devices are pruned; route tables are untouched."""
    sim, made = captured(ROUTES)
    sim.load_topology("topology1", True)
    sim.configure(ocs=False)
    devices = [d.id for d in sim.spec.devices]
    groups0 = {d: dict(sim.device(d).tables.groups) for d in devices}
    routes0 = {d: dict(sim.device(d).tables.routes) for d in devices}
    sim.inject(0, "module_fail", modules=["l1-1536.m0"])
    sim.run(5)

    groups1 = {d: dict(sim.device(d).tables.groups) for d in devices}
    diff = group_diff(groups0, groups1)
    g1 = golden["G1"]
    assert diff == g1["prunes"]
    assert len(diff) == g1["group_entries_pruned"] == 8
    assert len({e["device"] for e in diff}) == g1["group_devices"] == 5
    assert {d: dict(sim.device(d).tables.routes) for d in devices} == routes0
    assert routes0["l1-1536"]["npu-64"].neighbors == L2  # the routes were really installed
    assert all(sim.device(d).tables.version == 1 for d in devices)

    report = sim.report()
    assert times(report, "prune") == [1000] * 8
    reports = [ev for _, ev in made[-1].received(MsgType.LANE_DOWN)]
    assert all(isinstance(ev, LaneDown) for ev in reports)
    assert sorted(ev.device for ev in reports) == g1["reporters"]
    causes = {ev.device: ev.cause for ev in reports if isinstance(ev, LaneDown)}
    assert causes.pop("l1-1536") is DownCause.MODULE_FAULT
    assert set(causes.values()) == {DownCause.LOSS_OF_LIGHT}
    assert {t for t, _ in made[-1].received(MsgType.LANE_DOWN)} == {1000}
    assert report.messages == {"LANE_DOWN": 5}
    for key, value in g1["capacity"].items():
        assert round(sim.capacity(key), 6) == value, key


def test_g1_repair_timeline(golden: dict[str, Any]) -> None:
    sim, made = captured()
    sim.load_topology("topology1", True)
    sim.configure(ocs=False)
    sim.inject(0, "module_fail", modules=["l1-1536.m0"])
    sim.inject(10_000, "repair", modules=["l1-1536.m0"])
    sim.run(10_100)
    report = sim.report()
    repair = golden["G1"]["repair"]
    assert {ms(t) for t in times(report, "light", on=True)} == {repair["light"]}
    assert {ms(t) for t in times(report, "hello_ok")} == {repair["hello_ok"]}
    assert {ms(t) for t in times(report, "errors_clean")} == {repair["errors_clean"]}
    ups = made[-1].received(MsgType.LANE_UP)
    assert len(ups) == 5
    assert {ms(t) for t, _ in ups} == {repair["lane_up_at_controller"]}
    # nobody sends GROUP_SET in Phase 2, so the verified lanes stay out of the groups
    assert sim.capacity("l1-1536.d0") == 0.5


@pytest.mark.parametrize("profile", ["typical", "worst"])
def test_g2_reports_and_repair_timing(profile: str, golden: dict[str, Any]) -> None:
    g2 = golden["G2"]
    sim, made = captured()
    sim.load_topology("topology1", False)
    sim.configure(ocs=False, timing=profile)
    sim.inject(0, "module_fail", modules=["l1-1536.m0"])
    sim.inject(10_000, "repair", modules=["l1-1536.m0"])
    sim.run(20_000)
    ctl = made[-1]
    downs = ctl.received(MsgType.LANE_DOWN)
    assert sorted(ev.device for _, ev in downs) == g2["reporters"]
    down_ms = g2[f"timeline_{profile}"]["down"]["lane_down_at_controller"]
    assert {ms(t) for t, _ in downs} == {down_ms}
    for entry in g2["emptied_groups"]:
        assert entry["neighbor"] not in sim.device(entry["device"]).tables.groups

    report = sim.report()
    repair = g2[f"timeline_{profile}"]["repair"]
    assert {ms(t) for t in times(report, "light", on=True)} == {repair["light"]}
    assert {ms(t) for t in times(report, "hello_ok")} == {repair["hello_ok"]}
    assert {ms(t) for t in times(report, "errors_clean")} == {repair["errors_clean"]}
    ups = ctl.received(MsgType.LANE_UP)
    assert sorted(ev.device for _, ev in ups) == g2["reporters"]
    assert {ms(t) for t, _ in ups} == {repair["lane_up_at_controller"]}
