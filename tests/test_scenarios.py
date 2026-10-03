"""Phase 7 exit check: the golden scenarios G1-G8 end to end (SPEC 11), and the safety rules."""

from typing import Any

import pytest

from sdnctl.controller import SdnController
from sdnctl.interfaces import SimReport
from sdnctl.routing_engine import RoutingEngine
from sdnctl.scenario import load_scenario, run_scenario
from sdnctl.sim import Simulator
from sdnctl.topology import steady_state_view
from sdnctl.types import CrossConnect, IncidentState, LaneState
from tests.scenario_helpers import (
    SCENARIOS,
    WAVE_STOPS,
    capacity_at,
    scenario_run,
    sent,
    state_at,
    stepwise,
    times,
)


def controller(sim: Simulator) -> SdnController:
    """The SdnController a scenario ran."""
    ctl = sim.controller
    assert isinstance(ctl, SdnController)
    return ctl


def final_tables_equal_initial(sim: Simulator) -> bool:
    """Every device's tables equal the startup tables again."""
    initial = RoutingEngine().compute(steady_state_view(sim.spec))
    return all(
        sim.device(d).tables.routes == t.routes and sim.device(d).tables.groups == t.groups
        for d, t in initial.items()
    )


def repair_timeline(report: SimReport) -> dict[str, int]:
    """Light, HELLO, errors-clean and LANE_UP times of a repair (ms)."""
    return {
        "light": min(times(report, "light", on=True)),
        "hello_ok": max(times(report, "hello_ok")),
        "errors_clean": max(times(report, "errors_clean")),
        "lane_up_at_controller": max(times(report, "deliver", type="LANE_UP")),
    }


def reroute_timeline(report: SimReport) -> dict[str, int]:
    """Down and repair timelines of a reroute scenario (G2, G6)."""
    computes, wave1, wave2 = (times(report, "compute_done"), times(report, "wave_acked", wave=1),
                              times(report, "wave_acked", wave=2))
    return {
        "lane_down_at_controller": min(times(report, "deliver", type="LANE_DOWN")),
        "plan_done": times(report, "plan_done")[0],
        "compute_done": computes[0],
        "wave1_acked": wave1[0],
        "wave2_acked": wave2[0],
        "repair_compute_done": computes[1],
        "repair_wave1_acked": wave1[1],
        "repair_wave2_acked": wave2[1],
    }


# ------------------------------------------------------------------ G1


def test_g1(golden: dict[str, Any]) -> None:
    g = golden["G1"]
    sim, report = scenario_run("g1")
    assert dict(report.messages) == g["messages"]
    assert report.messages_total() == g["messages_total"]
    prunes = [{k: r[k] for k in ("device", "neighbor", "before", "after")}
              for r in report.trace if r["kind"] == "prune"]
    assert prunes == g["prunes"]
    for key, value in g["capacity"].items():
        assert round(capacity_at(report, key, 5), 6) == value, key
    assert state_at(report, "degraded_waiting_repair") == [g["degraded_waiting_repair_at"]]
    repair = repair_timeline(report)
    repair["groups_acked"] = times(report, "wave_acked")[0]
    assert repair == g["repair"]
    [outcome] = report.incidents
    assert (outcome.state, outcome.ended_at) == (IncidentState.CLOSED, 10_066_000)
    assert all(sim.capacity(k) == 1.0 for k in g["capacity"])
    assert sent(report, before_ms=10_000) == {"LANE_DOWN": 5}  # 0 route changes, 0 writes


# ------------------------------------------------------------------ G2 and G6 (reroute)


@pytest.mark.parametrize(("name", "key"), [("g2", "G2"), ("g6", "G6")])
def test_reroute_pair_counts_at_every_wave(name: str, key: str, golden: dict[str, Any]) -> None:
    """Safety rule 7, measured on the simulated devices at every wave boundary."""
    g = golden[key]
    _, _, counts = stepwise(name, WAVE_STOPS)
    down, up = g["down_states"], g["restore_states"]
    expected = [down["before_controller"], down["after_wave1"], down["after_wave2"],
                up["before_restore"], up["after_wave1"], up["after_wave2"]]
    assert [counts[s] for s in WAVE_STOPS] == [
        (e["looping_pairs"], e["blackholed_pairs"]) for e in expected
    ]


@pytest.mark.parametrize(("name", "key"), [("g2", "G2"), ("g6", "G6")])
def test_reroute_messages_waves_and_end(name: str, key: str, golden: dict[str, Any]) -> None:
    g = golden[key]
    sim, report, _ = stepwise(name, WAVE_STOPS)
    m = g["messages"]
    assert sent(report, before_ms=10_000) == {
        "LANE_DOWN": m["LANE_DOWN"], "ROUTE_SET": m["ROUTE_SET_down"], "ACK": m["ACK_down"]
    }
    up = sent(report, after_ms=10_000)
    assert up["LANE_UP"] == m["LANE_UP"] and up["ACK"] == m["ACK_restore"]
    assert up["GROUP_SET+ROUTE_SET"] == len(g["endpoints"])  # endpoints: group + route batched
    assert up["GROUP_SET+ROUTE_SET"] + up["ROUTE_SET"] == m["SET_restore"]
    assert report.messages_total() == g["messages_total"]
    waves = [r for r in report.trace if r["kind"] == "waves"]
    assert [w["sizes"] for w in waves] == [g["waves_down"], g["waves_restore"]]
    assert waves[0]["route_entries"] == g["route_entries_changed"]
    assert state_at(report, "degraded_waiting_repair") == [g["degraded_waiting_repair_at"]]
    [outcome] = report.incidents
    assert outcome.state is IncidentState.CLOSED
    assert final_tables_equal_initial(sim)


@pytest.mark.parametrize("profile", ["typical", "worst"])
def test_g2_timelines(profile: str, golden: dict[str, Any]) -> None:
    g = golden["G2"][f"timeline_{profile}"]
    _, report = scenario_run("g2", profile)
    got = reroute_timeline(report)
    assert {k: got[k] for k in g["down"]} == g["down"]
    repair = repair_timeline(report)
    repair.update(compute_done=got["repair_compute_done"], wave1_acked=got["repair_wave1_acked"],
                  wave2_acked=got["repair_wave2_acked"])
    assert repair == g["repair"]


@pytest.mark.parametrize("profile", ["typical", "worst"])
def test_g6_timelines(profile: str, golden: dict[str, Any]) -> None:
    g6 = golden["G6"]
    _, report = scenario_run("g6", profile)
    got = reroute_timeline(report)
    down = {k.replace("reroute_", ""): v for k, v in g6[f"reroute_timeline_{profile}"].items()}
    assert {k: got[k] for k in down} == down
    if profile == "typical":
        repair = repair_timeline(report)
        repair.update(compute_done=got["repair_compute_done"],
                      wave1_acked=got["repair_wave1_acked"], wave2_acked=got["repair_wave2_acked"])
        assert repair == g6["repair_timeline_typical"]


def test_g6_is_the_same_with_2_to_1(golden: dict[str, Any]) -> None:
    data = load_scenario(SCENARIOS / "g6.json")
    two_to_one = load_scenario({
        "name": "G6-2:1", "topology": data.topology, "features": {"two_plus_two": False,
        "ocs": False}, "events": [{"t_ms": e.t_ms, "event": e.event, **e.args}
                                  for e in data.events],
    })
    report = run_scenario(two_to_one).report()
    assert golden["G6"]["same_as_2to1"] is True
    assert report.messages_total() == golden["G6"]["messages_total"]
    assert [r["sizes"] for r in report.trace if r["kind"] == "waves"] == [
        golden["G6"]["waves_down"], golden["G6"]["waves_restore"]
    ]


def test_g6b_not_enough_spare(golden: dict[str, Any]) -> None:
    g = golden["G6"]["variant_b_ocs_on_one_spare"]
    _, report = scenario_run("g6b")
    m = g["messages"]
    assert dict(report.messages) == {
        "LANE_DOWN": m["LANE_DOWN"], "ROUTE_SET": m["ROUTE_SET_down"], "ACK": m["ACK_down"]
    }
    assert report.messages_total() == g["messages_total"]
    assert state_at(report, "degraded_no_spare") == [g["decided_at"]]
    [outcome] = report.incidents
    assert (outcome.state, outcome.reason) == (IncidentState.DEGRADED_NO_SPARE, "not_enough_spare")


# ------------------------------------------------------------------ G4 (OCS restore)


def ocs_timeline(report: SimReport) -> dict[str, int]:
    """The G4 timeline (ms)."""
    acks = sorted(times(report, "deliver", type="ACK"))
    return {
        "lane_down_at_controller": min(times(report, "deliver", type="LANE_DOWN")),
        "plan_done": times(report, "plan_done")[0],
        "prepare_acked": max(acks[:2]),
        "ocs_applied": times(report, "ocs_apply")[0],
        "ocs_done": times(report, "deliver", type="OCS_DONE")[0],
        "light": min(times(report, "light", on=True)),
        "hello_ok": max(times(report, "hello_ok")),
        "errors_clean": max(times(report, "errors_clean")),
        "lane_up_at_controller": max(times(report, "deliver", type="LANE_UP")),
        "groups_acked": times(report, "wave_acked")[0],
    }


@pytest.mark.parametrize("profile", ["typical", "worst"])
@pytest.mark.parametrize(("name", "key"), [("g4a", "G4a"), ("g4b", "G4b")])
def test_g4_ocs_restore(name: str, key: str, profile: str, golden: dict[str, Any]) -> None:
    g = golden[key]
    sim, report = scenario_run(name, profile)
    timeline = ocs_timeline(report)
    assert timeline == g[f"timeline_{profile}"]
    m = g["messages"]
    assert dict(report.messages) == {
        "LANE_DOWN": m["LANE_DOWN"], "PREPARE": m["PREPARE"], "OCS_SET": m["OCS_SET"],
        "OCS_DONE": m["OCS_DONE"], "LANE_UP": m["LANE_UP"], "GROUP_SET": m["GROUP_SET"],
        "ACK": m["ACK_prepare"] + m["ACK_group"],
    }
    assert report.messages_total() == g["messages_total"]
    for k, value in g["capacity_during"].items():
        assert round(capacity_at(report, k, timeline["ocs_done"]), 6) == value, k
    for k, value in g["capacity_final"].items():
        assert sim.capacity(k) == value, k
    [outcome] = report.incidents
    assert outcome.state.value.upper() == g["end_state"]
    assert outcome.ended_at == timeline["groups_acked"] * 1000
    for entry in g["group_set"]:
        lanes = sim.device(entry["device"]).tables.groups[entry["neighbor"]].lanes
        assert list(lanes) == entry["lanes"]
    view = controller(sim).tm.view()
    assert view.failed_modules() == {"npu-0.m0"}
    assert view.lane_state("npu-0.p13.0") is LaneState.FAILED
    assert g["spare"] not in view.spare_pool()
    gone = {CrossConnect(n, s) for n, s in g["ocs_disconnect"]}
    expected = {xc for xc in sim.spec.xconnects if xc not in gone}
    expected |= {CrossConnect(n, s) for n, s in g["ocs_connect"]}
    assert set(sim.ocs_matrix()) == expected


def test_g5_no_spare(golden: dict[str, Any]) -> None:
    g = golden["G5"]
    sim, report = scenario_run("g5")
    assert state_at(report, "degraded_no_spare") == [g["decided_at"]]
    assert report.messages_total() == g["messages_total"]
    assert sent(report) == {"LANE_DOWN": 2}  # no OCS_SET, no PREPARE
    assert sim.capacity("npu-0/npu-64") == g["pair_capacity"]
    [outcome] = report.incidents
    assert outcome.reason == "no_spare"


# ------------------------------------------------------------------ G7 and G8 (faults)


def test_g7_ocs_never_answers(golden: dict[str, Any]) -> None:
    g = golden["G7"]
    sim, report = scenario_run("g7")
    assert times(report, "send", type="OCS_SET") == [g["ocs_set_1"], g["ocs_set_2"]]
    assert times(report, "ocs_timeout") == [g["timeout_1"], g["timeout_2"]]
    assert state_at(report, "failed_needs_operator") == [g["ended_at"]]
    [outcome] = report.incidents
    assert outcome.state.value.upper() == g["result"] and "OCS" in outcome.reason
    m = g["messages"]
    assert dict(report.messages) == {
        "LANE_DOWN": m["LANE_DOWN"], "PREPARE": m["PREPARE"], "ACK": m["ACK_prepare"],
        "OCS_SET": m["OCS_SET"],
    }
    assert report.messages_total() == g["messages_total"]
    pair = [s.value for s in report.capacity if s.key == "npu-0/npu-64"]
    assert min(pair) == g["min_pair_capacity"] == sim.capacity("npu-0/npu-64")


def test_g8_one_ack_lost(golden: dict[str, Any]) -> None:
    g = golden["G8"]
    sim, report = scenario_run("g8")
    assert times(report, "send", type="GROUP_SET") == [g["group_set_sent"]] * 2
    npu0_acks = [t for t in times(report, "deliver", type="ACK", src="npu-0")
                 if t >= g["group_set_sent"]]
    assert npu0_acks == [g["npu0_ack"]]
    assert capacity_at(report, "npu-0/npu-64", g["devices_applied"] - 1) == 0.5
    assert capacity_at(report, "npu-0/npu-64", g["devices_applied"]) == 1.0
    assert times(report, "ack_timeout") == [g["timeout"]]
    assert len(times(report, "resend")) == g["resends"] == 1
    assert max(times(report, "deliver", type="ACK", src="npu-64")) == g["resend_acked"]
    assert report.messages_total() == g["messages_total"]
    [outcome] = report.incidents
    assert (outcome.state, outcome.ended_at) == (IncidentState.CLOSED, g["resend_acked"] * 1000)
    g4a, _ = scenario_run("g4a")
    for d in ("npu-0", "npu-64"):  # the final state equals G4a
        assert sim.device(d).tables.groups == g4a.device(d).tables.groups
        assert sim.device(d).tables.routes == g4a.device(d).tables.routes


# ------------------------------------------------------------------ safety rules (SPEC 12)

ALL = ["g1", "g2", "g4a", "g4b", "g5", "g6", "g6b", "g7", "g8"]


@pytest.mark.parametrize("name", ALL)
def test_versions_only_go_up_and_one_command_per_device(name: str) -> None:
    """Rules 3 and 8: per device, table versions never go down, and a command is only sent
    after the previous one was applied (or timed out)."""
    sim, report = scenario_run(name)
    device_write = 3  # ms, typical profile
    last_send: dict[str, int] = {}
    versions: dict[str, int] = {}
    for r in report.trace:
        if r["kind"] == "send" and r["src"] == "controller" and r["type"] not in (
            "KEEPALIVE", "OCS_SET"
        ):
            t = r["t"] // 1000
            assert t >= last_send.get(r["dst"], -device_write) + device_write, (r["dst"], t)
            last_send[r["dst"]] = t
        if r["kind"] == "apply":
            assert r["ok"], r
            assert r["version"] >= versions.get(r["device"], 1), r
            versions[r["device"]] = r["version"]


@pytest.mark.parametrize("name", ["g4a", "g4b", "g7", "g8"])
def test_controller_never_drops_capacity_below_local_protection(name: str) -> None:
    """Rule 6: after the local prune leaves the pair at 0.5, nothing the controller does lowers it;
    and rule 4: the repair only disconnects the failed lanes' cross-connects."""
    sim, report = scenario_run(name)
    pair = [s for s in report.capacity if s.key == "npu-0/npu-64"]
    assert min(s.value for s in pair) == 0.5
    [run] = controller(sim).scheduler.runs
    lane_at = {x.ocs_port: x.id for x in sim.spec.lanes if x.ocs_port is not None}
    if run.plan is not None:
        for xc in run.plan.disconnect:
            assert {lane_at[xc.north], lane_at[xc.south]} <= run.incident.lanes


# ------------------------------------------------------------------ the report's capacity samples


@pytest.mark.parametrize("name", ALL)
def test_capacity_samples_end_at_the_measured_values(name: str) -> None:
    """Replaying the capacity samples gives the capacity measured at the end, for every device,
    every domain and every sampled pair (a lane's service also depends on the lane it faces)."""
    sim, report = scenario_run(name)
    last = {s.key: s.value for s in report.capacity}
    keys = set(last) | {d.id for d in sim.spec.devices} | {dom.id for dom in sim.spec.domains}
    wrong = {k: (last.get(k, 1.0), sim.capacity(k)) for k in sorted(keys)
             if last.get(k, 1.0) != sim.capacity(k)}
    assert wrong == {}


@pytest.mark.parametrize(("name", "key"), [("g4a", "G4a"), ("g4b", "G4b")])
def test_g4_final_capacity_in_the_samples(name: str, key: str, golden: dict[str, Any]) -> None:
    sim, report = scenario_run(name)
    for k, value in golden[key]["capacity_final"].items():
        assert capacity_at(report, k, sim.now_us() / 1000) == value, k
