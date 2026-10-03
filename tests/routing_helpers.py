"""Reroute scenarios for Routing Engine tests: tables before, during and after a neighbor loss.

A real TopologyManager receives the LANE_DOWN and LANE_UP reports of a module failure and its
repair; the Routing Engine computes the tables for each view. Mixed states follow SPEC 11.0
("States between waves"): going down, every device already has its post-failure groups and uses
new routes once its wave is applied; going up, a device gets its restored groups and routes together
when its wave is applied.
"""

import functools
from dataclasses import dataclass, field

from sdnctl.model import Incident, TopologySpec
from sdnctl.routing_engine import RoutingEngine
from sdnctl.tables import AdjacencyChange, DeviceTables, TableDiff, Tables, Wave
from sdnctl.topology.view import TopologySnapshot
from sdnctl.topology_manager import TopologyManager
from sdnctl.types import DeviceId
from tests.conftest import built
from tests.tm_helpers import report_failure, report_repair


@dataclass
class Reroute:
    """Everything a neighbor-loss scenario needs, computed once."""

    spec: TopologySpec
    engine: RoutingEngine
    incident: Incident
    view0: TopologySnapshot
    view1: TopologySnapshot
    view2: TopologySnapshot
    tables0: Tables  # steady state
    tables1: Tables  # after the failure
    tables2: Tables  # after the repair
    diff_down: TableDiff
    waves_down: list[Wave]
    diff_up: TableDiff
    waves_up: list[Wave]
    memo: dict[str, dict[str, tuple[int, int]]] = field(default_factory=dict)

    def down_state(self, updated: set[DeviceId]) -> Tables:
        """Going down: post-failure groups everywhere; new routes on updated devices."""
        return {
            d: DeviceTables(
                d,
                t1.version,
                (t1 if d in updated else self.tables0[d]).routes,
                t1.groups,
            )
            for d, t1 in self.tables1.items()
        }

    def up_state(self, updated: set[DeviceId]) -> Tables:
        """Going up: updated devices have their restored groups and routes."""
        return {
            d: (self.tables2[d] if d in updated else self.tables1[d]) for d in self.tables1
        }

    def states(self, direction: str) -> dict[str, tuple[int, int]]:
        """(looping, blackholed) pairs before, after each wave, and after a wrong first wave."""
        if direction not in self.memo:
            self.memo[direction] = self._states(direction)
        return self.memo[direction]

    def _states(self, direction: str) -> dict[str, tuple[int, int]]:
        waves = self.waves_down if direction == "down" else self.waves_up
        state = self.down_state if direction == "down" else self.up_state
        view = self.view1 if direction == "down" else self.view2
        first = "before_controller" if direction == "down" else "before_restore"
        out: dict[str, tuple[int, int]] = {}
        cum: set[DeviceId] = set()
        report = self.engine.check_state(state(cum), view)
        out[first] = (report.looping_pairs, report.blackholed_pairs)
        for wave in waves:
            cum |= set(wave.devices)
            report = self.engine.check_state(state(cum), view)
            out[f"after_wave{wave.index}"] = (report.looping_pairs, report.blackholed_pairs)
        wrong = set(list(reversed(waves))[0].devices)
        report = self.engine.check_state(state(wrong), view)
        out["wrong_order_after_wave1"] = (report.looping_pairs, report.blackholed_pairs)
        return out


@functools.cache
def reroute(name: str, two_plus_two: bool, spares: int, modules: tuple[str, ...]) -> Reroute:
    """Fail modules at 0 (reports at 1 ms), repair them; compute every table set."""
    spec = built(name, two_plus_two, spares)
    engine = RoutingEngine()
    tm = TopologyManager(correlation_window=5000)
    tm.load(spec)
    view0 = tm.view()
    tables0 = engine.compute(view0)

    incident, lost = report_failure(tm, spec, modules)
    view1 = tm.view()
    tables1 = engine.compute(view1)
    pruned = {  # the controller's copy after the devices' local prune
        d: DeviceTables(d, 1, t0.routes, tables1[d].groups) for d, t0 in tables0.items()
    }
    diff_down = engine.diff(pruned, tables1)
    waves_down = engine.order_waves(diff_down, AdjacencyChange(incident.lost, frozenset()))

    report_repair(tm, lost)
    view2 = tm.view()
    tables2 = engine.compute(view2)
    diff_up = engine.diff(tables1, tables2)
    waves_up = engine.order_waves(diff_up, AdjacencyChange(frozenset(), incident.lost))
    return Reroute(spec, engine, incident, view0, view1, view2, tables0, tables1, tables2,
                   diff_down, waves_down, diff_up, waves_up)


def g2() -> Reroute:
    """G2: topology 1, 2:1, l1-1536.m0 fails."""
    return reroute("topology1", False, 0, ("l1-1536.m0",))


def g6() -> Reroute:
    """G6: topology 2, 2+2, npu-0.m0 and npu-0.m1 fail."""
    return reroute("topology2", True, 0, ("npu-0.m0", "npu-0.m1"))
