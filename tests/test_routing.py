"""Routing Engine (Phase 4): table numbers of SPEC 3, compute time, and G1, G2, G3, G6."""

import time
from collections import Counter
from typing import Any

import pytest

from sdnctl.routing_engine import RoutingEngine
from sdnctl.tables import (
    AdjacencyChange,
    DeviceTables,
    GroupChange,
    GroupEntry,
    RouteChange,
    RouteEntry,
    TableDiff,
)
from sdnctl.topology import steady_state_view
from sdnctl.types import Tier
from tests.conftest import built
from tests.routing_helpers import Reroute, g2, g6, reroute

ENGINE = RoutingEngine()


def pair_counts(states: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """golden.json's state blocks as (looping, blackholed)."""
    return {k: (v["looping_pairs"], v["blackholed_pairs"]) for k, v in states.items()}


def roles(r: Reroute) -> dict[str, str]:
    """Device id -> role."""
    return {d.id: d.role.value for d in r.spec.devices}


def without_live_nexthop(r: Reroute) -> int:
    """Endpoint entries whose every old next hop lost its group."""
    ends = {v for v, _ in r.incident.lost}
    return sum(
        1
        for v in ends
        for e in r.tables0[v].routes.values()
        if e.neighbors and not any(u in r.tables1[v].groups for u in e.neighbors)
    )


@pytest.mark.parametrize(("key", "spares"), [("topology1", 0), ("topology2_spare1", 1)])
def test_table_numbers_match_golden(key: str, spares: int, golden: dict[str, Any]) -> None:
    spec = built("topology1" if key == "topology1" else "topology2", True, spares)
    view = steady_state_view(spec)
    tables = ENGINE.compute(view)
    npus = [d.id for d in spec.devices if d.tier is Tier.NPU]
    routed = {d.id for d in spec.devices if view.routed(d.id)}
    g = golden[key]
    assert len(routed) == g["routed_devices"]
    assert sum(len(t.routes) for t in tables.values()) == g["route_entries"]
    assert sum(len(t.groups) for t in tables.values()) == g["group_entries"]
    assert sum(
        1 for d in routed for n in tables[d].groups if n in routed
    ) == g["group_entries_in_routing_domain"]
    hops = Counter(tables[s].routes[d].hops for s in npus for d in npus if s != d)
    assert {str(k): v for k, v in hops.items()} == g["npu_pair_hops"]
    sizes = Counter(len(e.neighbors) for t in tables.values() for e in t.routes.values())
    assert {str(k): v for k, v in sizes.items()} == g["nexthop_set_sizes"]
    report = ENGINE.check_state(tables, view)
    assert report.looping_pairs == g["steady_looping_pairs"]
    assert report.blackholed_pairs == g["steady_blackholed_pairs"]
    assert all(t.version == view.version for t in tables.values())


def test_compute_time_on_topology1() -> None:
    """SPEC 2: compute() on topology 1 must finish within 5 s (wall clock, this machine)."""
    view = steady_state_view(built("topology1", True))
    start = time.perf_counter()
    ENGINE.compute(view)
    elapsed = time.perf_counter() - start
    print(f"\ncompute() on topology 1: {elapsed:.2f} s")
    assert elapsed < 5.0


def test_compute_is_pure_and_deterministic() -> None:
    view = steady_state_view(built("topology2", True, 1))
    first, second = ENGINE.compute(view), ENGINE.compute(view)
    assert first == second
    assert ENGINE.diff(first, second) == TableDiff(routes={}, groups={})
    assert view.version == 1


def test_g1_group_only_change() -> None:
    r = reroute("topology1", True, 0, ("l1-1536.m0",))
    assert r.incident.lost == frozenset()
    assert r.diff_down.route_entries() == 0 and r.diff_down.group_entries() == 0
    assert r.waves_down == []
    assert r.diff_up.route_entries() == 0
    assert r.diff_up.group_entries() == 8
    assert [w.devices for w in r.waves_up] == [
        ("l1-1536", "l2-2304", "l2-2305", "l2-2306", "l2-2307")
    ]


def test_g2_route_diff_and_waves(golden: dict[str, Any]) -> None:
    r, g = g2(), golden["G2"]
    d, role = r.diff_down, roles(r)
    assert d.route_entries() == g["route_entries_changed"]
    assert len(d.routes) == g["devices_changed"] and d.group_entries() == 0
    assert dict(Counter(role[v] for v in d.routes)) == g["devices_changed_by_role"]
    assert dict(Counter(role[v] for v in d.routes for _ in d.routes[v])) == g["entries_by_role"]
    per = g["entries_per_device"]
    assert len(d.routes["l1-1536"]) == per["l1-1536"]
    assert len(d.routes["l2-2304"]) == per["l2-2304"] and len(d.routes["l2-2305"]) == per["l2-2305"]
    others = {len(c) for v, c in d.routes.items() if v not in ("l1-1536", "l2-2304", "l2-2305")}
    assert sorted(others) == per["each_other_changed_union"]
    [example] = [c for c in d.routes["l1-1536"] if c.dest == "npu-64"]
    assert example.old is not None and example.new is not None
    assert example.old.neighbors == ("l2-2304", "l2-2305", "l2-2306", "l2-2307")
    assert example.new.neighbors == ("l2-2306", "l2-2307")
    assert [len(w.devices) for w in r.waves_down] == g["waves_down"]
    assert list(r.waves_down[-1].devices) == g["endpoints"]
    assert [len(w.devices) for w in r.waves_up] == g["waves_restore"]
    assert list(r.waves_up[0].devices) == g["endpoints"]
    assert without_live_nexthop(r) == g["entries_without_live_nexthop"]


def test_g2_pair_counts(golden: dict[str, Any]) -> None:
    r = g2()
    assert r.states("down") == pair_counts(golden["G2"]["down_states"])
    assert r.states("up") == pair_counts(golden["G2"]["restore_states"])


def test_g6_route_diff_and_waves(golden: dict[str, Any]) -> None:
    r, g = g6(), golden["G6"]
    d, role = r.diff_down, roles(r)
    assert d.route_entries() == g["route_entries_changed"]
    assert len(d.routes) == g["devices_changed"]
    assert dict(Counter(role[v] for v in d.routes)) == g["devices_changed_by_role"]
    assert dict(Counter(role[v] for v in d.routes for _ in d.routes[v])) == g["entries_by_role"]
    [example] = [c for c in d.routes["npu-0"] if c.dest == "npu-8"]
    assert example.old == RouteEntry("npu-8", ("npu-64",), 3)
    assert example.new == RouteEntry(
        "npu-8", ("l1-408", "l1-409", "l1-432", "l1-433", "l1-456", "l1-457"), 4
    )
    assert [len(w.devices) for w in r.waves_down] == g["waves_down"]
    assert list(r.waves_down[-1].devices) == g["endpoints"]
    assert [len(w.devices) for w in r.waves_up] == g["waves_restore"]
    assert without_live_nexthop(r) == g["entries_without_live_nexthop"]


def test_g6_pair_counts(golden: dict[str, Any]) -> None:
    r = g6()
    assert r.states("down") == pair_counts(golden["G6"]["down_states"])
    assert r.states("up") == pair_counts(golden["G6"]["restore_states"])


def test_g3_wrong_order_makes_loops(golden: dict[str, Any]) -> None:
    """G3: order_waves() output fed in reverse into check_state()."""
    g3 = golden["G3"]
    assert g2().states("down")["wrong_order_after_wave1"][0] == g3[
        "T1_down_wrong_order_looping_pairs"
    ]
    assert g2().states("up")["wrong_order_after_wave1"][0] == g3[
        "T1_restore_wrong_order_looping_pairs"
    ]
    assert g6().states("down")["wrong_order_after_wave1"][0] == g3[
        "T2_down_wrong_order_looping_pairs"
    ]
    assert g6().states("up")["wrong_order_after_wave1"][0] == g3[
        "T2_restore_wrong_order_looping_pairs"
    ]


@pytest.mark.parametrize("scenario", [g2, g6], ids=["G2", "G6"])
def test_repair_returns_the_initial_tables(scenario: Any, golden: dict[str, Any]) -> None:
    r = scenario()
    assert ENGINE.diff(r.tables0, r.tables2) == TableDiff(routes={}, groups={})
    assert golden["G6"]["final_tables_equal_initial"] is True


def test_order_waves_rules() -> None:
    entry = RouteEntry("npu-1", ("npu-1",), 1)
    group = GroupEntry("npu-1", ("npu-0.p0.0",))
    diff = TableDiff(
        routes={"npu-2": [RouteChange("npu-1", entry, None)]},
        groups={"npu-0": [GroupChange("npu-1", group, None)]},
    )
    none = AdjacencyChange(frozenset(), frozenset())
    assert [w.devices for w in ENGINE.order_waves(diff, none)] == [("npu-0", "npu-2")]
    lost = AdjacencyChange(frozenset({("npu-0", "npu-1")}), frozenset())
    assert [w.devices for w in ENGINE.order_waves(diff, lost)] == [("npu-2",), ("npu-0",)]
    back = AdjacencyChange(frozenset(), frozenset({("npu-0", "npu-1")}))
    waves = ENGINE.order_waves(diff, back)
    assert [(w.index, w.devices) for w in waves] == [(1, ("npu-0",)), (2, ("npu-2",))]
    assert ENGINE.order_waves(TableDiff({}, {}), lost) == []
    with pytest.raises(ValueError):
        ENGINE.order_waves(diff, AdjacencyChange(lost.lost, back.restored))


def test_diff_lists_added_removed_and_changed_entries() -> None:
    a = RouteEntry("npu-1", ("npu-1",), 1)
    b = RouteEntry("npu-1", ("npu-2",), 2)
    c = RouteEntry("npu-3", ("npu-3",), 1)
    old = {"npu-0": DeviceTables("npu-0", 1, {"npu-1": a, "npu-3": c}, {})}
    new = {"npu-0": DeviceTables("npu-0", 2, {"npu-1": b}, {})}
    diff = ENGINE.diff(old, new)
    assert diff.routes == {
        "npu-0": [RouteChange("npu-1", a, b), RouteChange("npu-3", c, None)]
    }
    assert diff.groups == {}
    assert ENGINE.diff(old, old) == TableDiff(routes={}, groups={})
