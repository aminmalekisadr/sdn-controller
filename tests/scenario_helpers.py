"""Running golden scenarios and reading their traces, for the Phase 7 tests."""

import functools
from typing import Any

from sdnctl.interfaces import SimReport
from sdnctl.routing_engine import RoutingEngine
from sdnctl.scenario import load_scenario, run_scenario, start_scenario
from sdnctl.sim import Simulator
from sdnctl.topology import steady_state_view
from sdnctl.topology.view import TopologySnapshot
from tests.conftest import REPO_ROOT

SCENARIOS = REPO_ROOT / "scenarios"
ENGINE = RoutingEngine()
WAVE_STOPS = (2, 6, 9, 10_064, 10_067, 10_070)  # G2 and G6 wave boundaries (ms), typical
STOPS = {"g2": WAVE_STOPS, "g6": WAVE_STOPS}


@functools.cache
def scenario_run(name: str, profile: str = "typical") -> tuple[Simulator, SimReport]:
    """Run scenarios/<name>.json to its end (cached; a typical run shares the stepwise run)."""
    if profile == "typical":
        sim, report, _ = stepwise(name, STOPS.get(name, ()))
        return sim, report
    sim = run_scenario(load_scenario(SCENARIOS / f"{name}.json"), profile)
    return sim, sim.report()


@functools.cache
def stepwise(name: str, stops: tuple[int, ...]) -> tuple[Simulator, SimReport,
                                                          dict[int, tuple[int, int]]]:
    """Run a scenario, pausing at each stop (ms) to count (looping, blackholed) NPU pairs on the
    simulated devices' installed tables; then run it to its end."""
    scenario = load_scenario(SCENARIOS / f"{name}.json")
    sim = start_scenario(scenario)
    view = _routing_view(sim)
    counts: dict[int, tuple[int, int]] = {}
    for stop in stops:
        sim.run(stop)
        tables = {d.id: sim.device(d.id).tables for d in sim.spec.devices}
        report = ENGINE.check_state(tables, view)
        counts[stop] = (report.looping_pairs, report.blackholed_pairs)
    sim.run(scenario.end_ms())
    return sim, sim.report(), counts


def _routing_view(sim: Simulator) -> TopologySnapshot:
    return steady_state_view(sim.spec)  # check_state uses it for the routing domain only


def ms(t_us: int) -> int:
    """Microseconds to whole milliseconds."""
    assert t_us % 1000 == 0, t_us
    return t_us // 1000


def times(report: SimReport, kind: str, **match: Any) -> list[int]:
    """Times (ms) of the trace records of one kind whose fields equal match."""
    return [
        ms(r["t"])
        for r in report.trace
        if r["kind"] == kind and all(r.get(k) == v for k, v in match.items())
    ]


def state_at(report: SimReport, state: str) -> list[int]:
    """When (ms) the incident entered a state."""
    return times(report, "incident_state", state=state)


def sent(report: SimReport, before_ms: int | None = None, after_ms: int | None = None) -> dict[
    str, int
]:
    """Counted messages by type, sent in a time window (ms)."""
    out: dict[str, int] = {}
    for r in report.trace:
        if r["kind"] != "send" or r["type"] in ("KEEPALIVE", "HELLO"):
            continue
        t = r["t"] / 1000
        if (before_ms is not None and t >= before_ms) or (after_ms is not None and t < after_ms):
            continue
        out[r["type"]] = out.get(r["type"], 0) + 1
    return out


def capacity_at(report: SimReport, key: str, t_ms: float) -> float:
    """A capacity key's value at a time, from the capacity samples (1.0 before any sample)."""
    value = 1.0
    for s in report.capacity:
        if s.key == key and s.time <= t_ms * 1000:
            value = s.value
    return value
