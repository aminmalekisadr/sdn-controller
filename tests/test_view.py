"""TopologySnapshot: steady state (SPEC 9.8 items 6-7), and prunes and capacity vs golden.json."""

from collections.abc import Iterable
from typing import Any

import pytest

from sdnctl.model import TopologySpec
from sdnctl.topology import SpecIndex, TopologySnapshot, steady_state_view
from sdnctl.topology.view import steady_lane_states
from sdnctl.types import CrossConnect, LaneState
from tests.conftest import built


def capacity(view: TopologySnapshot, key: str) -> float:
    """Capacity by golden.json key: 'a/b' is a pair, '<device>.d<k>' a domain, else a device."""
    if "/" in key:
        a, b = key.split("/")
        return view.pair_capacity(a, b)
    if "." in key:
        return view.domain_capacity(key)
    return view.device_capacity(key)


def module_lanes(spec: TopologySpec, modules: Iterable[str]) -> list[str]:
    """Lanes of the given modules."""
    by_id = {m.id: m for m in spec.modules}
    return [x for m in modules for x in by_id[m].lanes]


def failed_view(spec: TopologySpec, modules: list[str]) -> TopologySnapshot:
    """The view after modules fail: their lanes and the lanes they face are DOWN."""
    index = SpecIndex(spec)
    steady = steady_state_view(spec, index)
    states = steady_lane_states(index)
    for x in module_lanes(spec, modules):
        states[x] = LaneState.DOWN
        peer = steady.peer_of(x)
        assert peer is not None
        states[peer] = LaneState.DOWN
    return TopologySnapshot(index, 2, states, spec.xconnects)


def test_topology1_steady_state() -> None:
    spec = built("topology1", True)
    view = steady_state_view(spec)
    assert view.neighbors("l1-1536") == tuple(
        [f"npu-{i}" for i in range(8)]
        + [f"l1-{i}" for i in range(1552, 1556)]
        + [f"l2-{i}" for i in range(2304, 2308)]
    )
    assert view.active_lanes("l1-1536", "l2-2304") == ("l1-1536.p12.0", "l1-1536.p12.1")
    assert view.version == 1
    for d in spec.devices:
        assert view.device_capacity(d.id) == 1.0
        for n in view.neighbors(d.id):
            assert view.pair_capacity(d.id, n) == 1.0
    assert all(view.domain_capacity(dom.id) == 1.0 for dom in spec.domains)


def test_topology2_steady_state() -> None:
    spec = built("topology2", True, 1)
    view = steady_state_view(spec)
    assert view.neighbors("npu-0") == tuple(
        [f"npu-{i}" for i in range(1, 8)]
        + ["npu-64", "l1-408", "l1-409", "l1-432", "l1-433", "l1-456", "l1-457"]
    )
    p13_p14 = ("npu-0.p13.0", "npu-0.p13.1", "npu-0.p14.0", "npu-0.p14.1")
    assert view.active_lanes("npu-0", "npu-64") == p13_p14
    assert view.usable_lanes("npu-0", "npu-64") == p13_p14
    assert view.lane_state("npu-0.p15.0") is LaneState.IDLE
    assert view.peer_of("npu-0.p15.0") is None
    assert view.peer_of("npu-0.p13.0") == "npu-64.p13.0"
    assert view.lane_by_ocs_port("N257") == "npu-0.p15.0"
    assert view.routed("l1-384") and not view.routed("l2-576")
    assert view.domain_capacity("npu-0.d0") == 1.0
    assert view.device_capacity("npu-0") == 1.0


SCENARIOS = [
    ("G1", ("topology1", True, 0), "prunes"),
    ("G2", ("topology1", False, 0), "emptied_groups"),
    ("G4a", ("topology2", True, 1), "prunes"),
    ("G4b", ("topology2", False, 1), "prunes"),
]


@pytest.mark.parametrize(("key", "variant", "field"), SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_failure_prunes_match_golden(
    key: str, variant: tuple[str, bool, int], field: str, golden: dict[str, Any]
) -> None:
    spec = built(*variant)
    module = "l1-1536.m0" if variant[0] == "topology1" else "npu-0.m0"
    steady, failed = steady_state_view(spec), failed_view(spec, [module])
    for entry in golden[key][field]:
        dev, nbr = entry["device"], entry["neighbor"]
        assert list(steady.active_lanes(dev, nbr)) == entry["before"]
        assert list(failed.active_lanes(dev, nbr)) == entry["after"]
        assert (nbr in failed.neighbors(dev)) == bool(entry["after"])


def test_g1_capacity_matches_golden(golden: dict[str, Any]) -> None:
    view = failed_view(built("topology1", True), ["l1-1536.m0"])
    for key, value in golden["G1"]["capacity"].items():
        assert round(capacity(view, key), 6) == value, key


@pytest.mark.parametrize(("key", "two_plus_two"), [("G4a", True), ("G4b", False)])
def test_g4_ocs_restore_matches_golden(
    key: str, two_plus_two: bool, golden: dict[str, Any]
) -> None:
    g = golden[key]
    spec = built("topology2", two_plus_two, 1)
    failed = failed_view(spec, ["npu-0.m0"])
    for k, value in g["capacity_during"].items():
        assert round(capacity(failed, k), 6) == value, k

    # rewire: dead cross-connects out, spare lanes to the same peer lanes; lanes now verifying
    gone = {CrossConnect(n, s) for n, s in g["ocs_disconnect"]}
    xconnects = [xc for xc in spec.xconnects if xc not in gone]
    xconnects += [CrossConnect(n, s) for n, s in g["ocs_connect"]]
    index = SpecIndex(spec)
    states = {x: failed.lane_state(x) for x in index.lanes}
    for x in g["module_lanes"]:
        states[x] = LaneState.FAILED
    new_pairs = [
        (failed.lane_by_ocs_port(n), failed.lane_by_ocs_port(s)) for n, s in g["ocs_connect"]
    ]
    new_lanes = [x for pair in new_pairs for x in pair if x is not None]
    assert len(new_lanes) == 4
    for x in new_lanes:
        states[x] = LaneState.VERIFYING
    verifying = TopologySnapshot(index, 3, states, xconnects, ["npu-0.m0"], lane_up=new_lanes)
    assert verifying.peer_of(g["spare_lanes"][0]) == new_pairs[0][1]
    for entry in g["group_set"]:
        dev = entry["device"]
        nbr = "npu-64" if dev == "npu-0" else "npu-0"
        assert list(verifying.usable_lanes(dev, nbr)) == entry["lanes"]
    assert verifying.pair_capacity("npu-0", "npu-64") == 0.5  # verifying lanes carry nothing yet

    for x in new_lanes:
        states[x] = LaneState.ACTIVE
    active = TopologySnapshot(index, 4, states, xconnects, ["npu-0.m0"])
    for k, value in g["capacity_final"].items():
        assert round(capacity(active, k), 6) == value, k


def test_verifying_lane_needs_lane_up_from_both_ends() -> None:
    spec = built("topology2", True, 1)
    index = SpecIndex(spec)
    states = steady_lane_states(index)
    states["npu-0.p13.0"] = states["npu-64.p13.0"] = LaneState.VERIFYING
    one_end = TopologySnapshot(index, 2, states, spec.xconnects, lane_up=["npu-0.p13.0"])
    assert "npu-0.p13.0" not in one_end.usable_lanes("npu-0", "npu-64")
    both = TopologySnapshot(
        index, 2, states, spec.xconnects, lane_up=["npu-0.p13.0", "npu-64.p13.0"]
    )
    assert "npu-0.p13.0" in both.usable_lanes("npu-0", "npu-64")
    assert "npu-0.p13.0" not in both.active_lanes("npu-0", "npu-64")


def test_snapshot_needs_every_lane_state() -> None:
    spec = built("topology2", True, 0)
    index = SpecIndex(spec)
    states = steady_lane_states(index)
    del states["npu-0.p0.0"]
    with pytest.raises(ValueError, match="every lane"):
        TopologySnapshot(index, 1, states, spec.xconnects)
