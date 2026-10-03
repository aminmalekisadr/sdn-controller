"""Topology counts against golden.json (SPEC 3.2, 3.3), and spec files in build/ (SPEC 9.6)."""

from collections import Counter
from typing import Any

import pytest

from sdnctl.model import TopologySpec
from sdnctl.topology import read_spec, steady_state_view, write_spec
from sdnctl.types import LinkKind, Role, Tier
from tests.conftest import REPO_ROOT, VARIANTS, built

# These need the Routing Engine; they are checked in Phase 4.
ROUTING_KEYS = {
    "route_entries",
    "npu_pair_hops",
    "nexthop_set_sizes",
    "steady_looping_pairs",
    "steady_blackholed_pairs",
}
TIER_ORDER = [t.value for t in Tier]


def counts(spec: TopologySpec) -> dict[str, Any]:
    """The topology counts golden.json lists, computed from a spec and its steady-state view."""
    devices = {d.id: d for d in spec.devices}
    device_of = {p.id: p.device for p in spec.ports}
    by_type: Counter[str] = Counter()
    for link in spec.links:
        a, b = devices[device_of[link.a]], devices[device_of[link.b]]
        ends = sorted((a.tier.value, b.tier.value), key=TIER_ORDER.index)
        key = "-".join(ends)
        if {a.role, b.role} == {Role.UNION, Role.EXT}:
            key += "(union-ext)"
        if link.kind in (LinkKind.D2D, LinkKind.OPTICAL):
            key += f"[{link.kind.value}]"
        by_type[key] += 1
    tiers = Counter(d.tier for d in spec.devices)
    roles = Counter(d.role for d in spec.devices)
    view = steady_state_view(spec)
    neighbors = {d: view.neighbors(d) for d in devices}
    return {
        "devices": len(spec.devices),
        "npu": tiers[Tier.NPU],
        "l1": tiers[Tier.L1],
        "l1_union": roles[Role.UNION],
        "l1_ext": roles[Role.EXT],
        "l2": tiers[Tier.L2],
        "links": len(spec.links),
        "links_by_type": dict(by_type),
        "lanes": len(spec.lanes),
        "optical_domains": len(spec.domains),
        "optical_domains_by_tier": dict(
            Counter(devices[dom.device].tier.value for dom in spec.domains)
        ),
        "working_modules": sum(1 for m in spec.modules if not m.spare),
        "spare_modules": sum(1 for m in spec.modules if m.spare),
        "ocs_cross_connects": len(spec.xconnects),
        "ocs_ports_per_side": sum(
            1 for x in spec.lanes if x.ocs_port is not None and x.ocs_port.startswith("N")
        ),
        "routed_devices": sum(1 for d in devices if view.routed(d)),
        "group_entries": sum(len(n) for n in neighbors.values()),
        "group_entries_in_routing_domain": sum(
            1 for d, ns in neighbors.items() if view.routed(d) for n in ns if view.routed(n)
        ),
    }


def golden_key(name: str, spares: int) -> str:
    """The golden.json block a topology variant is checked against."""
    return "topology1" if name == "topology1" else f"topology2_spare{spares}"


@pytest.mark.parametrize(("name", "two_plus_two", "spares"), VARIANTS)
def test_counts_match_golden(
    name: str, two_plus_two: bool, spares: int, golden: dict[str, Any]
) -> None:
    # golden.json lists the 2+2 builds; 2:1 changes only module wiring, so the counts are the same
    expected = {
        k: v for k, v in golden[golden_key(name, spares)].items() if k not in ROUTING_KEYS
    }
    actual = counts(built(name, two_plus_two, spares))
    assert {k: actual[k] for k in expected} == expected


@pytest.mark.parametrize(("name", "two_plus_two", "spares"), VARIANTS)
def test_spec_file_round_trip(name: str, two_plus_two: bool, spares: int) -> None:
    spec = built(name, two_plus_two, spares)
    path = write_spec(spec, REPO_ROOT / "build" / "specs")
    assert read_spec(path) == spec
